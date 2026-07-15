"""Commit-aware memory invalidation — current-HEAD projection (P1-2, v2).

A qa answer is written against the code as it was at answer time. When
the code its anchors point at differs NOW, the memory is not
known-wrong — but it is no longer verified against current code.

v1 (round-1) accumulated flags by walking commits with a cursor. Round-2
review showed that accumulation cannot satisfy the lifecycle contract:
a full rebuild wipes the table and the cursor (silently un-flagging
everything), branch checkouts leak flags across branches, reverts never
clear flags, and a single failed commit in the walk is skipped forever.

v2 states the invariant directly: **the flag set is a pure function of
(current HEAD, qa corpus)** — recomputed from scratch on every pass.

    flagged(qa, path) ⇔ blob(path @ base(qa.timestamp)) != blob(path @ HEAD)
                         and the difference is not whitespace-only

where base(ts) = the last commit on HEAD's history at or before the qa
timestamp. Consequences, by construction:

- full rebuild: table starts empty, recompute restores exactly the
  flags that still hold — nothing survives by accident, nothing is lost.
- checkout A→B→A: HEAD defines truth; B's flags cannot leak into A.
- revert: blob equality is restored, the flag disappears.
- no cursor, no per-commit walk → no permanent skips, no cursor
  regression under concurrency. Concurrent passes each write one
  atomic, internally-consistent projection; last writer wins and the
  next pass converges (store-level serialization is out of scope here
  and documented in the spec).
- rename/delete: the path is absent at HEAD → flagged (delete+add
  semantics, per review).
- whitespace-only edits: excluded via ``git diff -w`` (broader
  format-only rewrites are an accepted limitation, documented).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from hybrid_search.memory.integrity import _extract_result_paths
from hybrid_search.memory.supersession import _parse_timestamp

logger = logging.getLogger(__name__)

__all__ = ["anchor_paths", "project_revalidations"]

# A qa's anchors are the TOP results it was answered from. Deeper ranks
# are incidental co-retrievals — anchoring on all ten would invalidate
# half the corpus every time a hot file changes.
_ANCHOR_TOP_N = 3

_GIT_TIMEOUT_S = 10


def anchor_paths(content: str) -> list[str]:
    """The first N distinct result paths of a qa record."""
    seen: list[str] = []
    for path in _extract_result_paths(content or ""):
        if path not in seen:
            seen.append(path)
        if len(seen) >= _ANCHOR_TOP_N:
            break
    return seen


def _default_git(repo: Path, *argv: str) -> str | None:
    """stdout of a git command, or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", *argv], cwd=repo, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


class _RepoView:
    """Memoised git lookups for one projection pass."""

    def __init__(self, repo: Path, run_git: Callable[..., str | None]) -> None:
        self._repo = repo
        self._git = run_git
        self._base_by_ts: dict[str, str | None] = {}
        self._blob: dict[tuple[str, str], str | None] = {}

    def base_commit(self, ts: datetime) -> str | None:
        """Last commit on HEAD's history at or before ``ts``."""
        key = ts.isoformat()
        if key not in self._base_by_ts:
            self._base_by_ts[key] = self._git(
                self._repo, "rev-list", "-1", f"--before={key}", "HEAD",
            ) or None
        return self._base_by_ts[key]

    def blob(self, commit: str, path: str) -> str | None:
        """Blob sha of ``path`` at ``commit``; None when absent."""
        key = (commit, path)
        if key not in self._blob:
            self._blob[key] = self._git(
                self._repo, "rev-parse", f"{commit}:{path}",
            ) or None
        return self._blob[key]

    def whitespace_only_change(self, base: str, path: str) -> bool:
        """True when base..HEAD touches ``path`` only in whitespace
        (including blank-line churn). NOTE: ``--name-only`` ignores the
        whitespace flags (tree-level), so the actual patch output must
        be inspected — empty patch under -w while blobs differ means
        cosmetic."""
        out = self._git(
            self._repo, "diff", "-w", "--ignore-blank-lines",
            base, "HEAD", "--", path,
        )
        return out == ""

    def cause_commit(self, base: str, path: str) -> str | None:
        out = self._git(
            self._repo, "log", "-1", "--format=%h", f"{base}..HEAD", "--", path,
        )
        return out or None

    def head(self) -> str | None:
        return self._git(self._repo, "rev-parse", "HEAD") or None


def project_revalidations(
    repo: Path,
    entries: list[tuple[str, str]],
    *,
    run_git: Callable[..., str | None] = _default_git,
) -> tuple[list[tuple[str, str, str]], str | None]:
    """``([(chunk_id, cause_commit, changed_path)], head)`` — the full
    flag projection for the CURRENT HEAD, computed from scratch.

    ``entries`` is ``(chunk_id, content)`` for the project's qa chunks.
    Conservative on every unknown: no timestamp, no base commit, or a
    path absent at base ⇒ no flag (a guessed flag silently demotes a
    memory; a missed flag is caught by the trust contract's other
    layers). Returns head=None when the repo has no commits — callers
    must then leave the previous projection untouched rather than
    replacing it with an empty one.
    """
    view = _RepoView(repo, run_git)
    head = view.head()
    if head is None:
        return [], None

    rows: list[tuple[str, str, str]] = []
    for chunk_id, content in entries:
        ts = _parse_timestamp(content)
        if ts is None:
            continue
        anchors = anchor_paths(content)
        if not anchors:
            continue
        base = view.base_commit(ts)
        if base is None or base == head:
            continue
        for path in anchors:
            base_blob = view.blob(base, path)
            if base_blob is None:
                continue  # anchor didn't exist at qa time — unreliable
            head_blob = view.blob(head, path)
            if head_blob == base_blob:
                continue
            if head_blob is None:
                # Renamed or deleted since — delete+add semantics: the
                # anchored content is gone from HEAD.
                cause = view.cause_commit(base, path) or head[:7]
                rows.append((chunk_id, cause, path))
                break
            if view.whitespace_only_change(base, path):
                continue
            cause = view.cause_commit(base, path) or head[:7]
            rows.append((chunk_id, cause, path))
            break
    return rows, head
