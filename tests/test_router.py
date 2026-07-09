from __future__ import annotations

import pytest

from hybrid_search.memory.router import classify_confidence, classify_prompt, fallback_hint


THRESHOLDS = {
    "strong_score": 0.05,
    "strong_gap": 0.01,
    "weak_score": 0.02,
}


class TestClassifyConfidence:
    def test_zero_hits_is_weak(self) -> None:
        assert classify_confidence(0.0, None, THRESHOLDS) == "weak"

    def test_single_hit_can_be_mixed(self) -> None:
        assert classify_confidence(0.03, None, THRESHOLDS) == "mixed"

    def test_tie_with_modest_top_is_weak(self) -> None:
        # Relative tie (gap/top < 2%) with a top below strong_score → weak.
        assert classify_confidence(0.03, 0.0004, THRESHOLDS) == "weak"

    def test_tie_with_excellent_top_is_mixed(self) -> None:
        # A near-tie under an excellent top hit is not a failed ranking —
        # the old absolute floor (gap < 0.001) forced weak here.
        assert classify_confidence(0.08, 0.0009, THRESHOLDS) == "mixed"

    def test_coherent_tie_is_mixed(self) -> None:
        # Near-tie among one subsystem = several good answers.
        assert classify_confidence(0.03, 0.0004, THRESHOLDS, coherent=True) == "mixed"

    def test_gap_is_relative_not_absolute(self) -> None:
        # gap 0.0009 is 3% of top 0.03 — above the 2% tie ratio, so the old
        # absolute floor must not fire.
        assert classify_confidence(0.03, 0.0009, THRESHOLDS) == "mixed"

    def test_normal_strong(self) -> None:
        assert classify_confidence(0.08, 0.02, THRESHOLDS) == "strong"

    def test_normal_mixed(self) -> None:
        assert classify_confidence(0.03, 0.005, THRESHOLDS) == "mixed"

    def test_normal_weak(self) -> None:
        assert classify_confidence(0.01, 0.005, THRESHOLDS) == "weak"


class TestFallbackHint:
    @pytest.mark.parametrize(
        "prompt",
        [
            "`paid_fee_guard` 어디 있어?",
            "TuitionChargeSection 컴포넌트",
            "src/app/router.py 파일",
            "왜 Traceback 발생하지?",
            "Error: missing relation",
        ],
    )
    def test_identifier_shapes_choose_grep(self, prompt: str) -> None:
        assert "-> grep `" in fallback_hint(prompt)

    def test_non_identifier_prompt_chooses_wiki(self) -> None:
        assert "-> wiki `" in fallback_hint("수강료 정산 시스템은 어떻게 구성되어 있나")


class TestClassifyPrompt:
    @pytest.mark.parametrize(
        ("prompt", "reason"),
        [
            ("왜 `paid_fee_guard`가 안 되지", "exact identifier"),
            ("TuitionChargeSection 렌더링 위치", "exact identifier"),
            ("*.ts 파일에서 찾아줘", "file path"),
            ("src/app/dashboard/page.tsx 확인", "file path"),
            ("Error: missing relation Traceback 확인", "error trace"),
        ],
    )
    def test_grep_signals(self, prompt: str, reason: str) -> None:
        decision = classify_prompt(prompt)
        assert decision.tool == "grep"
        assert decision.reason == reason

    @pytest.mark.parametrize(
        "prompt",
        [
            "지난번 결정 다시 보여줘",
            "이전에 왜 이렇게 결정했지",
            "what did we do last time for billing",
        ],
    )
    def test_memory_signals(self, prompt: str) -> None:
        decision = classify_prompt(prompt)
        assert decision.tool == "memory"
        assert decision.reason == "history reference"

    @pytest.mark.parametrize(
        "prompt",
        [
            "수강료 정산이 어떻게 흘러가?",
            "왜 자꾸 결제가 취소되지",
            "how does billing work",
        ],
    )
    def test_hybrid_search_signals(self, prompt: str) -> None:
        decision = classify_prompt(prompt)
        assert decision.tool == "hybrid_search"
        assert decision.reason == "exploratory NL"

    def test_default_fallback(self) -> None:
        decision = classify_prompt("please review the recent dashboard behavior")
        assert decision.tool == "hybrid_search"
        assert decision.reason == "default"

    def test_grep_beats_exploratory_signal(self) -> None:
        decision = classify_prompt("왜 `paid_fee_guard`가 안 되지")
        assert decision.tool == "grep"
        assert decision.reason == "exact identifier"
