from __future__ import annotations

import pytest

from hybrid_search.memory.router import classify_confidence, fallback_hint


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

    def test_tie_is_weak(self) -> None:
        assert classify_confidence(0.08, 0.0009, THRESHOLDS) == "weak"

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
