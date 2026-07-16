"""Commit-aware memory invalidation — evidence-based HEAD projection (P1-2, v4).

A qa answer is written against the code as it was at answer time. When
the code its anchors point at differs NOW, the memory is no longer
verified against current code.

v2 recomputed flags from (HEAD, qa timestamp): "the code the qa saw" was
ESTIMATED as the last commit before the qa timestamp. Round-2 re-review
showed the estimate breaks on the most common agent workflow — edit
(uncommitted) → ask → commit later: the qa saw the NEW code, but the
timestamp estimate resolves to the OLD commit and false-flags it. It is
also wrong for qa written on another branch.

v3 captured the WORKING TREE at write time — round-2 re-review showed
that is still not "what the qa saw": the index (and thus the search
results the answer was grounded in) can lag the working tree. v4 states
provenance exactly: **the anchor evidence is the index content hash the
search results themselves carried** (``HybridResult.indexed_file_hash``,
= ``files.file_hash`` for indexed chunks, live index hash for in-flight
overlay results):

    anchor_hash_algo: index
    anchor_hashes: {path: scanner.compute_content_hash of what was served}

The projection compares those against the content at the pinned HEAD
using the same hash function. Evidence capture makes ZERO git calls on
the search hot path — the hashes ride in on the results. Legacy records
(no evidence, or an unrecognised algo) keep the timestamp estimate as a
fallback; estimation-derived state is inherently weaker, which the trust
contract already reflects (legacy records can never anchor STRONG
regardless).

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
    "head_unchanged",
    "is_source_anchor",
    "project_revalidations",
    "replace_projection_guarded",
]

# Anchor identity boundaries (round-2 final P0): only SOURCE-GROUNDED
# results may anchor git revalidation. Memory/synthetic lanes live at
# virtual paths that are not in git HEAD — anchoring on them makes every
# recall-of-a-recall flag itself as renamed/deleted on the next reindex.
_NON_SOURCE_NODE_TYPES = frozenset({
    "qa_log", "memory_card", "domain_term", "episodic_example",
    "commit", "conv_turn", "module", "module_member", "graph_card",
})
_VIRTUAL_PATH_PREFIXES = (".hybrid-search/", ".conversations/", ".git-history/")


def is_source_anchor(node_type: str | None, file_path: str | None) -> bool:
    """True when a search result is grounded in an actual source/doc
    file (indexed chunk or in-flight overlay) — the only results whose
    content can be meaningfully re-checked against a git HEAD."""
    if (node_type or "") in _NON_SOURCE_NODE_TYPES:
        return False
    path = file_path or ""
    if not path:
        return False
    return not any(path.startswith(p) for p in _VIRTUAL_PATH_PREFIXES)

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


def _default_git_bytes(repo: Path, *argv: str) -> bytes:
    """Raw stdout bytes of a git command; raises GitError on failure.
    Needed for ``cat-file`` — content hashing must see exact bytes."""
    try:
        proc = subprocess.run(
            ["git", *argv], cwd=repo, capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception as exc:
        raise GitError(f"git {argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(
            f"git {argv[0]} rc={proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )
    return proc.stdout


def _stored_anchor_hashes(content: str) -> dict[str, tuple[str, str | None]] | None:
    """``{path: (hash, project | None)}``, only when the algo marker
    matches what we know how to compare. Unknown/missing algo → legacy
    fallback. Values are either plain hash strings (early records, own
    project implied) or ``{"h": hash, "p": project}`` objects."""
    if (_frontmatter_value(content, "anchor_hash_algo") or "") != "index":
        return None
    raw = _frontmatter_value(content, "anchor_hashes")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    parsed: dict[str, tuple[str, str | None]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            h = value.get("h")
            if not h:
                continue
            parsed[str(key)] = (str(h), str(value["p"]) if value.get("p") else None)
        else:
            parsed[str(key)] = (str(value), None)
    return parsed or None


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
    """Memoised git lookups pinned to one HEAD SHA for one pass.

    Scale posture (round-3): the pass runs on every reindex, so git
    subprocess count must not grow with the qa corpus. Two amortised
    lookups keep it O(1)-ish in corpus size:

    - the full commit log (one ``git log`` call) + bisect answers every
      timestamp→base-commit query — v4's per-timestamp ``rev-list``
      was one subprocess per distinct qa timestamp.
    - the full HEAD tree (one ``git ls-tree -r`` call) answers every
      blob-at-HEAD query; only non-HEAD commits (legacy base lookups)
      pay a targeted ls-tree.
    """

    def __init__(
        self,
        repo: Path,
        head: str,
        run_git: Callable[..., str],
        run_git_bytes: Callable[..., bytes] = _default_git_bytes,
    ) -> None:
        self._repo = repo
        self._head = head
        self._git = run_git
        self._git_bytes = run_git_bytes
        self._blob: dict[tuple[str, str], str | None] = {}
        self._content_hash: dict[tuple[str, str], str | None] = {}
        self._log: list[tuple[str, str]] | None = None  # (iso_ts, sha) ascending
        self._head_tree: dict[str, str] | None = None

    def _commit_log(self) -> list[tuple[str, str]]:
        if self._log is None:
            out = self._git(
                self._repo, "log", "--format=%cI %H", "--reverse", self._head,
            )
            log: list[tuple[str, str]] = []
            for line in out.splitlines():
                parts = line.strip().split(" ", 1)
                if len(parts) == 2:
                    log.append((parts[0], parts[1]))
            self._log = log
        return self._log

    def base_commit(self, ts_iso: str) -> str | None:
        """Last commit on the pinned HEAD's history at or before ts.
        None = repo history starts later (absence, not error)."""
        import bisect
        from datetime import datetime as _dt, timezone as _tz

        def _aware(value: _dt) -> _dt:
            # git %cI always carries an offset; a naive qa timestamp is
            # treated as UTC (same convention as the recency decay).
            return value if value.tzinfo else value.replace(tzinfo=_tz.utc)

        try:
            ts = _aware(_dt.fromisoformat(ts_iso))
        except ValueError:
            return None
        log = self._commit_log()
        keys: list[_dt] = []
        for iso, _ in log:
            try:
                keys.append(_aware(_dt.fromisoformat(iso)))
            except ValueError:
                return None  # unparseable commit date — treat as absent
        idx = bisect.bisect_right(keys, ts) - 1
        return log[idx][1] if idx >= 0 else None

    def _head_tree_blobs(self) -> dict[str, str]:
        if self._head_tree is None:
            out = self._git(self._repo, "ls-tree", "-r", self._head)
            tree: dict[str, str] = {}
            for line in out.splitlines():
                # "<mode> blob <sha>\t<path>"
                meta, _, path = line.partition("\t")
                fields = meta.split()
                if len(fields) >= 3 and fields[1] == "blob" and path:
                    tree[path] = fields[2]
            self._head_tree = tree
        return self._head_tree

    def blob(self, commit: str, path: str) -> str | None:
        """Blob sha of ``path`` at ``commit``; None when the path does
        not exist there (clean absence)."""
        if commit == self._head:
            return self._head_tree_blobs().get(path)
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

    def content_hash(self, commit: str, path: str) -> str | None:
        """Index-equivalent content hash of ``path`` at ``commit``;
        None when the path does not exist there. Uses the same function
        the scanner stores (compute_content_hash), so it compares 1:1
        with the evidence hashes the search results carried."""
        key = (commit, path)
        if key not in self._content_hash:
            if self.blob(commit, path) is None:
                self._content_hash[key] = None
            else:
                from hybrid_search.index.scanner import compute_content_hash

                raw = self._git_bytes(
                    self._repo, "cat-file", "blob", f"{commit}:{path}",
                )
                self._content_hash[key] = compute_content_hash(
                    raw, is_markdown=path.lower().endswith(".md"),
                )
        return self._content_hash[key]

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


class _HeadMoved(Exception):
    """Raised inside the replace transaction to force a rollback when
    HEAD moved between projection and commit."""


def replace_projection_guarded(
    db,
    project_id: str,
    result: ProjectionResult,
    repo: Path,
    *,
    run_git: Callable[..., str] = _default_git,
) -> bool:
    """Atomically replace the stored projection, guarded by HEAD checks
    INSIDE the transaction — immediately before and after the write
    (round-2 re-review: check-then-write outside the transaction left a
    window where a stale projection could land after a checkout). A
    post-write mismatch raises and ROLLS BACK, so a projection for the
    wrong HEAD is never committed. Returns True when the replace stuck.
    """
    assert result.head is not None and result.complete
    try:
        with db.transaction() as conn:
            if not head_unchanged(repo, result.head, run_git=run_git):
                raise _HeadMoved("pre-write")
            db.replace_qa_revalidation(
                conn, project_id, result.rows, projection_head=result.head,
            )
            if not head_unchanged(repo, result.head, run_git=run_git):
                raise _HeadMoved("post-write")
    except _HeadMoved as exc:
        logger.debug("projection replace discarded (HEAD moved %s)", exc)
        return False
    return True


def project_revalidations(
    repo: Path,
    entries: list[tuple[str, str]],
    *,
    project: str | None = None,
    run_git: Callable[..., str] = _default_git,
) -> ProjectionResult:
    """The full needs_revalidation projection for the pinned HEAD.

    Evidence-bearing qa (v4): the index content hash the search results
    carried (result provenance) vs the content hash at the pinned HEAD —
    exact, branch-agnostic, dirty-worktree correct. Virtual anchors
    (memory-lane paths) are filtered even in OLD evidence; anchors from
    another project are not locally revalidatable and are skipped.
    Legacy qa (no evidence): timestamp-estimated base commit (documented
    approximation; such records can never anchor STRONG anyway).
    Conservative on semantic absence (no flag); INCOMPLETE on
    infrastructure errors (caller keeps the previous projection).
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
                # Filter BEFORE the top-N slice: in records written before
                # the writer-side boundary, virtual anchors may occupy the
                # leading slots and would otherwise shadow a real source
                # anchor behind them.
                source_anchors = [
                    (path, value) for path, value in stored.items()
                    if is_source_anchor(None, path)
                    # Node types weren't stored in old evidence, so the
                    # virtual-prefix filter is the strongest recoverable
                    # boundary: those paths are never in HEAD and must
                    # not read as renamed/deleted (round-2 follow-up).
                ]
                for path, (stored_hash, anchor_project) in source_anchors[
                    :_ANCHOR_TOP_N
                ]:
                    if (
                        project is not None
                        and anchor_project is not None
                        and anchor_project != project
                    ):
                        # Cross-project anchor: its content lives in a
                        # different repo — comparing it against THIS
                        # project's HEAD would be meaningless
                        # (not_locally_revalidatable, round-2 final P0).
                        continue
                    head_hash = view.content_hash(head, path)
                    if head_hash == stored_hash:
                        continue
                    if head_hash is None:
                        # Renamed or deleted since — delete+add semantics.
                        rows.append((chunk_id, view.cause_commit(path), path))
                        break
                    # Whitespace refinement is unavailable here: only the
                    # hash of the served content survives, not its bytes.
                    # Over-flags a whitespace-only rewrite; never hides a
                    # real one.
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
