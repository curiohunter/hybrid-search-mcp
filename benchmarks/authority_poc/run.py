"""Authority nudge mini-PoC runner.

For each gold query:
  1. Retrieve bm25_ids, vector_ids once (shared across modes).
  2. Fuse twice — once with chunk_authority_scores=None (OFF),
     once with the real map from StoreDB (ON).
  3. Enrich both top-10 lists with chunk metadata.
  4. Dump side-by-side results to JSON + emit a TSV template for manual
     relevance labeling.

Usage:
    python benchmarks/authority_poc/run.py [--project PROJECT_NAME]

Outputs (written next to this script):
    results.json      raw 2-mode results per query
    label_me.tsv      one row per (query, mode, rank) — fill `relevance` column
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.fusion import reciprocal_rank_fusion
from hybrid_search.search.orchestrator import get_bm25_weight
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


HERE = Path(__file__).parent
GOLD_PATH = HERE / "gold_queries.json"
RESULTS_PATH = HERE / "results.json"
LABEL_TSV_PATH = HERE / "label_me.tsv"


def _search_once(query: str, project_info, config, embedder, limit: int = 10):
    """Run BM25 + vector retrieval once, return both ranked lists + authority map."""
    project_dir = get_project_dir(config.projects_dir, project_info.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        raise SystemExit(f"No index for project '{project_info.name}'.")

    db = StoreDB(idx_paths.store_db)
    bm25_eng = BM25Engine(idx_paths.tantivy_dir, read_only=True)
    vec_eng = VectorEngine(idx_paths.vectors_dir, embedder.embedding_dim)

    depth = limit * 3
    try:
        query_vector = embedder.embed_query(query)
        bm25_ids = [r.chunk_id for r in bm25_eng.search(query, limit=depth)]
        vec_ids = [r.chunk_id for r in vec_eng.search(query_vector, limit=depth)]
        authority = db.get_chunk_authority_scores(project_info.id)

        # Enrich callback: given chunk_id → minimal metadata for labeling.
        def enrich(chunk_id: str) -> dict:
            c = db.get_chunk(chunk_id)
            if c is None:
                return {"chunk_id": chunk_id, "name": None, "file_path": None}
            fr = db.get_file(c.file_id)
            return {
                "chunk_id": chunk_id,
                "name": c.name or "",
                "qualified_name": c.qualified_name or "",
                "node_type": c.node_type or "",
                "file_path": fr.relative_path if fr else "",
                "start_line": c.start_line,
                "end_line": c.end_line,
                "snippet": (c.docstring or c.content or "")[:120].replace("\n", " ⏎ "),
            }

        # Collect metadata *before* closing the DB. Union of all chunk_ids we
        # might surface across both modes (top-10 of each).
        chunk_universe = set(bm25_ids[:limit]) | set(vec_ids[:limit])
        # Also pre-fuse both modes to know which chunk_ids will rank — then enrich those.
        weight, _ = get_bm25_weight(query)
        fused_off = reciprocal_rank_fusion(
            bm25_ids, vec_ids, k=config.search.rrf_k,
            bm25_weight=weight, chunk_authority_scores=None,
        )[:limit]
        fused_on = reciprocal_rank_fusion(
            bm25_ids, vec_ids, k=config.search.rrf_k,
            bm25_weight=weight, chunk_authority_scores=authority,
        )[:limit]
        chunk_universe.update(fr.chunk_id for fr in fused_off)
        chunk_universe.update(fr.chunk_id for fr in fused_on)

        metadata = {cid: enrich(cid) for cid in chunk_universe}
    finally:
        db.close()

    return {
        "bm25_weight": weight,
        "fused_off": [
            {
                "rank": i + 1,
                "chunk_id": fr.chunk_id,
                "rrf_score": round(fr.rrf_score, 6),
                "authority": fr.authority,
                **metadata[fr.chunk_id],
            }
            for i, fr in enumerate(fused_off)
        ],
        "fused_on": [
            {
                "rank": i + 1,
                "chunk_id": fr.chunk_id,
                "rrf_score": round(fr.rrf_score, 6),
                "authority": fr.authority,
                **metadata[fr.chunk_id],
            }
            for i, fr in enumerate(fused_on)
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="hybrid-search-mcp")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    pinfo = registry.get_by_name(args.project)
    if pinfo is None:
        raise SystemExit(f"Unknown project: {args.project}")

    embedder = Embedder(config.embedding)

    all_results = []
    for entry in gold["queries"]:
        print(f"[{entry['id']}] ({entry['type']}) {entry['query']}")
        result = _search_once(entry["query"], pinfo, config, embedder, limit=args.limit)
        all_results.append({
            "id": entry["id"],
            "type": entry["type"],
            "query": entry["query"],
            "notes": entry.get("notes", ""),
            **result,
        })

    RESULTS_PATH.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {RESULTS_PATH}")

    # Emit TSV for labeling. One row per (query, mode, rank).
    # Columns: id, type, query, mode, rank, chunk_id, file_path, name, snippet, relevance
    rows = ["id\ttype\tquery\tmode\trank\tchunk_id\tfile_path\tname\tsnippet\trelevance"]
    for r in all_results:
        for mode_key, mode_label in [("fused_off", "OFF"), ("fused_on", "ON")]:
            for item in r[mode_key]:
                rows.append("\t".join([
                    r["id"],
                    r["type"],
                    r["query"].replace("\t", " "),
                    mode_label,
                    str(item["rank"]),
                    item["chunk_id"],
                    item["file_path"] or "",
                    item["name"] or "",
                    (item["snippet"] or "").replace("\t", " "),
                    "",  # relevance — to be filled
                ]))
    LABEL_TSV_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"Wrote {LABEL_TSV_PATH} — fill the `relevance` column (0/1/2) and run score.py")


if __name__ == "__main__":
    main()
