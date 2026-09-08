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

DISTILLED = frozenset({"qa_log", "memory_card", "domain_term", "episodic_example"})
RAW = frozenset({"conv_turn"})


def lane_of(node_type: str) -> str:
    if node_type in DISTILLED:
        return "distilled"
    if node_type in RAW:
        return "raw"
    return "other"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    args = ap.parse_args()

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    project = registry.get_by_path(gold["project_path"])
    if project is None:
        raise SystemExit(f"project not registered: {gold['project_path']}")

    store = Path(config.projects_dir) / project.id / "store.db"
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT node_type, content FROM chunks WHERE project_id = ?",
        (project.id,),
    ).fetchall()
    conn.close()

    unreachable: list[str] = []
    single_lane: list[str] = []
    for q in gold["queries"]:
        print(f"\n[{q['id']} {q.get('topic','')}] {q['query']}")
        for phrase in q["any_of"]:
            by_type: Counter[str] = Counter()
            for node_type, content in rows:
                if phrase in (content or ""):
                    by_type[node_type or "?"] += 1
            lanes = {lane_of(t) for t in by_type}
            total = sum(by_type.values())
            detail = " ".join(f"{t}={n}" for t, n in by_type.most_common())
            flag = ""
            if total == 0:
                flag = "  ← UNREACHABLE"
                unreachable.append(f"{q['id']}:{phrase}")
            elif lanes <= {"distilled"} or lanes <= {"raw"}:
                flag = f"  ← single lane ({next(iter(lanes))})"
                single_lane.append(f"{q['id']}:{phrase}")
            print(f"    {phrase!r}: {total} chunks  [{detail}]{flag}")

    print(f"\nphrases unreachable: {len(unreachable)}")
    for x in unreachable:
        print(f"  {x}")
    print(f"phrases carried by one lane only: {len(single_lane)}")
    for x in single_lane:
        print(f"  {x}")


if __name__ == "__main__":
    main()
