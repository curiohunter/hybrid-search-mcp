"""Graph card — structural questions get the call graph inline."""

from __future__ import annotations

from hybrid_search.search.orchestrator import _has_graph_intent


class TestGraphIntent:
    def test_korean_structural_phrases(self) -> None:
        assert _has_graph_intent("completeRefund는 누가 호출해") is True
        assert _has_graph_intent("이 함수 어디서 사용돼") is True
        assert _has_graph_intent("ledger 모듈이 뭘 의존하는지") is True

    def test_english_structural_phrases(self) -> None:
        assert _has_graph_intent("who calls completeRefund") is True
        assert _has_graph_intent("callers of syncCalendarEvents") is True
        assert _has_graph_intent("what depends on the ledger module") is True

    def test_plain_queries_do_not_trigger(self) -> None:
        assert _has_graph_intent("환불 기능 설명해줘") is False
        assert _has_graph_intent("how does auth work") is False
        assert _has_graph_intent("") is False
