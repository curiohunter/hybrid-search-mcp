"""P4 — where exactly does a gold question fail?

A rate says how many questions miss; it never says why, and the review refused
to let the design widen to every memory lane on a guess about the reason. This
labels each miss by the stage that lost it:

  absent        the answer is not in this project's index at all
  pool          the parent was never retrieved — no reranking can reach it
  rank          it was retrieved and fused, but never reached the served list
  served        it WAS served and the scorer still missed it (a scoring bug)

Only `pool` is fixed by changing the retrieval unit. `rank` is an ordering
problem, and rounds 3-6 measured six ordering rules at zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search import orchestrator as O  # noqa: E402
from hybrid_search.search.fusion import reciprocal_rank_fusion  # noqa: E402

MEMORY_TYPES = ["qa_log", "memory_card", "domain_term", "episodic_example", "commit"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    emb = Embedder(cfg.embedding, cfg.models_dir)
    orch = O.SearchOrchestrator(config=cfg, registry=reg, embedder=emb)
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    pinfo = reg.get_by_path(gold["project_path"])
    conn = sqlite3.connect(
        f"file:{Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)

    labels = Counter()
    print(f"{'id':5s} {'label':8s} {'pool':>6s} {'fused':>6s}  topic")
    for q in gold["queries"]:
        phrases = q["any_of"]
        targets = {r[0] for r in conn.execute(
            "select id from chunks where project_id=?", (pinfo.id,))
            if False}  # placeholder; replaced below
        targets = set()
        for ph in phrases:
            for (cid,) in conn.execute(
                "select id from chunks where project_id=? and instr(content, ?)>0",
                    (pinfo.id, ph)):
                targets.add(cid)

        resp = orch.hybrid_search(query=q["query"], cwd=gold["project_path"],
                                  limit=args.limit)
        served = next((i for i, r in enumerate(resp.results, 1)
                       if any(ph in ((r.content or "") + (r.snippet or ""))
                              for ph in phrases)), None)
        if served:
            continue  # answered; nothing to diagnose

        if not targets:
            labels["absent"] += 1
            print(f"{q['id']:5s} {'absent':8s} {'-':>6s} {'-':>6s}  {q['topic']}")
            continue

        # Was the parent retrievable by either engine at the memory lane's depth?
        qv = emb.embed_texts([q["query"]])[0]
        depth = max(args.limit * 3, cfg.search.retrieval_depth_floor)
        b, v, *_ = orch._search_single(pinfo, q["query"], qv, max(depth, 100),
                                       None, MEMORY_TYPES, None)
        pool_rank = next((i for i, c in enumerate(b, 1) if c in targets), None)
        vec_rank = next((i for i, c in enumerate(v, 1) if c in targets), None)
        fused = reciprocal_rank_fusion(b, v, k=cfg.search.rrf_k, bm25_weight=0.15)
        fused_rank = next((i for i, f in enumerate(fused, 1)
                           if f.chunk_id in targets), None)

        if pool_rank is None and vec_rank is None:
            label = "pool"
        elif fused_rank is None:
            label = "pool"
        else:
            label = "rank"
        labels[label] += 1
        best = min([r for r in (pool_rank, vec_rank) if r] or [0]) or None
        print(f"{q['id']:5s} {label:8s} {str(best):>6s} {str(fused_rank):>6s}  {q['topic']}")

    n = len(gold["queries"])
    answered = n - sum(labels.values())
    print(f"\nanswered {answered}/{n} · " +
          " · ".join(f"{k} {v}" for k, v in labels.most_common()))


if __name__ == "__main__":
    main()
