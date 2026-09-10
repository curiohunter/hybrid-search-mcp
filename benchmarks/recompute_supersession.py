"""Recompute the qa supersession mapping inside a frozen snapshot.

The mapping is persisted at INDEX time, so a topic-matcher change is
invisible to a snapshot built before it. This is pure CPU over qa
content already in the store — no embeddings, no reindex.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from hybrid_search.config import load_config
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir
from hybrid_search.memory.supersession import compute_supersession

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--project", action="append", default=None)
args = ap.parse_args()

cfg = load_config(Path(args.config))
reg = ProjectRegistry(cfg.global_dir)
names = args.project or [p.name for p in reg.list_all()]
for name in names:
    pinfo = reg.get_by_name(name)
    if pinfo is None:
        print(f"{name}: not registered")
        continue
    idx = IndexPaths(get_project_dir(cfg.projects_dir, pinfo.id))
    if not idx.store_db.exists():
        continue
    db = StoreDB(idx.store_db)
    try:
        chunks = db.get_chunks_by_node_type(pinfo.id, "qa_log")
        with db.transaction() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM qa_supersession WHERE project_id = ?",
                (pinfo.id,),
            ).fetchone()[0]
        mapping = compute_supersession([(c.id, c.content or "") for c in chunks])
        with db.transaction() as conn:
            db.replace_qa_supersession(conn, pinfo.id, mapping)
        print(f"{name}: qa={len(chunks)}  supersession {before} -> {len(mapping)}")
    finally:
        db.close()
