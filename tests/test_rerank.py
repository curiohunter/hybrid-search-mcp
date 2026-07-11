"""Lexical second-stage rerank — query-term coverage over the fused head."""

from __future__ import annotations

from hybrid_search.search.orchestrator import HybridResult
from hybrid_search.search.rerank import lexical_rerank


def _mk(chunk_id: str, rrf: float, *, name: str = "", snippet: str = "", content: str | None = None) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id,
        rrf_score=rrf,
        bm25_rank=1,
        vector_rank=1,
        file_path=f"src/{chunk_id}.py",
        project="p",
        name=name or chunk_id,
        qualified_name=chunk_id,
        node_type="function",
        start_line=1,
        end_line=2,
        content=content,
        snippet=snippet,
    )


class TestLexicalRerank:
    def test_coverage_lifts_answering_chunk(self) -> None:
        # "adjacent" chunk leads on fused score but covers none of the query;
        # the chunk that actually contains the asked-about terms must win.
        adjacent = _mk("adjacent", 1.0, snippet="tuition ledger row rendering")
        answering = _mk("answering", 0.8, snippet="정산 배치는 새벽 4시에 실행")
        out = lexical_rerank("정산 배치 몇시에 실행되나", [adjacent, answering])
        assert out[0].chunk_id == "answering"

    def test_korean_agglutination_matches_by_substring(self) -> None:
        # Query token "배치" must match content "배치는" (token-set equality fails).
        r = _mk("r", 0.9, snippet="정산 배치는 매일 돈다")
        out = lexical_rerank("배치 스케줄", [_mk("other", 1.0), r])
        assert out[0].chunk_id == "r"

    def test_scores_are_slot_preserved(self) -> None:
        # Rerank permutes order but must not inflate the score scale — the
        # confidence thresholds are calibrated on raw RRF distributions.
        a = _mk("a", 1.0)
        b = _mk("b", 0.8, snippet="정산 배치 새벽 실행")
        out = lexical_rerank("정산 배치 몇시", [a, b])
        assert [r.chunk_id for r in out] == ["b", "a"]
        assert [r.rrf_score for r in out] == [1.0, 0.8]

    def test_tail_beyond_top_n_keeps_order(self) -> None:
        head = [_mk(f"h{i}", 1.0 - i * 0.01) for i in range(3)]
        tail = [_mk("tail_hit", 0.5, snippet="정산 배치")]
        out = lexical_rerank("정산 배치", head + tail, top_n=3)
        # tail_hit covers the query but sits past the window — must not move.
        assert out[-1].chunk_id == "tail_hit"

    def test_zero_weight_is_passthrough(self) -> None:
        results = [_mk("a", 1.0), _mk("b", 0.9)]
        assert lexical_rerank("정산", results, weight=0.0) is results

    def test_no_tokens_is_passthrough(self) -> None:
        results = [_mk("a", 1.0), _mk("b", 0.9)]
        assert lexical_rerank("!!", results) is results

    def test_zero_coverage_scores_unchanged(self) -> None:
        results = [_mk("a", 1.0), _mk("b", 0.9)]
        out = lexical_rerank("무관한 질의어", results)
        assert [r.rrf_score for r in out] == [1.0, 0.9]
