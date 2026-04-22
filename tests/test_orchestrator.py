"""Orchestrator integration tests — authority routing & type-gating (M1.2).

Mocks ``_search_single`` + ``_enrich_results`` so the test focuses on how
``hybrid_search`` wires the authority map into fusion based on query type.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hybrid_search.project import ProjectInfo
from hybrid_search.search.orchestrator import SearchOrchestrator


def _make_orchestrator(authority_map: dict[str, float]) -> SearchOrchestrator:
    config = MagicMock()
    config.search.rrf_k = 60
    config.search.reranking.enabled = False
    config.search.reranking.max_candidates = 20

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
        return_value=(["a", "b"], ["b", "a"], 2, [], authority_map)
    )
    orch._enrich_results = MagicMock(return_value=[])
    # Module injection would try to open a StoreDB with a mock path — stub it.
    orch._module_results_for_query = MagicMock(return_value=[])
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

        _, kwargs = fusion.call_args
        assert kwargs["chunk_authority_scores"] is None
        assert resp.query_type == "EXACT_SYMBOL"

    def test_snake_case_symbol_disables_authority(self):
        auth = {"a": 1.0}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="compute_file_hash", project="test")

        _, kwargs = fusion.call_args
        assert kwargs["chunk_authority_scores"] is None

    def test_korean_nl_keeps_authority(self):
        auth = {"a": 1.0, "b": 0.5}
        orch = _make_orchestrator(auth)
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="저신뢰 엣지가 검색에 미치는 영향", project="test")

        _, kwargs = fusion.call_args
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

        _, kwargs = fusion.call_args
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

        _, kwargs = fusion.call_args
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

        _, kwargs = fusion.call_args
        assert kwargs["chunk_authority_scores"] is None

    def test_natural_language_with_empty_authority_passes_none(self):
        """Empty map falls back to None for non-gated queries (existing behavior)."""
        orch = _make_orchestrator({})
        with patch(
            "hybrid_search.search.orchestrator.reciprocal_rank_fusion"
        ) as fusion:
            fusion.return_value = []
            orch.hybrid_search(query="로그인 처리 흐름", project="test")

        _, kwargs = fusion.call_args
        assert kwargs["chunk_authority_scores"] is None


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
