"""Progressive disclosure — compact default vs detail="full".

Code/doc hits ship snippet-only in compact mode (the agent can Read the
file); memory-lane hits (conversations, commits, qa) keep capped content
because there is no readable file behind them.
"""

from __future__ import annotations

from hybrid_search.search.orchestrator import HybridResult, HybridSearchResponse
from hybrid_search.tools.hybrid_search import _MEMORY_CONTENT_CAP, handle_hybrid_search


class _MockOrchestrator:
    def __init__(self, response: HybridSearchResponse) -> None:
        self._response = response

    def hybrid_search(self, **kwargs) -> HybridSearchResponse:
        return self._response


def _result(chunk_id: str, node_type: str, content: str) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id, rrf_score=0.02, bm25_rank=1, vector_rank=1,
        file_path=f"src/{chunk_id}.py" if node_type == "function" else f".virtual/{chunk_id}",
        project="p", name=chunk_id, qualified_name=chunk_id, node_type=node_type,
        start_line=1, end_line=9, content=content, snippet=content[:80],
    )


def _response(results) -> HybridSearchResponse:
    return HybridSearchResponse(
        results=results, query_type="KOREAN_NL", effective_bm25_weight=0.15,
        query_time_ms=10, total_chunks_searched=100,
    )


class TestCompactDefault:
    def test_code_chunk_content_omitted(self) -> None:
        orch = _MockOrchestrator(_response([_result("fn", "function", "def fn():\n" * 100)]))
        out = handle_hybrid_search(orch, query="테스트")
        assert out["results"][0]["content"] is None
        assert out["results"][0]["snippet"]  # snippet stays

    def test_memory_chunk_content_kept_but_capped(self) -> None:
        long_text = "대화 내용 " * 500  # far over the cap
        orch = _MockOrchestrator(_response([_result("t1", "conv_turn", long_text)]))
        out = handle_hybrid_search(orch, query="지난번에 뭐라고 했지")
        content = out["results"][0]["content"]
        assert content is not None
        assert len(content) <= _MEMORY_CONTENT_CAP + 60
        assert "truncated" in content

    def test_short_memory_content_untouched(self) -> None:
        orch = _MockOrchestrator(_response([_result("c1", "commit", "fix: 짧은 커밋")]))
        out = handle_hybrid_search(orch, query="커밋")
        assert out["results"][0]["content"] == "fix: 짧은 커밋"


class TestFullDetail:
    def test_full_returns_complete_content_for_code(self) -> None:
        body = "def fn():\n" * 100
        orch = _MockOrchestrator(_response([_result("fn", "function", body)]))
        out = handle_hybrid_search(orch, query="테스트", detail="full")
        assert out["results"][0]["content"] == body

    def test_full_returns_complete_memory_content(self) -> None:
        long_text = "대화 내용 " * 500
        orch = _MockOrchestrator(_response([_result("t1", "conv_turn", long_text)]))
        out = handle_hybrid_search(orch, query="지난번", detail="full")
        assert out["results"][0]["content"] == long_text
