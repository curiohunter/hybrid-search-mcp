"""Evaluate heuristic prompt router classification accuracy."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.memory.router import classify_prompt  # noqa: E402


def _load_gold(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON list")
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("benchmarks/router_gold.json"))
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    rows = []
    total = correct = 0
    by_class: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for item in _load_gold(args.gold):
        prompt = item.get("prompt", "")
        expected = item.get("expected_tool", "")
        decision = classify_prompt(prompt)
        matched = decision.tool == expected
        total += 1
        correct += int(matched)
        by_class[expected]["total"] += 1
        by_class[expected]["correct"] += int(matched)
        rows.append({
            "id": item.get("id"),
            "expected_tool": expected,
            "actual_tool": decision.tool,
            "reason": decision.reason,
            "matched": matched,
        })

    breakdown = {
        tool: {
            "correct": counts["correct"],
            "total": counts["total"],
            "accuracy": round(counts["correct"] / counts["total"], 4) if counts["total"] else 0.0,
        }
        for tool, counts in sorted(by_class.items())
    }
    accuracy = correct / total if total else 0.0
    report = {
        "gold": str(args.gold),
        "accuracy": round(accuracy, 4),
        "target": 0.80,
        "passed": accuracy >= 0.80,
        "correct": correct,
        "total": total,
        "by_class": breakdown,
        "rows": rows,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"router accuracy: {accuracy:.1%} ({correct}/{total}, target >= 80%)")
        for tool, counts in breakdown.items():
            print(
                f"  {tool}: {counts['accuracy']:.1%} "
                f"({counts['correct']}/{counts['total']})"
            )

    if not report["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
