"""Pairwise acceptance rate of the strict index-time predicate over a
random sample of qa pairs — the precision proxy that group membership
reshuffling hides."""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_search.config import load_config
from hybrid_search.memory import supersession as ss
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--project", required=True)
ap.add_argument("--pairs", type=int, default=200000)
ap.add_argument("--out", required=True)
a = ap.parse_args()
cfg = load_config(Path(a.config))
reg = ProjectRegistry(cfg.global_dir)
p = reg.get_by_name(a.project)
db = StoreDB(IndexPaths(get_project_dir(cfg.projects_dir, p.id)).store_db)
chunks = [c for c in db.get_chunks_by_node_type(p.id, "qa_log")
          if not ss._is_machine_payload(c.content or "")]
db.close()
items = [ss._topic_item(c.content or "") for c in chunks]
qs = [(ss._frontmatter_value(c.content or "", "query") or "")[:80] for c in chunks]
rng = random.Random(20260909)
n = len(items)
acc = []
for _ in range(a.pairs):
    i = rng.randrange(n)
    j = rng.randrange(n)
    if i == j:
        continue
    if ss._same_topic_strict(items[i], items[j]):
        acc.append((i, j))
print(f"{a.project}: n={n} sampled={a.pairs} accepted={len(acc)} "
      f"rate={len(acc) / a.pairs * 1e4:.1f} per 10k")
json.dump({"accepted": [[qs[i], qs[j]] for i, j in acc[:400]], "count": len(acc),
           "sampled": a.pairs}, open(a.out, "w"), ensure_ascii=False)
