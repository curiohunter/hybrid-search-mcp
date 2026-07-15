"""Evaluate the qa topic matcher against benchmarks/topic_gold_set.json.

Two modes:

  eval  (default) — score the current thresholds per lang×relation slice.
        Exit code 1 if any adjacent/bridge slice has a false group or a
        non-limitation same slice is below its floor.

  sweep — grid-search (query_overlap, answer_overlap) under the hard
        constraint of ZERO false groups on every adjacent and bridge
        slice, maximizing same-topic recall. Prints the frontier; the
        chosen constants land in src/hybrid_search/search/qa_topics.py.

The gate floors are deliberately not 100% on `same`: the matcher stays
conservative because a false group is the worse failure (a fresh
adjacent answer steals an old exact answer's guaranteed slot).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.search import qa_topics  # noqa: E402
from hybrid_search.search.qa_topics import topic_tokens  # noqa: E402

GOLD = Path(__file__).parent / "topic_gold_set.json"

# Pass floors per relation (fraction of the slice). Adjacent/bridge are
# hard zero-tolerance; same has a recall floor per language.
SAME_FLOOR = {"ko": 0.90, "en": 0.85, "mixed": 0.85}


def _pair_tokens(side: dict) -> tuple[dict, dict]:
    return (topic_tokens(side["query"]), topic_tokens(side["answer"]))


def evaluate(pairs: list[dict]) -> dict:
    slices: dict[tuple[str, str], dict] = {}
    failures: list[str] = []
    limitations: list[tuple[str, bool]] = []
    for p in pairs:
        lang, rel = p["lang"], p["relation"]
        if p.get("known_limitation"):
            got = qa_topics.same_topic(_pair_tokens(p["a"]), _pair_tokens(p["b"]))
            limitations.append((p["id"], got))
            continue
        s = slices.setdefault((lang, rel), {"pass": 0, "total": 0})
        s["total"] += 1
        if rel == "bridge":
            items = [_pair_tokens(i) for i in p["items"]]
            groups = qa_topics.topic_group_indices(items)
            ok = not any(0 in g and 2 in g for g in groups)
        else:
            got = qa_topics.same_topic(_pair_tokens(p["a"]), _pair_tokens(p["b"]))
            ok = got == (rel == "same")
        s["pass"] += ok
        if not ok:
            failures.append(p["id"])
    return {"slices": slices, "failures": failures, "limitations": limitations}


def _gate(report: dict) -> tuple[bool, list[str]]:
    problems = []
    for (lang, rel), s in sorted(report["slices"].items()):
        if rel in ("adjacent", "bridge") and s["pass"] != s["total"]:
            problems.append(f"{lang}/{rel}: {s['pass']}/{s['total']} — false group(s), hard fail")
        if rel == "same" and s["pass"] < SAME_FLOOR[lang] * s["total"]:
            problems.append(
                f"{lang}/same: {s['pass']}/{s['total']} below floor {SAME_FLOOR[lang]:.0%}"
            )
    return (not problems, problems)


def cmd_eval() -> int:
    pairs = json.loads(GOLD.read_text())["pairs"]
    report = evaluate(pairs)
    print(f"{'slice':16} pass/total")
    for (lang, rel), s in sorted(report["slices"].items()):
        print(f"{lang + '/' + rel:16} {s['pass']}/{s['total']}")
    if report["failures"]:
        print("failed ids:", ", ".join(report["failures"]))
    for pid, got in report["limitations"]:
        print(f"known-limitation {pid}: grouped={got} (excluded from gate)")
    ok, problems = _gate(report)
    for p in problems:
        print("GATE FAIL:", p)
    print("gate:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_sweep() -> int:
    pairs = json.loads(GOLD.read_text())["pairs"]
    grid_q = [round(0.20 + 0.02 * i, 2) for i in range(16)]  # 0.20..0.50
    grid_a = [round(0.10 + 0.02 * i, 2) for i in range(16)]  # 0.10..0.40
    rows = []
    for q_thr in grid_q:
        for a_thr in grid_a:
            qa_topics._QUERY_OVERLAP = q_thr
            qa_topics._ANSWER_OVERLAP = a_thr
            report = evaluate(pairs)
            false_groups = sum(
                s["total"] - s["pass"]
                for (_, rel), s in report["slices"].items()
                if rel in ("adjacent", "bridge")
            )
            same_pass = sum(
                s["pass"] for (_, rel), s in report["slices"].items() if rel == "same"
            )
            same_total = sum(
                s["total"] for (_, rel), s in report["slices"].items() if rel == "same"
            )
            rows.append((false_groups, -same_pass, q_thr, a_thr, same_pass, same_total))
    rows.sort()
    print("false_groups  same_recall  q_thr  a_thr")
    for fg, _, q_thr, a_thr, sp, st in rows[:15]:
        print(f"{fg:12}  {sp}/{st:9}  {q_thr:.2f}  {a_thr:.2f}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="eval", choices=["eval", "sweep"])
    args = ap.parse_args()
    sys.exit(cmd_eval() if args.mode == "eval" else cmd_sweep())


if __name__ == "__main__":
    main()
