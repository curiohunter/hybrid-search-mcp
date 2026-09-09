"""Lane-composition check for a conversational gold set.

``run_conv_bench.py`` scores whether a gold phrase reaches the top of the
results. That number only means something if the phrase was reachable from
more than one lane to begin with: a set whose phrases exist *only* inside
distilled notes will reward any change that promotes notes, and a set whose
phrases exist only in raw turns will reward the opposite. Neither would be
measuring ranking.

So before trusting a run, count where each phrase actually lives:

    python benchmarks/verify_conv_gold.py --gold <your gold set>

For every phrase it reports the number of chunks containing it, broken down
by node_type, and flags phrases that no lane carries (a typo, or a fact that
left the corpus) and phrases that a single lane monopolises (the set is
assuming its conclusion for that row). The gold set itself is not committed —
see the module docstring of ``run_conv_bench.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import sqlite3  # noqa: E402

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402

# A lane is not a node_type. ``qa_log`` covers two very different things and
# conflating them made this checker lie: a stop-hook log under ``qa/YYYY/MM/``
# is a near-verbatim copy of the turn it recorded, while ``qa/consolidated/`` is
# Reflector output that restates many turns in new words. Counting the former as
# "distilled" marked raw-only facts as lane-mixed, because the same sentence is
# in the transcript AND in the log written from it — by construction, for every
# turn the quality gate let through.
DISTILLED_TYPES = frozenset({"memory_card", "domain_term", "episodic_example"})
RAW_TYPES = frozenset({"conv_turn"})
_CONSOLIDATED = "/consolidated/"
_QA_LOG_DIR = ".hybrid-search/qa/"


def lane_of(node_type: str, path: str) -> str:
    if node_type in DISTILLED_TYPES:
        return "distilled"
    if node_type == "qa_log":
        return "distilled" if _CONSOLIDATED in (path or "") else "raw"
    if node_type in RAW_TYPES:
        return "raw"
    return "other"


def label_of(node_type: str, path: str) -> str:
    """node_type as reported, with the qa split made visible."""
    if node_type == "qa_log":
        return "qa_consolidated" if _CONSOLIDATED in (path or "") else "qa_turnlog"
    return node_type or "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument(
        "--config",
        default=None,
        help="Path to a hybrid-search config.toml. Use the same frozen snapshot "
             "the benchmark runs against — verifying a gold set against a live "
             "index while measuring against a snapshot compares two corpora.",
    )
    args = ap.parse_args()

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    config = load_config(Path(args.config) if args.config else None)
    registry = ProjectRegistry(config.global_dir)
    project = registry.get_by_path(gold["project_path"])
    if project is None:
        raise SystemExit(f"project not registered: {gold['project_path']}")

    store = Path(config.projects_dir) / project.id / "store.db"
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT c.node_type, f.relative_path, c.content "
        "FROM chunks c LEFT JOIN files f ON f.id = c.file_id "
        "WHERE c.project_id = ?",
        (project.id,),
    ).fetchall()
    conn.close()

    unreachable: list[str] = []
    single_lane: list[str] = []
    mixed: list[str] = []
    for q in gold["queries"]:
        print(f"\n[{q['id']} {q.get('topic','')}] {q['query']}")
        for phrase in q["any_of"]:
            by_type: Counter[str] = Counter()
            lanes: set[str] = set()
            for node_type, path, content in rows:
                if phrase in (content or ""):
                    by_type[label_of(node_type, path)] += 1
                    lanes.add(lane_of(node_type, path))
            total = sum(by_type.values())
            detail = " ".join(f"{t}={n}" for t, n in by_type.most_common())
            flag = ""
            if total == 0:
                flag = "  ← UNREACHABLE"
                unreachable.append(f"{q['id']}:{phrase}")
            elif lanes <= {"distilled"} or lanes <= {"raw"}:
                flag = f"  ← single lane ({next(iter(lanes))})"
                single_lane.append(f"{q['id']}:{phrase}")
            else:
                mixed.append(f"{q['id']}:{phrase} [{detail}]")
            print(f"    {phrase!r}: {total} chunks  [{detail}]{flag}")

    print(f"\nphrases unreachable: {len(unreachable)}")
    for x in unreachable:
        print(f"  {x}")
    print(f"phrases carried by one lane only: {len(single_lane)}")
    print(f"phrases in BOTH lanes: {len(mixed)}")
    for x in mixed:
        print(f"  {x}")


if __name__ == "__main__":
    main()
