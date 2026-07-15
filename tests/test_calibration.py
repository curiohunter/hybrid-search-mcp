"""P1-3 tests — calibration metrics for the confidence contract.

The point of the module: precision AND coverage together (no
never-say-strong gaming), ECE/Brier against the published nominal
values, per-slice breakdowns, and hard gates for the word "calibrated".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_search.eval.calibration import (
    GATE_STRONG_COVERAGE,
    GATE_STRONG_PRECISION,
    NOMINAL,
    compute_report,
    load_rows,
    render_markdown,
)


def _rows(spec: list[tuple[str, bool]], answerable: bool = True) -> list[dict]:
    return [
        {"confidence": label, "correct": correct, "answerable": answerable}
        for label, correct in spec
    ]


class TestPerLabel:
    def test_precision_and_counts(self) -> None:
        report = compute_report(_rows([
            ("strong", True), ("strong", True), ("strong", False),
            ("mixed", True), ("weak", False),
        ]))
        strong = report.per_label["strong"]
        assert (strong.n, strong.correct) == (3, 2)
        assert strong.precision == pytest.approx(2 / 3)

    def test_empty_input(self) -> None:
        report = compute_report([])
        assert report.n_rows == 0 and report.ece is None

    def test_unknown_label_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_report([{"confidence": "high", "correct": True}])

    def test_missing_correct_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_report([{"confidence": "strong"}])


class TestCalibrationStats:
    def test_perfectly_nominal_strong_has_low_ece(self) -> None:
        # 19/20 strong correct ≈ nominal 0.95 → ECE ~0
        spec = [("strong", True)] * 19 + [("strong", False)]
        report = compute_report(_rows(spec))
        assert report.ece == pytest.approx(0.0, abs=1e-6)

    def test_overconfident_strong_raises_ece(self) -> None:
        report = compute_report(_rows([("strong", False)] * 10))
        assert report.ece == pytest.approx(NOMINAL["strong"])

    def test_brier_definition(self) -> None:
        report = compute_report(_rows([("strong", True), ("weak", False)]))
        expected = ((0.95 - 1.0) ** 2 + (0.20 - 0.0) ** 2) / 2
        assert report.brier == pytest.approx(expected, abs=1e-4)

    def test_coverage_risk_is_cumulative(self) -> None:
        report = compute_report(_rows([
            ("strong", True), ("strong", True),
            ("mixed", True), ("mixed", False),
            ("weak", False), ("weak", False),
        ]))
        by_tier = {p["answer_at"]: p for p in report.coverage_risk}
        tol = 1e-4  # report values are rounded to 4 decimals
        assert by_tier["strong"]["coverage"] == pytest.approx(2 / 6, abs=tol)
        assert by_tier["strong"]["risk"] == pytest.approx(0.0, abs=tol)
        assert by_tier["mixed"]["coverage"] == pytest.approx(4 / 6, abs=tol)
        assert by_tier["mixed"]["risk"] == pytest.approx(1 / 4, abs=tol)
        assert by_tier["weak"]["coverage"] == pytest.approx(1.0, abs=tol)
        assert by_tier["weak"]["risk"] == pytest.approx(3 / 6, abs=tol)


class TestGates:
    def test_never_say_strong_fails_the_coverage_gate(self) -> None:
        """The exact gaming the gate exists to prevent: false-strong 0
        by never saying strong."""
        report = compute_report(_rows([("mixed", True)] * 10))
        assert report.per_label["strong"].n == 0
        assert report.gates["strong_coverage_ok"] is False
        assert report.gates["calibrated_claim_allowed"] is False

    def test_both_gates_pass(self) -> None:
        spec = [("strong", True)] * 5 + [("mixed", True)] * 5
        report = compute_report(_rows(spec))
        assert report.strong_coverage == pytest.approx(0.5)
        assert report.gates["calibrated_claim_allowed"] is True

    def test_low_precision_fails_even_with_coverage(self) -> None:
        spec = [("strong", True), ("strong", False)] * 5
        report = compute_report(_rows(spec))
        assert report.gates["strong_precision_ok"] is False
        assert report.gates["calibrated_claim_allowed"] is False

    def test_coverage_counts_answerable_only(self) -> None:
        rows = _rows([("strong", True)] * 2) + _rows(
            [("weak", True)] * 8, answerable=False,
        )
        report = compute_report(rows)
        # 2 answerable rows, both strong → coverage 1.0, not 0.2.
        assert report.strong_coverage == pytest.approx(1.0)


class TestSlices:
    def test_slice_breakdown(self) -> None:
        rows = [
            {"confidence": "strong", "correct": True, "corpus": "ripgrep", "language": "en"},
            {"confidence": "strong", "correct": False, "corpus": "valuein", "language": "ko"},
        ]
        report = compute_report(rows)
        assert report.slices["corpus=ripgrep"].per_label["strong"].precision == 1.0
        assert report.slices["corpus=valuein"].per_label["strong"].precision == 0.0
        assert "language=ko" in report.slices


class TestIO:
    def test_load_jsonl_and_array(self, tmp_path: Path) -> None:
        rows = [{"confidence": "strong", "correct": True}]
        jsonl = tmp_path / "rows.jsonl"
        jsonl.write_text(json.dumps(rows[0]) + "\n")
        assert load_rows(jsonl) == rows
        arr = tmp_path / "rows.json"
        arr.write_text(json.dumps(rows))
        assert load_rows(arr) == rows

    def test_markdown_renders_gates(self) -> None:
        report = compute_report(_rows([("strong", True)] * 5 + [("mixed", True)] * 5))
        md = render_markdown(report)
        assert "calibrated' claim allowed: YES" in md
        assert f"strong precision ≥ {GATE_STRONG_PRECISION}" in md
        assert f"strong coverage ≥ {GATE_STRONG_COVERAGE}" in md
