"""P5 — does inserting claim rows into the shared index damage the code lane?

`BM25Engine` is one index per project holding every node_type, so adding tens
of thousands of short claims moves `avgdl`, and BM25's length normalisation is
computed against it: every existing document's score shifts. The vector store
is shared the same way. That is a cost the design has to pay before it is
allowed to ship, and it is measured here rather than argued.

This builds an augmented COPY of a frozen snapshot — production is untouched —
with claims present in SQLite and tantivy. Claims get no vectors: P2/P3 showed
the gain is largely lexical, so BM25-only insertion is the candidate design,
not a shortcut. Then the code-axis gold runs against the copy.

Worst case on purpose: nothing folds claims into parents here, so they compete
with code chunks unfiltered. If the code axis survives that, it survives the
real design.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from hybrid_search.config import load_config  # noqa: E402
from claim_split import body_for_claims, split_claims  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.bm25 import BM25Engine  # noqa: E402

PARENT_TYPES = ("conv_turn", "qa_log", "memory_card")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="frozen snapshot config")
    ap.add_argument("--out", required=True, help="directory for the augmented copy")
    ap.add_argument("--project", required=True)
    args = ap.parse_args()

    src_cfg = load_config(Path(args.config))
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(Path(src_cfg.projects_dir), out / "projects")
    shutil.copytree(Path(src_cfg.global_dir), out / "global")
    cfg_text = Path(args.config).read_text(encoding="utf-8")
    new_cfg = out / "config.toml"
    new_cfg.write_text(
        cfg_text.replace(str(src_cfg.data_dir), str(out)), encoding="utf-8")

    cfg = load_config(new_cfg)
    pinfo = ProjectRegistry(cfg.global_dir).get_by_path(args.project)
    store = Path(cfg.projects_dir) / pinfo.id / "store.db"
    conn = sqlite3.connect(store)

    before = conn.execute(
        "select count(*) from chunks where project_id=?", (pinfo.id,)).fetchone()[0]
    rows = conn.execute(
        "select id, file_id, node_type, content from chunks "
        f"where project_id=? and node_type in ({','.join('?'*len(PARENT_TYPES))})",
        (pinfo.id, *PARENT_TYPES)).fetchall()

    engine = BM25Engine(Path(cfg.projects_dir) / pinfo.id / "tantivy")
    inserted = 0
    for cid, file_id, node_type, content in rows:
        for i, c in enumerate(split_claims(body_for_claims(content or "", node_type))):
            claim_id = f"{cid}#c{i:03d}"
            conn.execute(
                "insert or replace into chunks "
                "(id, file_id, project_id, name, qualified_name, node_type, "
                " content, embedding_input, parent_name) "
                "values (?,?,?,?,?,?,?,?,?)",
                (claim_id, file_id, pinfo.id, c.kind, claim_id, "claim",
                 c.text, c.text, cid))
            engine.add(claim_id, c.kind, claim_id, c.text)
            inserted += 1
    conn.commit()
    engine.commit()
    after = conn.execute(
        "select count(*) from chunks where project_id=?", (pinfo.id,)).fetchone()[0]
    conn.close()

    print(f"augmented snapshot: {out}")
    print(f"chunks {before:,} -> {after:,}  (+{inserted:,} claims)")
    print(f"tantivy rows: {engine.count:,}")
    print(f"\nrun the gate with:\n"
          f"  python benchmarks/run_valuein_bench.py --config {new_cfg} \\\n"
          f"    --gold benchmarks/valuein_gold.json --limit 10")


if __name__ == "__main__":
    main()
