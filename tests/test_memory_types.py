"""P1-1 tests — typed memory schema + write-time classification + quarantine.

Write-time: every new qa record carries ``memory_type`` (what kind of
memory) and ``verification`` (how much to trust it), biased toward the
conservative ``inferred``. Read-time: an inferred qa can top the ranking
but can never be the sole basis of a STRONG confidence claim, and
``needs_revalidation`` memories decay harder. Legacy records without the
fields keep today's behavior exactly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hybrid_search.memory import memory_types, qa_log
from hybrid_search.search.orchestrator import (
    HybridResult,
    SearchOrchestrator,
    _apply_memory_boost,
    _memory_verification,
)


# --- classifier ---------------------------------------------------------------

class TestClassify:
    def _c(self, query, answer=None, tools=(), trigger="stop_hook", client=None):
        return memory_types.classify(
            query=query, answer_excerpt=answer, tools_used=tools,
            trigger=trigger, client=client,
        )

    def test_machine_payload_is_observation(self) -> None:
        assert self._c("<task-notification> done") == ("observation", "inferred")

    def test_mcp_search_log_is_observation(self) -> None:
        assert self._c("환불 기능 알려줘", trigger="mcp_tool") == (
            "observation", "inferred",
        )

    def test_short_approval_is_accepted_decision(self) -> None:
        assert self._c("응 진행해") == ("decision", "accepted")
        assert self._c("1번으로 해줘") == ("decision", "accepted")
        assert self._c("yes, proceed") == ("decision", "accepted")

    def test_long_message_with_approval_word_is_not_a_decision(self) -> None:
        long_query = "진행해도 될지 모르겠는데 " + "관련된 맥락 " * 10
        mtype, _ = self._c(long_query)
        assert mtype != "decision"

    def test_codex_review_turn(self) -> None:
        mtype, verification = self._c(
            "재검토 판정: Request changes — blocker 3건",
            client="codex",
        )
        assert (mtype, verification) == ("review_finding", "inferred")

    def test_procedure_question(self) -> None:
        assert self._c("맥미니에서 어떻게 설치해?")[0] == "procedure"
        assert self._c("how to install this on a fresh mac")[0] == "procedure"

    def test_executed_with_test_evidence_is_verified_task_state(self) -> None:
        mtype, verification = self._c(
            "R1 구현 마무리해줘",
            answer="구현 완료했습니다. 1210 passed, 커밋했습니다.",
            tools=("Edit", "Bash"),
        )
        assert (mtype, verification) == ("task_state", "verified")

    def test_executed_without_evidence_stays_inferred(self) -> None:
        mtype, verification = self._c(
            "리팩토링 해줘",
            answer="구조를 정리했습니다.",
            tools=("Edit",),
        )
        assert (mtype, verification) == ("observation", "inferred")

    def test_plain_reasoning_turn_is_hypothesis(self) -> None:
        assert self._c(
            "이 버그 원인이 뭘까?", answer="아마 락 경합으로 보입니다.",
        ) == ("hypothesis", "inferred")


# --- write path -----------------------------------------------------------------

class TestWritePath:
    def test_record_turn_persists_typed_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
        root = tmp_path / "proj"
        (root / ".hybrid-search").mkdir(parents=True)
        written = qa_log.record_turn(
            query="응 진행해",
            cwd=str(root),
            answer_chars=10,
            answer_excerpt="P0-2 구현을 진행합니다.",
            async_write=False,
            dedup=False,
        )
        assert written is not None
        text = written.read_text(encoding="utf-8")
        assert "memory_type: decision" in text
        assert "verification: accepted" in text


# --- read-time quarantine ---------------------------------------------------------

def _qa_result(chunk_id: str, verification: str | None, score: float) -> HybridResult:
    fm = "---\nquery: \"alpha beta\"\n"
    if verification:
        fm += f"verification: {verification}\n"
    fm += "---\n\nanswer about alpha beta\n"
    return HybridResult(
        chunk_id=chunk_id, rrf_score=score, bm25_rank=1, vector_rank=1,
        file_path=f"qa/{chunk_id}.md", project="p", name=chunk_id,
        qualified_name=chunk_id, node_type="qa_log", start_line=1, end_line=5,
        content=fm, snippet="alpha beta",
    )


def _code_result(chunk_id: str, score: float) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id, rrf_score=score, bm25_rank=2, vector_rank=2,
        file_path=f"src/{chunk_id}.py", project="p", name=chunk_id,
        qualified_name=chunk_id, node_type="function", start_line=1, end_line=5,
        content="def alpha_beta(): pass", snippet="alpha beta",
    )


def _mk_orch() -> SearchOrchestrator:
    config = MagicMock()
    config.router.confidence.as_dict.return_value = {
        "strong_score": 0.02, "strong_gap": 0.001, "weak_score": 0.005,
        "cosine_anchor": 0.0,
    }
    return SearchOrchestrator(config, MagicMock(), MagicMock())


class TestQuarantine:
    def _confidence(self, top: HybridResult) -> str:
        orch = _mk_orch()
        resp = orch._make_response(
            query="alpha beta",
            results=[top, _code_result("second", 0.01)],
            query_type="ENGLISH_NL",
            effective_bm25_weight=0.4,
            query_time_ms=1.0,
            total_chunks_searched=10,
        )
        return resp.confidence

    def test_inferred_qa_cannot_anchor_strong(self) -> None:
        assert self._confidence(_qa_result("top", "inferred", 0.06)) == "mixed"

    def test_needs_revalidation_qa_cannot_anchor_strong(self) -> None:
        assert self._confidence(
            _qa_result("top", "needs_revalidation", 0.06)
        ) == "mixed"

    def test_verified_qa_keeps_strong(self) -> None:
        assert self._confidence(_qa_result("top", "verified", 0.06)) == "strong"

    def test_accepted_decision_keeps_strong(self) -> None:
        assert self._confidence(_qa_result("top", "accepted", 0.06)) == "strong"

    def test_legacy_untyped_qa_cannot_anchor_strong(self) -> None:
        """2026-07-15 review condition: legacy records keep their ranking
        but get no free pass on the trust contract — otherwise every
        pre-schema auto-captured turn keeps a privilege new records must
        earn (only their confidence LABEL is capped, never their score)."""
        assert self._confidence(_qa_result("top", None, 0.06)) == "mixed"

    def test_code_top_hit_unaffected(self) -> None:
        assert self._confidence(_code_result("top", 0.06)) == "strong"


# --- post-splice displayed-top cap (round-1 fix 1) -----------------------------------

class TestDisplayedTopCap:
    """The supersession splice changes the DISPLAYED top after
    classification — the trust cap must re-apply on what the agent
    actually reads, demote-only."""

    def _response(self, results, confidence="strong"):
        from hybrid_search.search.orchestrator import HybridSearchResponse
        return HybridSearchResponse(
            results=results, query_type="ENGLISH_NL",
            effective_bm25_weight=0.4, query_time_ms=1.0,
            total_chunks_searched=10, confidence=confidence,
        )

    def test_inferred_spliced_top_demotes_strong(self) -> None:
        from hybrid_search.search.orchestrator import _cap_confidence_for_displayed_top
        resp = self._response([_qa_result("spliced", "inferred", 0.06)])
        assert _cap_confidence_for_displayed_top(resp).confidence == "mixed"

    def test_legacy_spliced_top_demotes_strong(self) -> None:
        from hybrid_search.search.orchestrator import _cap_confidence_for_displayed_top
        resp = self._response([_qa_result("spliced", None, 0.06)])
        assert _cap_confidence_for_displayed_top(resp).confidence == "mixed"

    def test_revalidation_flagged_spliced_top_demotes_strong(self) -> None:
        from hybrid_search.search.orchestrator import (
            _apply_revalidation_flag,
            _cap_confidence_for_displayed_top,
        )
        top = _apply_revalidation_flag(
            _qa_result("spliced", "verified", 0.06), ("abc1234", "src/x.py"),
        )
        resp = self._response([top])
        assert _cap_confidence_for_displayed_top(resp).confidence == "mixed"

    def test_verified_top_keeps_strong(self) -> None:
        from hybrid_search.search.orchestrator import _cap_confidence_for_displayed_top
        resp = self._response([_qa_result("spliced", "verified", 0.06)])
        assert _cap_confidence_for_displayed_top(resp).confidence == "strong"

    def test_never_upgrades(self) -> None:
        from hybrid_search.search.orchestrator import _cap_confidence_for_displayed_top
        resp = self._response(
            [_qa_result("spliced", "verified", 0.06)], confidence="mixed",
        )
        assert _cap_confidence_for_displayed_top(resp).confidence == "mixed"

    def test_code_top_untouched(self) -> None:
        from hybrid_search.search.orchestrator import _cap_confidence_for_displayed_top
        resp = self._response([_code_result("top", 0.06)])
        assert _cap_confidence_for_displayed_top(resp).confidence == "strong"


# --- boost demotion -----------------------------------------------------------------

class TestRevalidationDecay:
    def test_needs_revalidation_decays_harder(self) -> None:
        fresh = _qa_result("fresh", None, 0.02)
        stale_anchor = _qa_result("stale", "needs_revalidation", 0.02)
        out = _apply_memory_boost([fresh, stale_anchor], memory_intent=False)
        by_id = {r.chunk_id: r for r in out}
        assert by_id["stale"].rrf_score == pytest.approx(0.02 * 0.6)
        assert by_id["fresh"].rrf_score > by_id["stale"].rrf_score

    def test_verification_parser(self) -> None:
        assert _memory_verification(_qa_result("x", "Verified", 0.01)) == "verified"
        assert _memory_verification(_qa_result("x", None, 0.01)) is None
