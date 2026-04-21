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


# M1.v2: boost-only nudge. The original damping-only formula
# ``rrf * (0.5 + 0.5*auth)`` penalised chunks with weak incoming edges,
# which hurt keyword/exact-symbol queries the most (mini-PoC 2026-04-21
# measured keyword Δ NDCG@10 = -0.049). Switching to a pure boost leaves
# auth-less chunks at baseline and only rewards the highest-confidence
# callees. α controls the ceiling: auth=1.0 → factor 1+α, auth=0 → 1.0.
#
# L6 n=60 (2026-04-21): α=0.3 best for self-contained, α=0.5 best for
# external workloads. Configurable via SearchConfig.authority_alpha.
DEFAULT_AUTHORITY_ALPHA = 0.3


def _apply_authority_nudge(
    rrf: float, authority: float | None, alpha: float = DEFAULT_AUTHORITY_ALPHA,
) -> float:
    """Boost-only nudge: rrf * (1.0 + α * authority).

    Chunks without an authority signal (None) pass through unchanged. Chunks
    with any authority signal are boosted — never damped — with the boost
    proportional to call-edge confidence. A low-confidence incoming edge
    (authority=0.3) yields a modest factor (~1.09), a high-confidence edge
    (authority=1.0) yields the full ceiling (1+α).
    """
    if authority is None:
        return rrf
    return rrf * (1.0 + alpha * authority)


def reciprocal_rank_fusion(
    bm25_ids: list[str],
    vector_ids: list[str],
    k: int = 60,
    bm25_weight: float = 0.5,
    chunk_authority_scores: dict[str, float] | None = None,
    authority_alpha: float = DEFAULT_AUTHORITY_ALPHA,
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
        adjusted[cid] = _apply_authority_nudge(raw, auth, authority_alpha)

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
