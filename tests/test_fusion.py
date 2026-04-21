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


class TestAuthorityNudge:
    """M1.v2 — boost-only nudge from the call graph.

    After the 2026-04-21 mini-PoC showed the damping-only formula hurt
    keyword queries (Δ NDCG@10 = -0.049), we switched to
    ``rrf * (1.0 + α * authority)`` with α=0.3. Chunks without an authority
    signal pass through unchanged; authority-bearing chunks are boosted in
    proportion to call-edge confidence, never penalised.
    """

    def test_no_authority_map_preserves_baseline(self) -> None:
        results = reciprocal_rank_fusion(["a", "b"], ["b", "a"], bm25_weight=0.5)
        baseline = {r.chunk_id: r.rrf_score for r in results}

        results_auth = reciprocal_rank_fusion(
            ["a", "b"], ["b", "a"], bm25_weight=0.5,
            chunk_authority_scores=None,
        )
        for r in results_auth:
            assert r.rrf_score == baseline[r.chunk_id]
            assert r.authority is None

    def test_high_authority_chunk_beats_equal_ranked_peer(self) -> None:
        # a and b appear at the same bm25 rank; authority tie-breaks.
        results = reciprocal_rank_fusion(
            ["a", "b"], [], bm25_weight=1.0,
            chunk_authority_scores={"a": 1.0, "b": 0.3},
        )
        ordered = [r.chunk_id for r in results]
        assert ordered[0] == "a"
        # Low-confidence edge is boosted modestly — never zeroed.
        b_result = next(r for r in results if r.chunk_id == "b")
        assert b_result.rrf_score > 0
        assert b_result.authority == 0.3

    def test_low_authority_applies_modest_boost(self) -> None:
        """authority=0.3 → factor = 1.0 + 0.3*0.3 = 1.09."""
        base = reciprocal_rank_fusion(["a"], [], bm25_weight=1.0)
        nudged = reciprocal_rank_fusion(
            ["a"], [], bm25_weight=1.0,
            chunk_authority_scores={"a": 0.3},
        )
        expected = base[0].rrf_score * 1.09
        assert abs(nudged[0].rrf_score - expected) < 1e-12

    def test_authority_equal_one_hits_boost_ceiling(self) -> None:
        """authority=1.0 → factor = 1.0 + α = 1.3 (with α=0.3)."""
        base = reciprocal_rank_fusion(["x"], ["x"], bm25_weight=0.5)
        nudged = reciprocal_rank_fusion(
            ["x"], ["x"], bm25_weight=0.5,
            chunk_authority_scores={"x": 1.0},
        )
        expected = base[0].rrf_score * 1.3
        assert abs(nudged[0].rrf_score - expected) < 1e-12

    def test_chunks_outside_map_are_neutral(self) -> None:
        """Missing map entries must not be boosted — they pass through at full RRF.

        Under the boost-only formula, authority cannot hurt a chunk. A chunk
        present in the map with authority=0.0 gets factor 1.0 (same as absent).
        So neutral ↔ damped distinction is gone — this test now pins that an
        absent chunk and an authority=0.0 chunk produce the same score.
        """
        results_absent = reciprocal_rank_fusion(
            ["a"], ["b"], bm25_weight=0.5,
            chunk_authority_scores={},  # neither chunk in map
        )
        results_explicit_zero = reciprocal_rank_fusion(
            ["a"], ["b"], bm25_weight=0.5,
            chunk_authority_scores={"a": 0.0},  # a explicit zero
        )
        score_a_absent = next(r.rrf_score for r in results_absent if r.chunk_id == "a")
        score_a_zero = next(r.rrf_score for r in results_explicit_zero if r.chunk_id == "a")
        # factor 1.0 either way — no penalty for explicit zero.
        assert abs(score_a_absent - score_a_zero) < 1e-12


class TestAuthorityAlphaConfigurable:
    """L6 decision (2026-04-21): alpha is exposed via SearchConfig."""

    def test_alpha_zero_disables_nudge(self):
        """authority_alpha=0.0 → factor 1.0 regardless of authority value."""
        nudged = reciprocal_rank_fusion(
            ["x"], ["x"], bm25_weight=0.5,
            chunk_authority_scores={"x": 1.0},
            authority_alpha=0.0,
        )
        base = reciprocal_rank_fusion(["x"], ["x"], bm25_weight=0.5)
        assert abs(nudged[0].rrf_score - base[0].rrf_score) < 1e-12

    def test_alpha_05_yields_15x_ceiling(self):
        """authority_alpha=0.5 + auth=1.0 → factor 1.5."""
        results = reciprocal_rank_fusion(
            ["x"], ["x"], bm25_weight=0.5,
            chunk_authority_scores={"x": 1.0},
            authority_alpha=0.5,
        )
        base = reciprocal_rank_fusion(["x"], ["x"], bm25_weight=0.5)
        assert abs(results[0].rrf_score - base[0].rrf_score * 1.5) < 1e-12

    def test_alpha_default_matches_constant(self):
        """Omitting authority_alpha uses DEFAULT_AUTHORITY_ALPHA=0.3."""
        from hybrid_search.search.fusion import DEFAULT_AUTHORITY_ALPHA
        default = reciprocal_rank_fusion(
            ["x"], ["x"], bm25_weight=0.5,
            chunk_authority_scores={"x": 1.0},
        )
        explicit = reciprocal_rank_fusion(
            ["x"], ["x"], bm25_weight=0.5,
            chunk_authority_scores={"x": 1.0},
            authority_alpha=DEFAULT_AUTHORITY_ALPHA,
        )
        assert DEFAULT_AUTHORITY_ALPHA == 0.3
        assert abs(default[0].rrf_score - explicit[0].rrf_score) < 1e-12
