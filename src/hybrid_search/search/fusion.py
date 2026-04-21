"""RRF (Reciprocal Rank Fusion) algorithm — §11 of design doc."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FusedResult:
    chunk_id: str
    rrf_score: float
    bm25_rank: int | None
    vector_rank: int | None
    authority: float | None = None


def _apply_authority_nudge(rrf: float, authority: float | None) -> float:
    """Bounded nudge: rrf * (0.5 + 0.5 * authority).

    Chunks without an authority signal (None) pass through unchanged. Chunks
    with a low-confidence incoming call edge (authority ~0.3) are damped but
    never zeroed — a weak signal still leaves them in the ranking. High
    confidence (1.0) neutralizes to a passthrough factor of 1.0.
    """
    if authority is None:
        return rrf
    return rrf * (0.5 + 0.5 * authority)


def reciprocal_rank_fusion(
    bm25_ids: list[str],
    vector_ids: list[str],
    k: int = 60,
    bm25_weight: float = 0.5,
    chunk_authority_scores: dict[str, float] | None = None,
) -> list[FusedResult]:
    """
    RRF Score = Σ (weight / (k + rank))

    k=60 is standard (Cormack et al. paper).

    If ``chunk_authority_scores`` is provided, the fused score is multiplied by
    ``0.5 + 0.5 * authority`` for chunks present in the map (M1 — numeric
    confidence injected from call graph). Absent chunks are treated as neutral.
    """
    scores: dict[str, float] = {}
    bm25_ranks: dict[str, int] = {}
    vector_ranks: dict[str, int] = {}
    vector_weight = 1.0 - bm25_weight

    for rank, chunk_id in enumerate(bm25_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + bm25_weight / (k + rank)
        bm25_ranks[chunk_id] = rank

    for rank, chunk_id in enumerate(vector_ids, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + vector_weight / (k + rank)
        vector_ranks[chunk_id] = rank

    # Apply authority nudge (M1)
    adjusted: dict[str, float] = {}
    authorities: dict[str, float | None] = {}
    for cid, raw in scores.items():
        auth = chunk_authority_scores.get(cid) if chunk_authority_scores else None
        authorities[cid] = auth
        adjusted[cid] = _apply_authority_nudge(raw, auth)

    # Sort by adjusted score descending
    ranked = sorted(adjusted.keys(), key=lambda cid: adjusted[cid], reverse=True)

    return [
        FusedResult(
            chunk_id=cid,
            rrf_score=adjusted[cid],
            bm25_rank=bm25_ranks.get(cid),
            vector_rank=vector_ranks.get(cid),
            authority=authorities.get(cid),
        )
        for cid in ranked
    ]
