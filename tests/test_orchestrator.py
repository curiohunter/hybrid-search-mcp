"""Orchestrator integration tests — authority routing & type-gating (M1.2).

Mocks ``_search_single`` + ``_enrich_results`` so the test focuses on how
``hybrid_search`` wires the authority map into fusion based on query type.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hybrid_search.project import ProjectInfo
from hybrid_search.search.in_flight import InFlightFile, InFlightOverlay
from hybrid_search.search.orchestrator import HybridResult, SearchOrchestrator


def _make_orchestrator(
    authority_map: dict[str, float],
    cosine_anchor: float = 0.0,
) -> SearchOrchestrator:
    config = MagicMock()
    config.search.rrf_k = 60
    config.search.reranking.enabled = False
    config.search.reranking.max_candidates = 20
    config.search.reranking.lexical = False
    config.search.reranking.lexical_weight = 0.6
    config.router.confidence.as_dict.return_value = {
        "strong_score": 0.02,
        "strong_gap": 0.001,
        "weak_score": 0.01,
        "cosine_anchor": cosine_anchor,
    }

    pinfo = ProjectInfo(
        id="proj1", name="test", path="/tmp/test",
        last_indexed_at=None, file_count=1, chunk_count=2,
    )
    registry = MagicMock()
    registry.list_all.return_value = [pinfo]
    registry.get_by_name.return_value = pinfo

    embedder = MagicMock()
    embedder.embed_query.return_value = [0.1, 0.2]

    orch = SearchOrchestrator(config, registry, embedder)
    orch._search_single = MagicMock(
        return_value=(["a", "b"], ["b", "a"], 2, [], authority_map, {"b": 0.7, "a": 0.6})
    )
    orch._enrich_results = MagicMock(return_value=[])
    # Module injection would try to open a StoreDB with a mock path — stub it.
    orch._module_results_for_query = MagicMock(return_value=([], []))
    return orch


class TestAuthorityGating:
    """M1.2 — EXACT_SYMBOL queries must bypass the authority nudge."""

    def test_exact_symbol_disables_authority(self):
        auth = {"a": 1.0, "b": 0.5}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            resp = orch.hybrid_search(query="FusedResult", project="test")

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] is None
        assert resp.query_type == "EXACT_SYMBOL"

    def test_response_metadata_does_not_change_hit_order(self):
        orch = _make_orchestrator({})
        expected = [
            HybridResult(
                chunk_id="a",
                rrf_score=0.02,
                bm25_rank=1,
                vector_rank=None,
                file_path="src/a.py",
                project="test",
                name="a",
                qualified_name="src.a",
                node_type="function",
                start_line=1,
                end_line=2,
                # A genuinely strong hit contains the query's content words —
                # empty text would now (correctly) demote strong to mixed.
                content="search entrypoint: how work is routed",
                snippet="",
            ),
            HybridResult(
                chunk_id="b",
                rrf_score=0.015,
                bm25_rank=None,
                vector_rank=1,
                file_path="src/b.py",
                project="test",
                name="b",
                qualified_name="src.b",
                node_type="function",
                start_line=3,
                end_line=4,
                content="",
                snippet="",
            ),
        ]
        orch._enrich_results = MagicMock(return_value=expected)

        response = orch.hybrid_search(query="how does search work", project="test")

        assert [r.chunk_id for r in response.results] == ["a", "b"]
        assert response.top_score == 0.02
        assert response.score_gap == 0.005
        assert response.confidence == "strong"

    def test_cosine_anchor_rescues_weak_to_mixed(self):
        # Low RRF top (below weak_score=0.01) → weak; but the vector lane's
        # best cosine (0.7 in the mock) clears the calibrated anchor.
        orch = _make_orchestrator({}, cosine_anchor=0.65)
        weak_hit = HybridResult(
            chunk_id="a", rrf_score=0.005, bm25_rank=1, vector_rank=1,
            file_path="src/a.py", project="test", name="a", qualified_name="a",
            node_type="function", start_line=1, end_line=2, content="", snippet="",
        )
        orch._enrich_results = MagicMock(return_value=[weak_hit])

        response = orch.hybrid_search(query="흐름이 어떻게 되나", project="test")

        assert response.top_cosine == 0.7
        assert response.confidence == "mixed"

    def test_cosine_anchor_disabled_keeps_weak(self):
        orch = _make_orchestrator({}, cosine_anchor=0.0)
        weak_hit = HybridResult(
            chunk_id="a", rrf_score=0.005, bm25_rank=1, vector_rank=1,
            file_path="src/a.py", project="test", name="a", qualified_name="a",
            node_type="function", start_line=1, end_line=2, content="", snippet="",
        )
        orch._enrich_results = MagicMock(return_value=[weak_hit])

        response = orch.hybrid_search(query="흐름이 어떻게 되나", project="test")

        assert response.confidence == "weak"

    def test_cosine_anchor_ignores_empty_results(self):
        orch = _make_orchestrator({}, cosine_anchor=0.65)
        orch._enrich_results = MagicMock(return_value=[])

        response = orch.hybrid_search(query="흐름이 어떻게 되나", project="test")

        assert response.confidence == "weak"

    def test_snake_case_symbol_disables_authority(self):
        auth = {"a": 1.0}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="compute_file_hash", project="test")

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] is None

    def test_korean_nl_keeps_authority(self):
        auth = {"a": 1.0, "b": 0.5}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="저신뢰 엣지가 검색에 미치는 영향", project="test")

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] == auth

    def test_english_nl_keeps_authority(self):
        auth = {"a": 1.0}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(
                query="how does wiki staleness detection work", project="test"
            )

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] == auth

    def test_mixed_korean_symbol_keeps_authority(self):
        """Mixed queries classify as KOREAN_NL — gating does not apply."""
        auth = {"a": 1.0}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            resp = orch.hybrid_search(
                query="resolve_call_edges 함수 역할", project="test"
            )

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] == auth
        assert resp.query_type == "KOREAN_NL"

    def test_exact_symbol_with_empty_authority_passes_none(self):
        """Empty map + EXACT_SYMBOL — still None (not ``{}``)."""
        orch = _make_orchestrator({})
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="HybridResult", project="test")

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] is None

    def test_natural_language_with_empty_authority_passes_none(self):
        """Empty map falls back to None for non-gated queries (existing behavior)."""
        orch = _make_orchestrator({})
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="로그인 처리 흐름", project="test")

        _, kwargs = fusion.call_args_list[0]  # first call = main lane (ambient memory lane fuses after)
        assert kwargs["chunk_authority_scores"] is None


class TestInFlightOverlay:
    def test_dirty_modified_file_replaces_stale_same_file_result(self):
        orch = _make_orchestrator({})
        stale = HybridResult(
            chunk_id="old",
            rrf_score=0.02,
            bm25_rank=1,
            vector_rank=None,
            file_path="src/app.py",
            project="test",
            name="app.py",
            qualified_name="src/app.py",
            node_type="function",
            start_line=1,
            end_line=2,
            content="old indexed content",
            snippet="old indexed content",
        )
        dirty = HybridResult(
            chunk_id="ephemeral:proj1:abc",
            rrf_score=0.03,
            bm25_rank=1,
            vector_rank=None,
            file_path="src/app.py",
            project="test",
            name="app.py",
            qualified_name="src/app.py",
            node_type="in_flight_file",
            start_line=1,
            end_line=None,
            content="new dirty content",
            snippet="[in-flight] src/app.py\nnew dirty content",
            trust_meta="[in-flight dirty worktree; not indexed]",
        )
        orch._enrich_results = MagicMock(return_value=[stale])

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            with patch("hybrid_search.search.orchestrator.score_in_flight_files") as score:
                collect.return_value = MagicMock(files=[object()], deleted_paths=set())
                score.return_value = [dirty]
                resp = orch.hybrid_search(query="dirty content", cwd="/tmp/test")

        assert [r.chunk_id for r in resp.results] == ["ephemeral:proj1:abc"]
        assert resp.results[0].trust_meta == "[in-flight dirty worktree; not indexed]"

    def test_dirty_deleted_file_suppresses_stale_indexed_result(self):
        orch = _make_orchestrator({})
        orch._enrich_results = MagicMock(
            return_value=[
                HybridResult(
                    chunk_id="old",
                    rrf_score=0.02,
                    bm25_rank=1,
                    vector_rank=None,
                    file_path="src/deleted.py",
                    project="test",
                    name="deleted.py",
                    qualified_name="src/deleted.py",
                    node_type="function",
                    start_line=1,
                    end_line=2,
                    content="old indexed content",
                    snippet="old indexed content",
                ),
                HybridResult(
                    chunk_id="keep",
                    rrf_score=0.01,
                    bm25_rank=2,
                    vector_rank=None,
                    file_path="src/keep.py",
                    project="test",
                    name="keep.py",
                    qualified_name="src/keep.py",
                    node_type="function",
                    start_line=1,
                    end_line=2,
                    content="keep",
                    snippet="keep",
                ),
            ]
        )

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            collect.return_value = MagicMock(files=[], deleted_paths={"src/deleted.py"})
            resp = orch.hybrid_search(query="deleted", cwd="/tmp/test")

        assert [r.file_path for r in resp.results] == ["src/keep.py"]

    def test_no_cwd_does_not_collect_overlay(self):
        orch = _make_orchestrator({})
        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            orch.hybrid_search(query="dirty content", project="test")

        collect.assert_not_called()

    def test_cross_project_search_does_not_collect_overlay(self):
        orch = _make_orchestrator({})
        p2 = ProjectInfo(
            id="proj2", name="other", path="/tmp/other",
            last_indexed_at=None, file_count=1, chunk_count=1,
        )
        orch._registry.list_all.return_value = [orch._registry.get_by_name.return_value, p2]
        orch._search_cross_project = MagicMock(return_value=(["a"], ["a"], 1, [], {}, {"a": 0.6}))

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            orch.hybrid_search(query="dirty content")

        collect.assert_not_called()

    def test_weak_confidence_remains_possible_for_low_dirty_score(self):
        orch = _make_orchestrator({})
        dirty = HybridResult(
            chunk_id="ephemeral:proj1:abc",
            rrf_score=0.001,
            bm25_rank=1,
            vector_rank=None,
            file_path="src/app.py",
            project="test",
            name="app.py",
            qualified_name="src/app.py",
            node_type="in_flight_file",
            start_line=1,
            end_line=None,
            content="weak",
            snippet="[in-flight] src/app.py\nweak",
            trust_meta="[in-flight dirty worktree; not indexed]",
        )
        orch._enrich_results = MagicMock(return_value=[])

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            with patch("hybrid_search.search.orchestrator.score_in_flight_files") as score:
                collect.return_value = MagicMock(files=[object()], deleted_paths=set())
                score.return_value = [dirty]
                resp = orch.hybrid_search(query="weak", cwd="/tmp/test")

        assert resp.confidence == "weak"

    def test_file_pattern_applies_to_in_flight_overlay(self):
        orch = _make_orchestrator({})
        orch._enrich_results = MagicMock(return_value=[])
        overlay = InFlightOverlay(
            files=[
                InFlightFile(
                    relative_path="src/app.py",
                    status="modified",
                    content="phase overlay marker",
                    content_hash="a",
                ),
                InFlightFile(
                    relative_path="docs/plan.md",
                    status="modified",
                    content="phase overlay marker",
                    content_hash="b",
                ),
            ],
            deleted_paths=set(),
        )

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            collect.return_value = overlay
            resp = orch.hybrid_search(
                query="phase overlay marker",
                cwd="/tmp/test",
                file_pattern="docs/*",
            )

        assert [r.file_path for r in resp.results] == ["docs/plan.md"]
        assert resp.results[0].node_type == "in_flight_file"

    def test_node_types_function_suppresses_in_flight_file(self):
        orch = _make_orchestrator({})
        orch._enrich_results = MagicMock(return_value=[])
        overlay = InFlightOverlay(
            files=[
                InFlightFile(
                    relative_path="src/app.py",
                    status="modified",
                    content="dirty function marker",
                    content_hash="a",
                ),
            ],
            deleted_paths=set(),
        )

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            collect.return_value = overlay
            resp = orch.hybrid_search(
                query="dirty function marker",
                cwd="/tmp/test",
                node_types=["function"],
            )

        assert resp.results == []

    def test_node_types_in_flight_file_allows_dirty_result(self):
        orch = _make_orchestrator({})
        orch._enrich_results = MagicMock(return_value=[])
        overlay = InFlightOverlay(
            files=[
                InFlightFile(
                    relative_path="src/app.py",
                    status="modified",
                    content="dirty overlay marker",
                    content_hash="a",
                ),
            ],
            deleted_paths=set(),
        )

        with patch("hybrid_search.search.orchestrator.collect_in_flight_overlay") as collect:
            collect.return_value = overlay
            resp = orch.hybrid_search(
                query="dirty overlay marker",
                cwd="/tmp/test",
                node_types=["in_flight_file"],
            )

        assert [r.node_type for r in resp.results] == ["in_flight_file"]
        assert resp.results[0].file_path == "src/app.py"


class TestBuildFilterExcludePattern:
    """D — exclude_pattern drops chunks whose file matches the glob."""

    @staticmethod
    def _db_with_files(files: list[tuple[str, str]], chunks: list[tuple[str, str, str]]):
        """files: list[(file_id, rel_path)], chunks: list[(chunk_id, file_id, node_type)]."""
        db = MagicMock()
        file_recs = [MagicMock(id=fid, relative_path=path) for fid, path in files]
        chunk_recs = [
            MagicMock(id=cid, file_id=fid, node_type=ntype)
            for cid, fid, ntype in chunks
        ]
        db.get_all_files.return_value = file_recs
        db.get_chunks_by_project.return_value = chunk_recs
        return db

    def test_exclude_pattern_drops_matching_files(self):
        from hybrid_search.search.orchestrator import _build_filter

        db = self._db_with_files(
            files=[("f1", "src/app.py"), ("f2", "docs/guide.md")],
            chunks=[("c1", "f1", "function"), ("c2", "f2", "section")],
        )
        result = _build_filter(db, "p1", None, None, exclude_pattern="docs/*")
        assert result == {"c1"}

    def test_exclude_pattern_combines_with_file_pattern(self):
        from hybrid_search.search.orchestrator import _build_filter

        db = self._db_with_files(
            files=[
                ("f1", "src/app.py"),
                ("f2", "src/test_app.py"),
                ("f3", "docs/guide.md"),
            ],
            chunks=[
                ("c1", "f1", "function"),
                ("c2", "f2", "function"),
                ("c3", "f3", "section"),
            ],
        )
        result = _build_filter(
            db, "p1", file_pattern="src/*", node_types=None,
            exclude_pattern="*test_*",
        )
        assert result == {"c1"}

    def test_none_when_all_filters_empty(self):
        from hybrid_search.search.orchestrator import _build_filter

        db = MagicMock()
        assert _build_filter(db, "p1", None, None, None) is None
