"""Commit-aware memory invalidation (P1-2).

A qa answer is written against the code as it was at answer time. When a
later commit changes the files that answer was grounded in, the memory
is not known-wrong — but it is no longer *verified against current
code*. Conversational memory systems can't see this at all; this project
indexes commits and reindexes on post-commit, so the link is one pass:

    reindex (post-commit) → changed paths of HEAD
        → every older qa anchored to a changed path
        → flag ``needs_revalidation`` (cause: the commit)

Flags live in the ``qa_revalidation`` side table, NOT in the qa file:
rewriting frontmatter would change the content hash and force a
re-embedding of every flagged memory on the next reindex. The
orchestrator reads the table at enrich time, surfaces the flag in
``trust_meta`` (`needs_revalidation since <sha>`), decays the score
(0.6×), and blocks the memory from anchoring a STRONG claim — same
quarantine lane as P1-1's ``inferred``.

The flag clears itself the natural way: a newer qa on the same topic
supersedes the stale one (R1 machinery), or the row is dropped when the
qa chunk disappears from the store.
"""

from __future__ import annotations

from datetime import datetime

from hybrid_search.memory.integrity import _extract_result_paths
from hybrid_search.memory.supersession import _parse_timestamp

__all__ = ["anchor_paths", "compute_revalidations"]

# A qa's anchors are the TOP results it was answered from. Deeper ranks
# are incidental co-retrievals — anchoring on all ten would invalidate
# half the corpus every time a hot file changes.
_ANCHOR_TOP_N = 3


def anchor_paths(content: str) -> list[str]:
    """The first N distinct result paths of a qa record."""
    seen: list[str] = []
    for path in _extract_result_paths(content or ""):
        if path not in seen:
            seen.append(path)
        if len(seen) >= _ANCHOR_TOP_N:
            break
    return seen


def compute_revalidations(
    entries: list[tuple[str, str]],
    changed_paths: set[str],
    *,
    cause_commit: str,
    commit_time: datetime | None,
) -> list[tuple[str, str, str]]:
    """``(chunk_id, cause_commit, changed_path)`` rows to flag.

    ``entries`` is ``(chunk_id, content)`` for the project's qa chunks.
    A qa written AFTER the commit already saw the new code — flagging it
    would invalidate fresh memories on every reindex — so records with a
    timestamp at or after ``commit_time`` are skipped. Records without a
    parseable timestamp are flagged conservatively only when
    ``commit_time`` is unknown too; otherwise they are skipped (no
    evidence of being stale beats a guessed flag).
    """
    if not changed_paths:
        return []
    rows: list[tuple[str, str, str]] = []
    for chunk_id, content in entries:
        anchors = anchor_paths(content)
        hit = next((p for p in anchors if p in changed_paths), None)
        if hit is None:
            continue
        ts = _parse_timestamp(content)
        if commit_time is not None:
            if ts is None or ts >= commit_time:
                continue
        rows.append((chunk_id, cause_commit, hit))
    return rows
