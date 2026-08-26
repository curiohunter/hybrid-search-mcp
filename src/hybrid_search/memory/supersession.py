"""Index-time qa supersession mapping — the R1 exposure fix.

The query-time supersession helpers (`_merge_memory_results`,
`_order_qa_by_recency`) only see the qa chunks that retrieval surfaced.
The ripgrep holdout R1 case fails *before* they run: the probe phrasing
matches the OBSOLETE answer verbatim, so the stale qa ranks #1 while the
correction never enters the candidate set at all — a topic group of one.

This module closes that gap at index time: it groups the WHOLE qa corpus
with the same calibrated matcher (`search.qa_topics`) and persists, for
every superseded qa chunk, the chunk_id of the newest answer on its
topic. The orchestrator then only needs a keyed lookup at query time to
splice the correction in next to the stale hit.

Timestamps come from the qa frontmatter (`timestamp:`), the same source
the recency ordering trusts — filesystem mtime lies after clones and
restores. A group whose members ALL lack a parseable timestamp produces
no mapping: a wrong "newest" would be a false supersession, and the
worse failure here is confidently replacing a fact with its stale twin.

Index-time grouping is STRICTER than the query-time matcher. Query-time
candidates are pre-filtered by the query itself, so `same_topic`'s
answer-only path (calibrated for cross-language pairs) is safe there.
Corpus-wide, long assistant answer excerpts about one project share
enough vocabulary that the answer-only path over-groups (2026-07-15
field check: "지금 해준것들이 뭔지" grouped with an unrelated turn).
Here a pair must ALSO agree on the question path; cross-language pairs
are deliberately missed — that is the ADV3 lane, and a conservative
miss just leaves the pre-fix behavior in place.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from hybrid_search.search import qa_topics

logger = logging.getLogger(__name__)

__all__ = ["compute_supersession"]

# Corpus-wide grouping is O(n^2) in the worst case. Real qa corpora are
# hundreds of entries; this cap only exists so a pathological corpus
# cannot stall a reindex. Entries beyond the cap (oldest first) are
# dropped from grouping — no silent wrong mapping, just no mapping.
_MAX_ENTRIES = 2000

_FRONTMATTER_LINE_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")


def _frontmatter_value(content: str, key: str) -> str | None:
    """Value of a top-level ``key:`` line inside the leading frontmatter."""
    if not content.startswith("---"):
        return None
    for line in content.split("\n", 200)[1:]:
        if line.startswith("---"):
            return None
        m = _FRONTMATTER_LINE_RE.match(line)
        if m and m.group(1) == key:
            return m.group(2).strip().strip('"').strip("'") or None
    return None


def _parse_timestamp(content: str) -> datetime | None:
    raw = _frontmatter_value(content, "timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _topic_item(content: str) -> tuple[dict[str, float], dict[str, float]]:
    """(question tokens, answer tokens) — mirrors the orchestrator's
    `_qa_topic_tokens` so index-time groups agree with query-time ones."""
    question = qa_topics.topic_tokens(_frontmatter_value(content, "query") or "")
    answer: dict[str, float] = {}
    if "## Answer excerpt" in content:
        excerpt = content.split("## Answer excerpt", 1)[1]
        excerpt = excerpt.split("## Top results", 1)[0]
        answer = qa_topics.topic_tokens(excerpt)
    return question, answer


def _is_machine_payload(content: str) -> bool:
    """Machine-generated queries (task notifications, hook payloads) are
    events, not facts — a "newest task notification" is never a
    correction of an older one."""
    query = _frontmatter_value(content, "query") or ""
    return query.startswith("<")


def _same_topic_strict(
    a: tuple[dict[str, float], dict[str, float]],
    b: tuple[dict[str, float], dict[str, float]],
) -> bool:
    """Corpus-wide predicate: the calibrated matcher PLUS mandatory
    question-path agreement (kills the answer-only over-grouping) PLUS
    two distinctive shared tokens on the QUESTIONS alone.

    The lenient matcher counts distinctive overlap on the question+answer
    union — right for retrieved candidates, wrong corpus-wide: short
    imperative turns ("진행해", "커밋 하고 푸시까지") share one command
    word, tiny token sets make the ratio bar trivial, and the answers
    supply the union's second distinctive token (2026-07-15 field check).
    Private qa_topics thresholds are reused on purpose — one calibration
    source (benchmarks/topic_gold_set.json), not a second set of magic
    numbers."""
    if not qa_topics.same_topic(a, b):
        return False
    if qa_topics._distinctive_shared_count(a[0], b[0]) < qa_topics._MIN_DISTINCTIVE_SHARED:
        return False
    return qa_topics.weighted_overlap(a[0], b[0]) >= qa_topics._QUERY_OVERLAP


def _strict_group_indices(
    items: list[tuple[dict[str, float], dict[str, float]]],
) -> list[list[int]]:
    """Complete-link grouping under the strict predicate (same shape as
    `qa_topics.topic_group_indices`, which hardcodes the lenient one)."""
    groups: list[list[int]] = []
    for i, item in enumerate(items):
        for group in groups:
            if all(_same_topic_strict(item, items[j]) for j in group):
                group.append(i)
                break
        else:
            groups.append([i])
    return groups


def compute_supersession(
    entries: list[tuple[str, str]],
) -> dict[str, str]:
    """``{superseded chunk_id: superseding chunk_id}`` over a qa corpus.

    ``entries`` is ``(chunk_id, content)`` for every qa_log chunk of one
    project. Grouping is the calibrated complete-link matcher; within a
    group the newest timestamp wins and every other member maps to it.
    """
    if len(entries) < 2:
        return {}

    dated: list[tuple[str, str, datetime | None]] = [
        (chunk_id, content, _parse_timestamp(content))
        for chunk_id, content in entries
        if not _is_machine_payload(content)
    ]
    if len(dated) < 2:
        return {}
    if len(dated) > _MAX_ENTRIES:
        # Keep the newest slice — stale-fact risk concentrates where new
        # answers exist to supersede old ones. Undated entries sort oldest.
        dated.sort(key=lambda e: (e[2] is not None, e[2] or datetime.min), reverse=True)
        logger.warning(
            "qa supersession: corpus has %d qa chunks; grouping only the "
            "newest %d", len(dated), _MAX_ENTRIES,
        )
        dated = dated[:_MAX_ENTRIES]

    # Newest-first input order seeds each complete-link group on the
    # newest member, matching the greedy tie-breaking the query-time
    # matcher documents.
    dated.sort(
        key=lambda e: ((e[2] is None), -(e[2].timestamp() if e[2] else 0.0), e[0])
    )
    items = [_topic_item(content) for _, content, _ in dated]

    mapping: dict[str, str] = {}
    for group in _strict_group_indices(items):
        if len(group) < 2:
            continue
        members = [dated[i] for i in group]
        with_ts = [m for m in members if m[2] is not None]
        if not with_ts:
            continue  # no trustworthy "newest" — refuse to guess
        newest = max(with_ts, key=lambda m: (m[2], m[0]))
        for chunk_id, _, _ in members:
            if chunk_id != newest[0]:
                mapping[chunk_id] = newest[0]
    return mapping
