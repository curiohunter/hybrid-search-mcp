"""Code-lane guard + generated_ratio (P1 lane separation).

The main code lane must stay answerable by code: wiki-derived chunks from
pre-fix indexes are dropped, ambient memory chunks are capped, exact-symbol
queries exclude memory entirely, and the response carries a source-diversity
signal (``generated_ratio``) as a retrieval-collapse early warning.
"""

from __future__ import annotations

from hybrid_search.memory.router import fallback_hint
from hybrid_search.search.orchestrator import (
    HybridResult,
    QueryType,
    _effective_gap_and_coherence,
    _generated_ratio,
    _guard_code_lane,
    _MEMORY_HEAD_CAP,
)


def _mk(
    chunk_id: str,
    node_type: str,
    rrf: float = 1.0,
    file_path: str | None = None,
) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id,
        rrf_score=rrf,
        bm25_rank=1,
        vector_rank=1,
        file_path=file_path or f"src/{chunk_id}.py",
        project="p",
        name=chunk_id,
        qualified_name=chunk_id,
        node_type=node_type,
        start_line=1,
        end_line=2,
        content=None,
        snippet="s",
    )


class TestGuardCodeLane:
    def test_code_only_passes_through(self) -> None:
        results = [_mk("a", "function"), _mk("b", "class"), _mk("c", "section")]
        assert _guard_code_lane(results, QueryType.KOREAN_NL, False) == results

    def test_wiki_derived_chunks_always_dropped(self) -> None:
        # Stale indexes built before the scanner fix still hold wiki chunks.
        results = [
            _mk("w", "section", file_path=".hybrid-search/wiki/storage-isolated.md"),
            _mk("a", "function"),
        ]
        guarded = _guard_code_lane(results, QueryType.KOREAN_NL, False)
        assert [r.chunk_id for r in guarded] == ["a"]

    def test_wiki_dropped_even_on_memory_intent(self) -> None:
        results = [_mk("w", "section", file_path=".hybrid-search/wiki/x.md")]
        assert _guard_code_lane(results, QueryType.KOREAN_NL, True) == []

    def test_memory_capped_on_topical_query(self) -> None:
        results = [
            _mk("q1", "qa_log"),
            _mk("q2", "qa_log"),
            _mk("q3", "qa_log"),
            _mk("q4", "memory_card"),
            _mk("a", "function"),
        ]
        guarded = _guard_code_lane(results, QueryType.KOREAN_NL, False)
        memory = [r for r in guarded if r.node_type in ("qa_log", "memory_card")]
        assert len(memory) == _MEMORY_HEAD_CAP
        # Highest-ranked memory chunks survive, order preserved.
        assert [r.chunk_id for r in guarded] == ["q1", "q2", "a"]

    def test_memory_intent_bypasses_cap(self) -> None:
        results = [_mk(f"q{i}", "qa_log") for i in range(5)]
        guarded = _guard_code_lane(results, QueryType.KOREAN_NL, True)
        assert len(guarded) == 5

    def test_exact_symbol_query_drops_memory(self) -> None:
        results = [_mk("q1", "qa_log"), _mk("a", "function")]
        guarded = _guard_code_lane(results, QueryType.EXACT_SYMBOL, False)
        assert [r.chunk_id for r in guarded] == ["a"]


class TestGeneratedRatio:
    def test_all_code_is_zero(self) -> None:
        assert _generated_ratio([_mk("a", "function"), _mk("b", "class")]) == 0.0

    def test_memory_and_generated_paths_counted(self) -> None:
        results = [
            _mk("a", "function"),
            _mk("q", "qa_log"),
            _mk("c", "conv_turn"),
            _mk("w", "section", file_path=".hybrid-search/qa/2026/07/x.md"),
        ]
        assert _generated_ratio(results) == 0.75

    def test_module_cards_not_counted(self) -> None:
        # Navigation aids are neither code nor pollution — excluded from the pool.
        results = [_mk("m", "module_card"), _mk("a", "function")]
        assert _generated_ratio(results) == 0.0

    def test_empty_results(self) -> None:
        assert _generated_ratio([]) == 0.0


def _mkf(chunk_id: str, rrf: float, file_path: str, module_id: str | None = None) -> HybridResult:
    r = _mk(chunk_id, "function", rrf=rrf, file_path=file_path)
    return HybridResult(**{**r.__dict__, "module_id": module_id})


class TestEffectiveGapAndCoherence:
    def test_empty(self) -> None:
        assert _effective_gap_and_coherence([]) == (None, False)

    def test_single_result_has_no_gap(self) -> None:
        gap, _ = _effective_gap_and_coherence([_mkf("a", 0.03, "src/a.py")])
        assert gap is None

    def test_same_file_siblings_are_not_a_tie(self) -> None:
        # Runner-ups from the top hit's own file are corroboration — the gap
        # is measured against the first *different-file* result.
        ranked = [
            _mkf("a1", 0.030, "src/a.py"),
            _mkf("a2", 0.0299, "src/a.py"),
            _mkf("b", 0.020, "src/b.py"),
        ]
        gap, _ = _effective_gap_and_coherence(ranked)
        assert gap == 0.01

    def test_all_same_file_is_max_separation(self) -> None:
        ranked = [_mkf("a1", 0.030, "src/a.py"), _mkf("a2", 0.0299, "src/a.py")]
        gap, _ = _effective_gap_and_coherence(ranked)
        assert gap == 0.03

    def test_same_directory_head_is_coherent(self) -> None:
        ranked = [
            _mkf("a", 0.030, "src/auth/login.py"),
            _mkf("b", 0.0299, "src/auth/token.py"),
            _mkf("c", 0.0298, "src/auth/session.py"),
        ]
        _, coherent = _effective_gap_and_coherence(ranked)
        assert coherent is True

    def test_same_module_head_is_coherent(self) -> None:
        ranked = [
            _mkf("a", 0.030, "src/auth/login.py", module_id="m1"),
            _mkf("b", 0.0299, "src/billing/pay.py", module_id="m1"),
            _mkf("c", 0.0298, "src/api/handler.py", module_id="m1"),
        ]
        _, coherent = _effective_gap_and_coherence(ranked)
        assert coherent is True

    def test_scattered_head_is_not_coherent(self) -> None:
        ranked = [
            _mkf("a", 0.030, "src/auth/login.py"),
            _mkf("b", 0.0299, "src/billing/pay.py"),
            _mkf("c", 0.0298, "docs/readme.md"),
        ]
        _, coherent = _effective_gap_and_coherence(ranked)
        assert coherent is False


class TestFallbackHintTopHit:
    def test_top_hit_appended(self) -> None:
        hint = fallback_hint("상담 등록 흐름", top_hit="src/consultations/flow.py")
        assert hint.endswith("(top hit: src/consultations/flow.py)")

    def test_without_top_hit_unchanged(self) -> None:
        assert "top hit" not in fallback_hint("상담 등록 흐름")
