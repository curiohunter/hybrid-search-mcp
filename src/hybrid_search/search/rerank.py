"""Lexical second-stage rerank over the fused candidate list.

RRF fuses *ranks*, so a chunk that is mediocre in both retrievers can
outrank one that is excellent in exactly the dimension the user asked
about — the top of the list ends up topically adjacent rather than
answering. This stage re-scores the head of the fused list by how much
of the query it actually covers.

Deliberately deterministic and model-free: no API call (the pre-fetch
hook path has a ~400 ms budget), no local ML (bulk local inference was
abandoned after it pinned an M3 — see docs/why.md). Coverage uses
substring containment, not token equality, because Korean agglutination
("배치" vs "배치는") breaks set membership.
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING

from hybrid_search.memory.quality import query_tokens

if TYPE_CHECKING:  # pragma: no cover — import cycle guard, typing only
    from hybrid_search.search.orchestrator import HybridResult


def _coverage(tokens: set[str], haystack: str) -> float:
    """Fraction of query tokens present (substring) in the haystack."""
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in haystack)
    return hits / len(tokens)


def lexical_rerank(
    query: str,
    results: list["HybridResult"],
    *,
    top_n: int = 20,
    weight: float = 0.6,
) -> list["HybridResult"]:
    """Re-score the top ``top_n`` results by query-term coverage.

    new_score = rrf_score * (1 + weight * coverage), coverage in [0, 1].
    Only the head window is re-sorted; the tail keeps its fused order, so
    the stage can never pull a deep low-signal chunk into the top on
    coverage alone.
    """
    if weight <= 0 or len(results) < 2:
        return results
    tokens = query_tokens(query)
    if not tokens:
        return results
    head = results[:top_n]
    adjusted: list[tuple[float, int, HybridResult]] = []
    for i, r in enumerate(head):
        haystack = " ".join(
            part for part in (r.name, r.file_path, r.snippet, r.content) if part
        ).casefold()
        cov = _coverage(tokens, haystack)
        adjusted.append((r.rrf_score * (1.0 + weight * cov), i, r))
    # Stable on the original order via the index tiebreak.
    adjusted.sort(key=lambda t: (-t[0], t[1]))

    # Score-preserving permutation: the coverage-adjusted value decides the
    # ORDER, but each slot keeps the fused score that lived at that rank.
    # The confidence contract's thresholds are calibrated on the raw RRF
    # score distribution — letting a x1.6 coverage multiplier leak into
    # top_score/score_gap made absent-topic junk read as "strong"
    # (bench v2 abstention 78% → 22% before this guard).
    slot_scores = sorted((r.rrf_score for r in head), reverse=True)
    reordered = [
        _dc_replace(r, rrf_score=score)
        for score, (_, _, r) in zip(slot_scores, adjusted)
    ]
    return reordered + results[top_n:]
