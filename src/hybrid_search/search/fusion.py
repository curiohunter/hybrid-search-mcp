"""RRF (Reciprocal Rank Fusion) algorithm — §11 of design doc."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FusedResult:
    chunk_id: str
    rrf_score: float
    bm25_rank: int | None
    vector_rank: int | None


def reciprocal_rank_fusion(
    bm25_ids: list[str],
    vector_ids: list[str],
    k: int = 60,
    bm25_weight: float = 0.5,
) -> list[FusedResult]:
    """
    RRF Score = Σ (weight / (k + rank))

    k=60 is standard (Cormack et al. paper).
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

    # Sort by fused score descending
    ranked = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)

    return [
        FusedResult(
            chunk_id=cid,
            rrf_score=scores[cid],
            bm25_rank=bm25_ranks.get(cid),
            vector_rank=vector_ranks.get(cid),
        )
        for cid in ranked
    ]
