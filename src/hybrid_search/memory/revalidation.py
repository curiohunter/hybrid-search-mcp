"""Commit-aware memory invalidation — evidence-based HEAD projection (P1-2, v3).

A qa answer is written against the code as it was at answer time. When
the code its anchors point at differs NOW, the memory is no longer
verified against current code.

v2 recomputed flags from (HEAD, qa timestamp): "the code the qa saw" was
ESTIMATED as the last commit before the qa timestamp. Round-2 re-review
showed the estimate breaks on the most common agent workflow — edit
(uncommitted) → ask → commit later: the qa saw the NEW code, but the
timestamp estimate resolves to the OLD commit and false-flags it. It is
also wrong for qa written on another branch.

v3 removes the estimation for all new records: **qa write time captures
what the qa actually saw** —

    anchor_head:   repo HEAD at write time
    anchor_dirty:  whether the anchored paths had uncommitted changes
    anchor_hashes: {path: git blob hash of the WORKING-TREE content}

and the projection compares stored working-tree blob hashes directly
against blobs at the pinned HEAD. Legacy records (no evidence) keep the
timestamp estimate as a fallback; estimation-derived state is inherently
weaker, which the trust contract already reflects (legacy records can
never anchor STRONG regardless).

Round-2 re-review hardening, all in this module:

- found / absent / error are distinct: path absence is a clean signal
  (``git ls-tree`` exit 0, empty output); timeouts and process failures
  raise ``GitError``. Any error makes the pass INCOMPLETE — the caller
  must keep the previous projection instead of silently un-flagging.
- every git command uses the HEAD SHA captured once at pass start;
  a checkout/commit mid-pass cannot mix two HEADs into one projection.
  Callers re-verify HEAD (``head_unchanged``) immediately before the
  atomic replace and discard the result on mismatch (CAS).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from hybrid_search.memory.integrity import _extract_result_paths
from hybrid_search.memory.supersession import _frontmatter_value, _parse_timestamp

logger = logging.getLogger(__name__)

__all__ = [
    "GitError",
    "ProjectionResult",
    "anchor_paths",
    "collect_anchor_evidence",
    "head_unchanged",
    "project_revalidations",
]

# A qa's anchors are the TOP results it was answered from. Deeper ranks
# are incidental co-retrievals — anchoring on all ten would invalidate
# half the corpus every time a hot file changes.
_ANCHOR_TOP_N = 3

_GIT_TIMEOUT_S = 10


class GitError(Exception):
    """Infrastructure failure (timeout, process error, bad repo state) —
    NOT semantic absence. Absence is reported as None/empty by lookups."""


def anchor_paths(content: str) -> list[str]:
    """The first N distinct result paths of a qa record."""
    seen: list[str] = []
    for path in _extract_result_paths(content or ""):
        if path not in seen:
            seen.append(path)
        if len(seen) >= _ANCHOR_TOP_N:
            break
    return seen


def _default_git(repo: Path, *argv: str) -> str:
    """stdout of a git command; raises GitError on any failure."""
    try:
        proc = subprocess.run(
            ["git", *argv], cwd=repo, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception as exc:
        raise GitError(f"git {argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {argv[0]} rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


# --- write-time evidence -----------------------------------------------------


def collect_anchor_evidence(
    repo: Path,
    paths: list[str],
    *,
    run_git: Callable[..., str] = _default_git,
) -> dict | None:
    """What the qa actually saw: HEAD, dirtiness, working-tree blob hashes.

    Called on the qa WRITE path — must never raise and never block it;
    None simply means "legacy record" (timestamp fallback at read time).
    """
    if not paths:
        return None
    try:
        head = run_git(repo, "rev-parse", "HEAD")
        existing = [p for p in paths[:_ANCHOR_TOP_N] if (repo / p).is_file()]
        hashes: dict[str, str] = {}
        if existing:
            out = run_git(repo, "hash-object", "--", *existing)
            hashes = dict(zip(existing, out.splitlines()))
        if not hashes:
            return None
        dirty_out = run_git(repo, "status", "--porcelain", "--", *existing)
        return {"head": head, "dirty": bool(dirty_out), "hashes": hashes}
    except Exception:
        return None


def _stored_anchor_hashes(content: str) -> dict[str, str] | None:
    raw = _frontmatter_value(content, "anchor_hashes")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    return {str(k): str(v) for k, v in data.items()}


# --- projection ---------------------------------------------------------------


@dataclass
class ProjectionResult:
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    head: str | None = None
    # False when any git ERROR occurred: the projection may be missing
    # flags it could not compute, so callers MUST NOT replace the stored
    # projection with it (a transient timeout would silently un-flag).
    complete: bool = True


class _RepoView:
    """Memoised git lookups pinned to one HEAD SHA for one pass."""

    def __init__(self, repo: Path, head: str, run_git: Callable[..., str]) -> None:
        self._repo = repo
        self._head = head
        self._git = run_git
        self._base_by_ts: dict[str, str | None] = {}
        self._blob: dict[tuple[str, str], str | None] = {}

    def base_commit(self, ts_iso: str) -> str | None:
        """Last commit on the pinned HEAD's history at or before ts.
        None = repo history starts later (absence, not error)."""
        if ts_iso not in self._base_by_ts:
            out = self._git(
                self._repo, "rev-list", "-1", f"--before={ts_iso}", self._head,
            )
            self._base_by_ts[ts_iso] = out or None
        return self._base_by_ts[ts_iso]

    def blob(self, commit: str, path: str) -> str | None:
        """Blob sha of ``path`` at ``commit``; None when the path does
        not exist there (clean absence via ls-tree exit 0 + empty)."""
        key = (commit, path)
        if key not in self._blob:
            out = self._git(self._repo, "ls-tree", commit, "--", path)
            sha: str | None = None
            if out:
                fields = out.split()
                if len(fields) >= 3 and fields[1] == "blob":
                    sha = fields[2]
            self._blob[key] = sha
        return self._blob[key]

    def blobs_differ_beyond_whitespace(self, old_blob: str, new_blob: str) -> bool:
        """False when the two blobs differ only in whitespace/blank
        lines. ``--name-only`` ignores whitespace flags (tree-level), so
        the patch output itself is inspected. An unreadable old blob
        (working-tree content that was never committed) cannot be
        refined — treated as a real change, which over-flags slightly
        but never hides one."""
        try:
            out = self._git(
                self._repo, "diff", "-w", "--ignore-blank-lines",
                old_blob, new_blob,
            )
        except GitError:
            return True
        return out != ""

    def cause_commit(self, path: str, since: str | None = None) -> str:
        """Most recent commit touching ``path`` up to the pinned HEAD."""
        try:
            rev_range = f"{since}..{self._head}" if since else self._head
            out = self._git(
                self._repo, "log", "-1", "--format=%h", rev_range, "--", path,
            )
            return out or self._head[:7]
        except GitError:
            return self._head[:7]


def head_unchanged(
    repo: Path, head: str, *, run_git: Callable[..., str] = _default_git,
) -> bool:
    """CAS guard: True iff the repo HEAD still equals ``head``. Callers
    check this immediately before replacing the stored projection and
    discard the computed result on mismatch."""
    try:
        return run_git(repo, "rev-parse", "HEAD") == head
    except GitError:
        return False


def project_revalidations(
    repo: Path,
    entries: list[tuple[str, str]],
    *,
    run_git: Callable[..., str] = _default_git,
) -> ProjectionResult:
    """The full needs_revalidation projection for the pinned HEAD.

    Evidence-bearing qa (v3): stored working-tree blob hash vs blob at
    HEAD — exact, branch-agnostic, dirty-worktree correct. Legacy qa:
    timestamp-estimated base commit (documented approximation; such
    records can never anchor STRONG anyway). Conservative on semantic
    absence (no flag); INCOMPLETE on infrastructure errors (caller keeps
    the previous projection).
    """
    try:
        head = run_git(repo, "rev-parse", "HEAD")
    except GitError:
        # No repo / no commits / git unavailable — nothing to project
        # against; the caller keeps whatever projection it had.
        return ProjectionResult(rows=[], head=None, complete=False)

    view = _RepoView(repo, head, run_git)
    rows: list[tuple[str, str, str]] = []
    try:
        for chunk_id, content in entries:
            stored = _stored_anchor_hashes(content)
            if stored:
                for path, stored_blob in list(stored.items())[:_ANCHOR_TOP_N]:
                    head_blob = view.blob(head, path)
                    if head_blob == stored_blob:
                        continue
                    if head_blob is None:
                        # Renamed or deleted since — delete+add semantics.
                        rows.append((chunk_id, view.cause_commit(path), path))
                        break
                    if not view.blobs_differ_beyond_whitespace(stored_blob, head_blob):
                        continue
                    rows.append((chunk_id, view.cause_commit(path), path))
                    break
                continue

            # Legacy fallback: estimate the base from the qa timestamp.
            ts = _parse_timestamp(content)
            if ts is None:
                continue
            anchors = anchor_paths(content)
            if not anchors:
                continue
            base = view.base_commit(ts.isoformat())
            if base is None or base == head:
                continue
            for path in anchors:
                base_blob = view.blob(base, path)
                if base_blob is None:
                    continue  # anchor didn't exist at estimated base
                head_blob = view.blob(head, path)
                if head_blob == base_blob:
                    continue
                if head_blob is not None and not view.blobs_differ_beyond_whitespace(
                    base_blob, head_blob
                ):
                    continue
                rows.append((chunk_id, view.cause_commit(path, since=base), path))
                break
    except GitError as exc:
        logger.debug("revalidation projection incomplete: %s", exc)
        return ProjectionResult(rows=rows, head=head, complete=False)
    return ProjectionResult(rows=rows, head=head, complete=True)
