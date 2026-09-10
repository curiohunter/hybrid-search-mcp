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

# Question overlap required when one side carries no answer excerpt.
# `qa_topics._QUERY_ONLY_OVERLAP` (0.6) is calibrated for query-time
# candidates, which the query itself has already filtered; corpus-wide the
# same bar admits far too much. Answer-less pairs in this corpus that
# clear 0.6 sit at a median 0.91, so the genuine ones are near-identical
# question text and survive this.
#
# Kept even after the project's own naming stopped carrying weight
# (`project_identity_tokens`, which removes the CAUSE of the case that
# surfaced this). Relaxing it back to 0.6 with that fix in place was
# measured: corpus acceptance 9.7 -> 10.5 per 10k, 36 more mappings. The
# root fix is not a superset of this bar — answer-less pairs run on
# question text alone, and other ubiquitous vocabulary exists.
_ANSWERLESS_QUERY_OVERLAP = 0.85

# A ratio needs something to be a ratio OF, and this bar is NOT APPLIED —
# it is recorded here because the defect it names is real and measured,
# and the next person should not have to rediscover either half.
#
# The defect (2026-09-10, dogfood corpus): over the 112 mappings the
# answer-less path produced, question mass on the lighter side has a
# median of 7.0 but a 10th percentile of 2.0, and 29 of them — a quarter
# — sit below 4.0. A question of two tokens clears any ratio trivially:
# sharing 2 of 2 is "100% overlap" and no evidence at all. One mapping
# paired ['가장','좋겠'] with ['가장','좋겠'] at 1.00.
#
# Why it is not applied: refusing those pairs improves the destructive
# metric (map-caused displacement 12% -> 10%, benchmarks/
# displacement_audit.py) and leaves Set A and the code axis untouched —
# but Set B drops 0.05 -> 0.02, and Set B is a floor constraint in this
# line's objective function. 3.0 was tried too; same drop, so the lost
# exposure is not coming from the thinnest pairs. The splice is both a
# correction and an exposure path, and cutting mappings cuts both.
#
# What would settle it: label the displacement cases (legitimate
# correction vs damage) so the two signals can be compared as accuracy
# rather than as two rates pointing opposite ways. See
# docs/plans/2026-09-10-displacement-audit.md §6.
_MIN_QUESTION_MASS = 4.0

# Index-time supersession needs a HIGHER question bar than query-time
# grouping, and this is the number 16 hand-labelled displacements produced
# (~/.hybrid-search/benchmarks/valuein_displacement_labels.json, keyed by
# displaced chunk — the corpus's own turns, so the labels stay outside the
# repo).
#
# Every case was read in full and labelled by one question: does the
# successor answer substantially the same question as the record it
# replaced? 8 did (legitimate), 8 did not (damage) — the map's
# displacement precision was 50%. Sorting by question overlap separates
# them better than anything else measured:
#
#   damage      q_ov: 1.00 0.87 0.87 0.60 0.50 0.40 0.39 0.38
#   legitimate  q_ov: 1.00 1.00 1.00 1.00 1.00 1.00 0.52 0.43
#
# Six of eight legitimate displacements are near-verbatim repeats of the
# same question. Requiring 0.70 refuses 5 of 8 damage cases and costs 2
# legitimate ones; the answer-less mass bar above catches 2 more damage
# for 1 more legitimate. `qa_topics._active_thresholds()[0]` (0.30 on the
# prefix backend) is calibrated for query-time candidates the query has
# already filtered — corpus-wide it is far too generous.
_SUPERSESSION_QUERY_OVERLAP = 0.70

# Containment is not similarity. `qa_topics.weighted_overlap` divides the
# shared weight by the LIGHTER side, which is right at query time — a
# short query matching part of a long record is a hit. Corpus-wide it
# reads a short question fully contained in a long pasted block as 1.00
# while the block shares almost nothing with it, and pasted blocks are
# everywhere in a qa corpus (people quote whole exchanges back).
#
# That artifact deleted the answer to a gold question: a turn whose query
# was a pasted transcript scored 1.00 against an unrelated one-line
# question and was replaced by it. Requiring the overlap to cover a share
# of the HEAVIER side too is symmetric, so containment alone cannot pass.
#
# Calibrated on 21 hand-labelled displacements
# (~/.hybrid-search/benchmarks/valuein_displacement_labels.json). Of the
# pairs the other rules still let through:
#
#   damage      symmetric overlap: 0.07 0.21 0.29 0.29
#   legitimate  symmetric overlap: 0.19 0.36 0.38 0.50 0.92 1.00 1.00 1.00 1.00
#
# 0.33 sits in the gap: it refuses all four remaining damage cases and
# costs one legitimate one.
_MIN_SYMMETRIC_QUESTION_OVERLAP = 0.33

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


def _topic_item(
    content: str, demote: frozenset[str] = frozenset()
) -> tuple[dict[str, float], dict[str, float]]:
    """(question tokens, answer tokens) — mirrors the orchestrator's
    `_qa_topic_tokens` so index-time groups agree with query-time ones.

    ``demote`` is the project's own naming (see
    `qa_topics.project_identity_tokens`); both sides must pass the same
    set or the two groupings drift apart."""
    question = qa_topics.topic_tokens(
        _frontmatter_value(content, "query") or "", demote=demote
    )
    answer: dict[str, float] = {}
    if "## Answer excerpt" in content:
        excerpt = content.split("## Answer excerpt", 1)[1]
        excerpt = excerpt.split("## Top results", 1)[0]
        answer = qa_topics.topic_tokens(excerpt, demote=demote)
    return question, answer


def _is_machine_payload(content: str) -> bool:
    """Machine-generated queries (task notifications, hook payloads) are
    events, not facts — a "newest task notification" is never a
    correction of an older one."""
    query = _frontmatter_value(content, "query") or ""
    return query.startswith("<")


def _symmetric_question_overlap(
    a: dict[str, float], b: dict[str, float]
) -> float:
    """Shared question weight over the HEAVIER side (0..1).

    The mirror of `qa_topics.weighted_overlap`, which uses the lighter
    side. Both together mean "each question is largely the other" rather
    than "one contains the other" — see `_MIN_SYMMETRIC_QUESTION_OVERLAP`.
    """
    if not a or not b:
        return 0.0
    shared = sum(min(a[t], b[t]) for t in a.keys() & b.keys())
    denom = max(sum(a.values()), sum(b.values()))
    return shared / denom if denom else 0.0


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
    # When either side has no parseable answer excerpt, `same_topic` falls
    # back to question overlap ALONE (_QUERY_ONLY_OVERLAP = 0.6). Corpus-
    # wide that is far too little: two turns whose questions share only the
    # project's own name clear it easily, because the name sits in every
    # shell prompt and every worktree path in its own corpus.
    #
    # Measured 2026-09-09 on the dogfood corpus: 305 of 2,017 qa records
    # (15%) carry no excerpt, and they were involved in 179 of 396
    # mappings — 45%. One evicted the answer to a gold question outright,
    # because the splice REPLACES a stale hit at full capacity. None of
    # that is the topic matcher's doing; it predates it, and a gold set
    # whose fixtures all have answers can never see it.
    #
    # These pairs are not banned, because a BARE turn log superseded by
    # the consolidated note built from it is the design — that mapping is
    # how the note reaches someone who hit the raw turn. The bar is raised
    # instead: with no answer to corroborate, the questions must be nearly
    # the same text, not merely about the same project.
    if not (a[1] and b[1]):
        if min(sum(a[0].values()), sum(b[0].values())) < _MIN_QUESTION_MASS:
            return False
        return (
            qa_topics._distinctive_shared_count(a[0], b[0])
            >= qa_topics._MIN_DISTINCTIVE_SHARED
            and qa_topics.weighted_overlap(a[0], b[0]) >= _ANSWERLESS_QUERY_OVERLAP
        )
    if not qa_topics.same_topic(a, b):
        return False
    if qa_topics._distinctive_shared_count(a[0], b[0]) < qa_topics._MIN_DISTINCTIVE_SHARED:
        return False
    query_thr = max(qa_topics._active_thresholds()[0], _SUPERSESSION_QUERY_OVERLAP)
    if qa_topics.weighted_overlap(a[0], b[0]) < query_thr:
        return False
    return _symmetric_question_overlap(a[0], b[0]) >= _MIN_SYMMETRIC_QUESTION_OVERLAP


def _strict_group_indices(
    items: list[tuple[dict[str, float], dict[str, float]]],
) -> list[list[int]]:
    """Complete-link grouping under the strict predicate (same shape as
    `qa_topics.topic_group_indices`, which hardcodes the lenient one).

    Used by the Reflector, which genuinely needs CLUSTERS — a
    consolidation note is written from a set of records that belong
    together. `compute_supersession` deliberately does NOT use it: it
    needs one successor per record, and deriving that from group
    membership made the map non-monotonic in the predicate (see the
    comment in `compute_supersession`).
    """
    groups: list[list[int]] = []
    for i, item in enumerate(items):
        for group in groups:
            if all(_same_topic_strict(item, items[j]) for j in group):
                group.append(i)
                break
        else:
            groups.append([i])
    return groups


def _is_consolidation(content: str) -> bool:
    """True for a Reflector note — a synthesis of many records."""
    return (_frontmatter_value(content, "memory_type") or "") == "consolidated"


def compute_supersession(
    entries: list[tuple[str, str]],
    *,
    project_name: str | None = None,
) -> dict[str, str]:
    """``{superseded chunk_id: superseding chunk_id}`` over a qa corpus.

    ``entries`` is ``(chunk_id, content)`` for every qa_log chunk of one
    project. Grouping is the calibrated complete-link matcher; within a
    group the newest timestamp wins and every other member maps to it.

    ``project_name`` demotes the corpus's own naming to low-information
    (`qa_topics.project_identity_tokens`). Corpus-wide it is the single
    most over-weighted token there is — every shell prompt and every
    quoted path carries it — and grouping on it alone is what deleted a
    gold question's answer on 2026-09-09.
    """
    demote = qa_topics.project_identity_tokens(project_name)
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
    items = [_topic_item(content, demote) for _, content, _ in dated]

    # For each record, the NEWEST record that is the same topic as THAT
    # record — decided pairwise, not by group membership.
    #
    # Group membership used to decide it (greedy complete-link, newest
    # first), and that made the whole map non-monotonic in the predicate:
    # removing one edge splits a group, and a record that had been its
    # group's winner can land in a different group and acquire a
    # successor it never pairwise matched. Measured 2026-09-10 — making
    # the predicate STRICTER produced 35 brand-new mappings and
    # retargeted 33, and one of the new ones deleted a gold question's
    # answer. Tightening a threshold could therefore create damage, which
    # is why every threshold move in this line kept costing the same
    # question no matter which direction it went.
    #
    # Pairwise is monotonic (a stricter predicate can only take a
    # successor away, never invent one), needs no anti-chaining rule (A
    # only ever maps to something A itself matched), and answers the
    # question the splice actually asks: "for THIS stale hit, what is the
    # current answer on its topic?" Scanning newest-first and stopping at
    # the first match keeps it cheap despite being O(n^2) in the worst
    # case.
    mapping: dict[str, str] = {}
    for i, (chunk_id, content, _ts) in enumerate(dated):
        if _is_consolidation(content):
            # A consolidation is not an older version of anything. It
            # synthesises its own cluster of records, so two notes on
            # adjacent topics are two answers, not an answer and its
            # update — and the topic matcher pairs them readily because a
            # Reflector run writes many notes on one day about one area.
            # Superseding one by the other is destructive: at full
            # capacity the splice REPLACES the stale hit, so the note that
            # actually answered the question leaves the results
            # (2026-09-09). Notes still supersede the raw logs they were
            # built from — that direction is the design.
            continue
        # `dated` is newest-first (undated last), so everything before i
        # is newer than i. Which candidates are eligible depends on
        # whether i has an answer of its own:
        #
        #   · i HAS an answer — only a NEWER answer may replace it. That
        #     is supersession proper: a fact restated later.
        #   · i has NO answer — any answered record on its topic may take
        #     its place, older included. That is not supersession by time
        #     but the exposure path the design wants: someone who hits a
        #     bare turn should be handed the record that answers it, and
        #     the labelled cases confirm it (3 of 8 legitimate
        #     displacements were exactly this shape).
        #
        # Either way the FIRST match wins, which is the newest one.
        candidates = range(i) if items[i][1] else range(len(dated))
        for j in candidates:
            if j == i:
                continue
            # The winner must carry an answer: a record with no excerpt
            # has nothing to correct anything WITH, and the splice would
            # replace a real answer with an empty one.
            if dated[j][2] is None or not items[j][1]:
                continue
            if _same_topic_strict(items[i], items[j]):
                mapping[chunk_id] = dated[j][0]
                break
    return mapping
