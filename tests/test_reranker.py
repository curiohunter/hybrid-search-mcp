"""Tests for LLM re-ranking (Phase 10) — Claude Code native approach.

No external API calls. Reranking = return more candidates + hint for Claude Code.
"""

from __future__ import annotations

import pytest

from hybrid_search.config import Config, RerankingConfig, SearchConfig
from hybrid_search.search.orchestrator import HybridResult, HybridSearchResponse
from hybrid_search.tools.hybrid_search import _RERANK_HINT, handle_hybrid_search


# -- Config tests --

class TestRerankingConfig:
    def test_defaults(self) -> None:
        cfg = RerankingConfig()
        assert cfg.enabled is False
        assert cfg.max_candidates == 20

    def test_custom_values(self) -> None:
        cfg = RerankingConfig(enabled=True, max_candidates=30)
        assert cfg.enabled is True
        assert cfg.max_candidates == 30

    def test_search_config_includes_reranking(self) -> None:
        cfg = SearchConfig()
        assert isinstance(cfg.reranking, RerankingConfig)
        assert cfg.reranking.enabled is False

    def test_config_reranking_accessible(self) -> None:
        cfg = Config()
        assert cfg.search.reranking.enabled is False
        assert cfg.search.reranking.max_candidates == 20


# -- Rerank hint tests --

class TestRerankHint:
    def test_hint_includes_query(self) -> None:
        hint = _RERANK_HINT.format(n=20, limit=10, query="로그인 처리")
        assert "로그인 처리" in hint

    def test_hint_includes_counts(self) -> None:
        hint = _RERANK_HINT.format(n=20, limit=10, query="test")
        assert "20" in hint
        assert "10" in hint

    def test_hint_mentions_rerank(self) -> None:
        hint = _RERANK_HINT.format(n=15, limit=5, query="q")
        assert "RERANK" in hint
        assert "reorder" in hint


# -- HybridSearchResponse reranked field --

class TestHybridSearchResponseReranked:
    def test_default_not_reranked(self) -> None:
        resp = HybridSearchResponse(
            results=[], query_type="ENGLISH_NL",
            effective_bm25_weight=0.5, query_time_ms=100,
            total_chunks_searched=0,
        )
        assert resp.reranked is False

    def test_reranked_flag(self) -> None:
        resp = HybridSearchResponse(
            results=[], query_type="ENGLISH_NL",
            effective_bm25_weight=0.5, query_time_ms=100,
            total_chunks_searched=0, reranked=True,
        )
        assert resp.reranked is True


# -- Tool handler rerank_hint injection --

def _make_mock_result(i: int) -> HybridResult:
    return HybridResult(
        chunk_id=f"chunk_{i}",
        rrf_score=1.0 / (i + 1),
        bm25_rank=i + 1,
        vector_rank=i + 1,
        file_path=f"src/mod_{i}.py",
        project="test",
        name=f"func_{i}",
        qualified_name=f"mod_{i}::func_{i}",
        node_type="function",
        start_line=i * 10,
        end_line=i * 10 + 5,
        content=f"def func_{i}(): pass",
        snippet=f"def func_{i}(): pass",
    )


class _MockOrchestrator:
    """Mock orchestrator that returns pre-built responses."""

    def __init__(self, response: HybridSearchResponse) -> None:
        self._response = response

    def hybrid_search(self, **kwargs) -> HybridSearchResponse:
        return self._response


class TestToolHandlerRerankHint:
    def test_no_hint_when_not_reranked(self) -> None:
        resp = HybridSearchResponse(
            results=[_make_mock_result(i) for i in range(5)],
            query_type="ENGLISH_NL",
            effective_bm25_weight=0.5,
            query_time_ms=100,
            total_chunks_searched=100,
            reranked=False,
        )
        orch = _MockOrchestrator(resp)
        result = handle_hybrid_search(orch, query="test", limit=10)
        assert "rerank_hint" not in result

    def test_hint_present_when_reranked_and_more_results(self) -> None:
        resp = HybridSearchResponse(
            results=[_make_mock_result(i) for i in range(20)],
            query_type="KOREAN_NL",
            effective_bm25_weight=0.15,
            query_time_ms=200,
            total_chunks_searched=500,
            reranked=True,
        )
        orch = _MockOrchestrator(resp)
        result = handle_hybrid_search(orch, query="로그인 에러", limit=10)
        assert "rerank_hint" in result
        assert "로그인 에러" in result["rerank_hint"]
        assert "20" in result["rerank_hint"]

    def test_no_hint_when_reranked_but_few_results(self) -> None:
        """If reranked but results <= limit, no hint needed."""
        resp = HybridSearchResponse(
            results=[_make_mock_result(i) for i in range(5)],
            query_type="ENGLISH_NL",
            effective_bm25_weight=0.5,
            query_time_ms=100,
            total_chunks_searched=5,
            reranked=True,
        )
        orch = _MockOrchestrator(resp)
        result = handle_hybrid_search(orch, query="small query", limit=10)
        assert "rerank_hint" not in result

    def test_result_count_matches_response(self) -> None:
        n = 15
        resp = HybridSearchResponse(
            results=[_make_mock_result(i) for i in range(n)],
            query_type="ENGLISH_NL",
            effective_bm25_weight=0.5,
            query_time_ms=100,
            total_chunks_searched=100,
            reranked=True,
        )
        orch = _MockOrchestrator(resp)
        result = handle_hybrid_search(orch, query="test", limit=10)
        assert len(result["results"]) == n  # All candidates returned for Claude Code

    def test_result_fields_complete(self) -> None:
        resp = HybridSearchResponse(
            results=[_make_mock_result(0)],
            query_type="EXACT_SYMBOL",
            effective_bm25_weight=0.8,
            query_time_ms=50,
            total_chunks_searched=100,
        )
        orch = _MockOrchestrator(resp)
        # detail="full" — compact mode intentionally omits code content
        # (progressive disclosure; the agent Reads the file instead).
        result = handle_hybrid_search(orch, query="funcName", detail="full")
        r = result["results"][0]
        assert r["chunk_id"] == "chunk_0"
        assert r["name"] == "func_0"
        assert r["file_path"] == "src/mod_0.py"
        assert r["content"] == "def func_0(): pass"
        assert r["snippet"] == "def func_0(): pass"

        compact = handle_hybrid_search(orch, query="funcName")
        assert compact["results"][0]["content"] is None
        assert compact["results"][0]["snippet"] == "def func_0(): pass"


# -- Config TOML parsing --

class TestConfigTomlParsing:
    def test_load_reranking_from_toml(self, tmp_path) -> None:
        toml_content = """\
[search]
default_limit = 15

[search.reranking]
enabled = true
max_candidates = 30
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)

        from hybrid_search.config import load_config
        cfg = load_config(config_file)
        assert cfg.search.reranking.enabled is True
        assert cfg.search.reranking.max_candidates == 30
        assert cfg.search.default_limit == 15

    def test_load_default_reranking_when_absent(self, tmp_path) -> None:
        toml_content = """\
[search]
default_limit = 10
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)

        from hybrid_search.config import load_config
        cfg = load_config(config_file)
        assert cfg.search.reranking.enabled is False
        assert cfg.search.reranking.max_candidates == 20
