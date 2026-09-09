"""P6b — does decomposition fix the miss, or move it?

Round-2 review, point #4: P4 labelled most Set B misses `pool` at chunk level,
and P2 then showed a decomposed index retrieves many of those parents. If the
parent now arrives but still does not reach the head, the design has converted
a retrieval failure into a ranking failure and claimed a win for it.

Cross-tabs, per question: the chunk-level label against where the parent lands
after decomposition and folding.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.bm25 import BM25Engine  # noqa: E402

_SENT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")
PARENTS = ("conv_turn", "qa_log", "memory_card")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--scratch", required=True, help="dir holding abl_sentence")
    ap.add_argument("--depth", type=int, default=100)
    ap.add_argument("--parents", type=int, default=50)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    pinfo = reg.get_by_path(gold["project_path"])
    conn = sqlite3.connect(
        f"file:{Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)
    rows = conn.execute(
        "select id, node_type, content from chunks where project_id=? "
        f"and node_type in ({','.join('?'*len(PARENTS))})",
        (pinfo.id, *PARENTS)).fetchall()
    parent_text = {r[0]: r[2] or "" for r in rows}
    parent_of: dict[str, str] = {}
    for cid, _nt, content in rows:
        i = 0
        for s in _SENT_RE.split(content or ""):
            s = s.strip()
            if 12 <= len(s) <= 400 and re.search(r"[가-힣A-Za-z]", s):
                parent_of[f"{cid}#u{i:03d}"] = cid
                i += 1

    engine = BM25Engine(Path(args.scratch) / "abl_sentence", read_only=True)
    chunk_engine = BM25Engine(
        Path(cfg.projects_dir) / pinfo.id / "tantivy", read_only=True)

    tab = Counter()
    print(f"{'id':5s} {'chunk-level':13s} {'after-decomp':13s}")
    for q in gold["queries"]:
        phrases = q["any_of"]
        # chunk level: is the parent anywhere in a deep BM25 pool?
        ch = chunk_engine.search(q["query"], limit=300)
        chunk_rank = next((i for i, h in enumerate(ch, 1)
                           if any(p in parent_text.get(h.chunk_id, "")
                                  for p in phrases)), None)
        chunk_label = ("miss" if chunk_rank is None
                       else "top3" if chunk_rank <= 3 else "pooled")

        hits = engine.search(q["query"], limit=args.depth)
        best: dict[str, float] = {}
        for h in hits:
            p = parent_of.get(h.chunk_id)
            if p and p not in best:
                best[p] = h.score
        ranked = sorted(best, key=lambda p: -best[p])[: args.parents]
        r = next((i for i, p in enumerate(ranked, 1)
                  if any(ph in parent_text.get(p, "") for ph in phrases)), None)
        after = ("miss" if r is None else "top3" if r <= 3 else f"pooled@{r}")
        tab[(chunk_label, after.split("@")[0])] += 1
        print(f"{q['id']:5s} {chunk_label:13s} {after:13s}")

    print("\n교차표 (chunk-level → after decomposition)")
    for (a, b), n in sorted(tab.items()):
        print(f"  {a:7s} → {b:7s}  {n}")


if __name__ == "__main__":
    main()
