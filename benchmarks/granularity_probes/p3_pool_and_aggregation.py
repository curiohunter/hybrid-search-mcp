"""P3 — how deep to retrieve claims, and how to score their parent.

Two decisions the review forced (2026-09-08):

* **Pool.** Cutting the claim list at 50 rows leaves 6-10 distinct parents,
  fewer than the 50 the current pipeline enriches. The fix is two-stage —
  retrieve N claims, fold, then keep 50 parents — and N has to be measured.
* **Aggregation.** "Parent = best claim" is max over children, and max grows
  with the number of children, so a long parent wins on length. Three
  candidates are compared rather than assumed.

`diversity` is the guard: distinct parents surviving into the top 50. If a
setting wins on rank while collapsing diversity it has traded one failure for
another, and the number to watch is that column, not the rank.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from hybrid_search.config import load_config  # noqa: E402
from claim_split import body_for_claims, split_claims  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.bm25 import BM25Engine  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fold(hits, parent_of, claim_count, mode: str) -> list[str]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for h in hits:
        p = parent_of.get(h.chunk_id)
        if p:
            grouped[p].append(h.score)
    scored: dict[str, float] = {}
    for p, scores in grouped.items():
        scores.sort(reverse=True)
        if mode == "max":
            scored[p] = scores[0]
        elif mode == "top3mean":
            head = scores[:3]
            scored[p] = sum(head) / len(head)
        elif mode == "max_len_norm":
            # Directly answers the max-bias objection: divide the best claim by
            # how many chances the parent had to produce one.
            scored[p] = scores[0] / (1.0 + math.log(max(claim_count.get(p, 1), 1)))
        else:
            raise SystemExit(f"unknown aggregation {mode}")
    return sorted(scored, key=lambda p: -scored[p])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--gold", required=True, action="append")
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--node-types", default="conv_turn,qa_log,memory_card")
    ap.add_argument("--depths", default="100,300,1000")
    ap.add_argument("--modes", default="max,top3mean,max_len_norm")
    ap.add_argument("--parents", type=int, default=50)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    node_types = args.node_types.split(",")
    gold_sets = [json.loads(Path(g).read_text(encoding="utf-8")) for g in args.gold]
    pinfo = reg.get_by_path(gold_sets[0]["project_path"])
    conn = sqlite3.connect(
        f"file:{Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)

    engine = BM25Engine(Path(args.scratch) / "claim_bm25", read_only=True)
    parent_of: dict[str, str] = {}
    parent_text: dict[str, str] = {}
    claim_count: dict[str, int] = {}
    q = ("select id, node_type, content from chunks where project_id=? "
         f"and node_type in ({','.join('?' * len(node_types))})")
    for cid, node_type, content in conn.execute(q, (pinfo.id, *node_types)):
        parent_text[cid] = content or ""
        n = len(split_claims(body_for_claims(content or "", node_type)))
        claim_count[cid] = n
        for i in range(n):
            parent_of[f"{cid}#c{i:03d}"] = cid
    print(f"claims {len(parent_of):,} · parents {len(parent_text):,} "
          f"· engine rows {engine.count:,}\n")

    for gold in gold_sets:
        n = len(gold["queries"])
        print(f"=== set n={n} ===")
        print(f"{'depth':>6s} {'aggregation':13s} {'top3':>16s} {'top10':>6s} "
              f"{'MRR':>6s} {'parents/query':>14s}")
        for depth in (int(d) for d in args.depths.split(",")):
            for mode in args.modes.split(","):
                ranks, diversity = [], []
                for query in gold["queries"]:
                    hits = engine.search(query["query"], limit=depth)
                    ranked = fold(hits, parent_of, claim_count, mode)[: args.parents]
                    diversity.append(len(ranked))
                    ranks.append(next(
                        (i for i, p in enumerate(ranked, 1)
                         if any(ph in parent_text.get(p, "")
                                for ph in query["any_of"])), None))
                found = [r for r in ranks if r]
                top3 = sum(1 for r in found if r <= 3)
                top10 = sum(1 for r in found if r <= 10)
                lo, hi = wilson(top3, n)
                print(f"{depth:>6d} {mode:13s} {top3/n:>6.2f} [{lo:.2f},{hi:.2f}] "
                      f"{top10/n:>6.2f} {sum(1.0/r for r in found)/n:>6.3f} "
                      f"{sum(diversity)/n:>14.1f}")
        print()


if __name__ == "__main__":
    main()
