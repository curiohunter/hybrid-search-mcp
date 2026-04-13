"""Tests for RRF (Reciprocal Rank Fusion) — search/fusion.py."""

from hybrid_search.search.fusion import FusedResult, reciprocal_rank_fusion


class TestReciprocalRankFusion:
    """reciprocal_rank_fusion() tests."""

    def test_basic_fusion_both_lists(self) -> None:
        bm25 = ["a", "b", "c"]
        vec = ["b", "c", "d"]
        results = reciprocal_rank_fusion(bm25, vec, k=60, bm25_weight=0.5)

        ids = [r.chunk_id for r in results]
        # b and c appear in both lists → higher scores
        assert "b" in ids
        assert "c" in ids
        assert "a" in ids
        assert "d" in ids
        # b appears at rank 2 in bm25 and rank 1 in vec → likely highest
        assert results[0].chunk_id == "b"

    def test_scores_are_descending(self) -> None:
        bm25 = ["x", "y", "z"]
        vec = ["z", "y", "x"]
        results = reciprocal_rank_fusion(bm25, vec, k=60, bm25_weight=0.5)
        scores = [r.rrf_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_ranks_tracked_correctly(self) -> None:
        bm25 = ["a", "b"]
        vec = ["b", "c"]
        results = reciprocal_rank_fusion(bm25, vec)
        result_map = {r.chunk_id: r for r in results}

        assert result_map["a"].bm25_rank == 1
        assert result_map["a"].vector_rank is None
        assert result_map["b"].bm25_rank == 2
        assert result_map["b"].vector_rank == 1
        assert result_map["c"].bm25_rank is None
        assert result_map["c"].vector_rank == 2

    def test_empty_bm25_list(self) -> None:
        results = reciprocal_rank_fusion([], ["a", "b"], k=60, bm25_weight=0.5)
        assert len(results) == 2
        assert results[0].chunk_id == "a"
        assert results[0].bm25_rank is None

    def test_empty_vector_list(self) -> None:
        results = reciprocal_rank_fusion(["a", "b"], [], k=60, bm25_weight=0.5)
        assert len(results) == 2
        assert results[0].chunk_id == "a"
        assert results[0].vector_rank is None

    def test_both_empty(self) -> None:
        results = reciprocal_rank_fusion([], [])
        assert results == []

    def test_bm25_weight_zero_ignores_bm25(self) -> None:
        bm25 = ["a", "b"]
        vec = ["c", "d"]
        results = reciprocal_rank_fusion(bm25, vec, bm25_weight=0.0)
        ids = [r.chunk_id for r in results]
        # vector-only scores: c > d; bm25 contributes 0
        assert ids[0] == "c"
        assert ids[1] == "d"

    def test_bm25_weight_one_ignores_vector(self) -> None:
        bm25 = ["a", "b"]
        vec = ["c", "d"]
        results = reciprocal_rank_fusion(bm25, vec, bm25_weight=1.0)
        ids = [r.chunk_id for r in results]
        assert ids[0] == "a"
        assert ids[1] == "b"

    def test_k_parameter_affects_scores(self) -> None:
        bm25 = ["a"]
        vec = ["a"]
        result_k1 = reciprocal_rank_fusion(bm25, vec, k=1)
        result_k100 = reciprocal_rank_fusion(bm25, vec, k=100)
        # Smaller k → higher score (1/(1+1) > 1/(100+1))
        assert result_k1[0].rrf_score > result_k100[0].rrf_score

    def test_duplicate_ids_in_same_list(self) -> None:
        # If a list has duplicates, each occurrence adds to score
        bm25 = ["a", "a"]
        vec = []
        results = reciprocal_rank_fusion(bm25, vec, bm25_weight=0.5)
        # "a" appears twice in bm25, score = 0.5/(60+1) + 0.5/(60+2)
        assert len(results) == 1
        expected = 0.5 / 61 + 0.5 / 62
        assert abs(results[0].rrf_score - expected) < 1e-10

    def test_score_formula_correctness(self) -> None:
        bm25 = ["a"]
        vec = ["a"]
        results = reciprocal_rank_fusion(bm25, vec, k=60, bm25_weight=0.5)
        expected = 0.5 / 61 + 0.5 / 61  # rank 1 in both
        assert abs(results[0].rrf_score - expected) < 1e-10

    def test_asymmetric_weights(self) -> None:
        bm25 = ["a"]
        vec = ["b"]
        results = reciprocal_rank_fusion(bm25, vec, k=60, bm25_weight=0.8)
        result_map = {r.chunk_id: r for r in results}
        # a gets 0.8/61, b gets 0.2/61
        assert result_map["a"].rrf_score > result_map["b"].rrf_score

    def test_large_lists(self) -> None:
        bm25 = [f"chunk_{i}" for i in range(100)]
        vec = [f"chunk_{i}" for i in range(50, 150)]
        results = reciprocal_rank_fusion(bm25, vec)
        assert len(results) == 150  # 0..149 unique
        # Overlapping chunks (50-99) should have highest scores
        top_10_ids = {r.chunk_id for r in results[:10]}
        for i in range(50, 60):
            assert f"chunk_{i}" in top_10_ids
