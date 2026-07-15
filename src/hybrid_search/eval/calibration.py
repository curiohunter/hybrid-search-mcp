"""Calibration metrics for the confidence contract (P1-3).

"False-strong 0/N" alone can be gamed by never saying strong — the
cleanrepro run showed present-confidence sitting at 4/4 *mixed*. To use
the word "calibrated" honestly, precision and COVERAGE must be reported
together, plus the standard calibration statistics, sliced by corpus and
language so a KO-tuned threshold can't hide an EN regression.

Input rows (one per evaluated query):

    {"confidence": "strong"|"mixed"|"weak",
     "correct": bool,          # did the top answer actually answer it
     "answerable": bool,       # does the corpus contain an answer at all
     "corpus": str, "language": str}   # optional slice keys

For unanswerable rows ``correct`` means "the system abstained" (weak +
fallback). The gates below are the CONTRACT: marketing may say
"calibrated confidence" only while both hold on the newest holdout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LABELS = ("strong", "mixed", "weak")

# Nominal trust each label claims. These ARE the contract numbers the
# docs advertise — ECE/Brier are measured against them, so changing one
# here changes what "calibrated" means publicly.
NOMINAL = {"strong": 0.95, "mixed": 0.60, "weak": 0.20}

# Release gates (spec P1-3 CC-T3). Both must hold — precision without
# coverage is the never-say-strong game; coverage without precision is
# false confidence.
GATE_STRONG_PRECISION = 0.95
GATE_STRONG_COVERAGE = 0.20


@dataclass
class LabelStats:
    n: int = 0
    correct: int = 0

    @property
    def precision(self) -> float | None:
        return self.correct / self.n if self.n else None


@dataclass
class CalibrationReport:
    per_label: dict[str, LabelStats] = field(default_factory=dict)
    ece: float | None = None
    brier: float | None = None
    strong_coverage: float | None = None   # share of answerable rows labeled strong
    coverage_risk: list[dict[str, float]] = field(default_factory=list)
    gates: dict[str, bool] = field(default_factory=dict)
    slices: dict[str, "CalibrationReport"] = field(default_factory=dict)
    n_rows: int = 0
    n_answerable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_answerable": self.n_answerable,
            "per_label": {
                label: {"n": s.n, "correct": s.correct, "precision": s.precision}
                for label, s in self.per_label.items()
            },
            "ece": self.ece,
            "brier": self.brier,
            "strong_coverage": self.strong_coverage,
            "coverage_risk": self.coverage_risk,
            "gates": self.gates,
            "slices": {k: v.to_dict() for k, v in self.slices.items()},
        }


def _validate(row: dict[str, Any]) -> tuple[str, bool, bool]:
    label = str(row.get("confidence", "")).lower()
    if label not in LABELS:
        raise ValueError(f"unknown confidence label: {row.get('confidence')!r}")
    if not isinstance(row.get("correct"), bool):
        raise ValueError(f"row missing boolean 'correct': {row!r}")
    answerable = row.get("answerable", True)
    if not isinstance(answerable, bool):
        raise ValueError(f"'answerable' must be boolean: {row!r}")
    return label, row["correct"], answerable


def compute_report(
    rows: Iterable[dict[str, Any]],
    *,
    slice_keys: tuple[str, ...] = ("corpus", "language"),
    _slice: bool = True,
) -> CalibrationReport:
    rows = list(rows)
    report = CalibrationReport(
        per_label={label: LabelStats() for label in LABELS},
        n_rows=len(rows),
    )
    if not rows:
        return report

    sq_err = 0.0
    answerable_n = 0
    answerable_strong = 0
    for row in rows:
        label, correct, answerable = _validate(row)
        stats = report.per_label[label]
        stats.n += 1
        stats.correct += int(correct)
        sq_err += (NOMINAL[label] - (1.0 if correct else 0.0)) ** 2
        if answerable:
            answerable_n += 1
            if label == "strong":
                answerable_strong += 1

    report.n_answerable = answerable_n
    report.brier = round(sq_err / len(rows), 4)
    report.ece = round(
        sum(
            (s.n / len(rows)) * abs((s.precision or 0.0) - NOMINAL[label])
            for label, s in report.per_label.items()
            if s.n
        ),
        4,
    )
    report.strong_coverage = (
        round(answerable_strong / answerable_n, 4) if answerable_n else None
    )

    # Coverage-risk curve: answer only at >= each label tier.
    cum_n = 0
    cum_correct = 0
    for label in LABELS:
        s = report.per_label[label]
        cum_n += s.n
        cum_correct += s.correct
        if cum_n:
            report.coverage_risk.append({
                "answer_at": label,
                "coverage": round(cum_n / len(rows), 4),
                "risk": round(1.0 - cum_correct / cum_n, 4),
            })

    strong = report.per_label["strong"]
    report.gates = {
        "strong_precision_ok": (
            strong.precision is not None and strong.precision >= GATE_STRONG_PRECISION
        ),
        "strong_coverage_ok": (
            report.strong_coverage is not None
            and report.strong_coverage >= GATE_STRONG_COVERAGE
        ),
    }
    report.gates["calibrated_claim_allowed"] = (
        report.gates["strong_precision_ok"] and report.gates["strong_coverage_ok"]
    )

    if _slice:
        for key in slice_keys:
            values = sorted({str(r[key]) for r in rows if r.get(key)})
            for value in values:
                subset = [r for r in rows if str(r.get(key, "")) == value]
                report.slices[f"{key}={value}"] = compute_report(
                    subset, _slice=False,
                )
    return report


def load_rows(path: Path) -> list[dict[str, Any]]:
    """JSONL (one row per line) or a JSON array."""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a JSON array")
        return data
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def render_markdown(report: CalibrationReport) -> str:
    lines = [
        "# Confidence calibration report",
        "",
        f"- rows: {report.n_rows} ({report.n_answerable} answerable)",
        f"- ECE: {report.ece}  |  Brier: {report.brier}",
        f"- strong coverage (answerable): {report.strong_coverage}",
        "",
        "| label | n | precision | nominal |",
        "|---|---:|---:|---:|",
    ]
    for label in LABELS:
        s = report.per_label[label]
        precision = f"{s.precision:.3f}" if s.precision is not None else "—"
        lines.append(f"| {label} | {s.n} | {precision} | {NOMINAL[label]} |")
    lines += ["", "| answer at | coverage | risk |", "|---|---:|---:|"]
    for point in report.coverage_risk:
        lines.append(
            f"| ≥{point['answer_at']} | {point['coverage']:.3f} | {point['risk']:.3f} |"
        )
    lines += [
        "",
        "## Gates",
        "",
        f"- strong precision ≥ {GATE_STRONG_PRECISION}: "
        f"{'PASS' if report.gates.get('strong_precision_ok') else 'FAIL'}",
        f"- strong coverage ≥ {GATE_STRONG_COVERAGE}: "
        f"{'PASS' if report.gates.get('strong_coverage_ok') else 'FAIL'}",
        f"- **'calibrated' claim allowed: "
        f"{'YES' if report.gates.get('calibrated_claim_allowed') else 'NO'}**",
    ]
    if report.slices:
        lines += ["", "## Slices", ""]
        lines += ["| slice | n | strong precision | strong coverage | ece |",
                  "|---|---:|---:|---:|---:|"]
        for name, sub in report.slices.items():
            sp = sub.per_label["strong"].precision
            lines.append(
                f"| {name} | {sub.n_rows} | "
                f"{f'{sp:.3f}' if sp is not None else '—'} | "
                f"{sub.strong_coverage if sub.strong_coverage is not None else '—'} | "
                f"{sub.ece} |"
            )
    return "\n".join(lines) + "\n"
