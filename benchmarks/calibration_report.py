"""CLI wrapper for the confidence calibration report (P1-3).

Usage:
    python benchmarks/calibration_report.py --input rows.jsonl [--json]

Input rows (JSONL or JSON array):
    {"confidence": "strong", "correct": true, "answerable": true,
     "corpus": "ripgrep", "language": "en"}

The metrics library lives in ``hybrid_search.eval.calibration`` (unit
tested); this file is I/O only. The gates printed at the bottom are the
public contract: the docs may say "calibrated confidence" only while
both PASS on the newest frozen holdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.eval.calibration import (  # noqa: E402
    compute_report,
    load_rows,
    render_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path,
                        help="JSONL/JSON rows: confidence, correct, answerable, corpus, language")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    parser.add_argument("--out", type=Path, help="Write to file instead of stdout")
    args = parser.parse_args()

    report = compute_report(load_rows(args.input))
    rendered = (
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
        if args.json else render_markdown(report)
    )
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(rendered)
    return 0 if report.gates.get("calibrated_claim_allowed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
