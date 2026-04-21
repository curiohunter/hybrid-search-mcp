"""Full L6 sweep runner — self-contained + external spot check × α ∈ {0.2, 0.3, 0.5}.

Per-α, per-project:
  1. Retrieve bm25_ids/vector_ids once per query (shared across ON/OFF).
  2. Patch fusion._AUTHORITY_BOOST_ALPHA to the sweep value.
  3. Fuse OFF (authority=None) and ON (authority=map).
  4. Apply relevance rules from the gold JSON (v2 structured matchers, or
     external proxy based on expected_files).

Output:
  results_v2.json — per-query rows with rel_off, rel_on, α-specific rrf scores.
    Schema: list of {query_id, project, type, query, alpha, mode, rank, chunk_id,
                     file_path, name, snippet, relevance}

Run:
    python benchmarks/authority_poc/run_v2.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry
from hybrid_search.search import fusion as fusion_mod
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.fusion import reciprocal_rank_fusion
from hybrid_search.search.orchestrator import QueryType, get_bm25_weight
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

HERE = Path(__file__).parent
SELF_GOLD = HERE / "gold_queries_v2.json"
EXTERNAL_GOLD = HERE / "external_queries.json"
OUTPUT_PATH = HERE / "results_v2.json"

ALPHAS = [0.2, 0.3, 0.5]
TOP_K = 10
RETRIEVAL_DEPTH = 30


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def _match_rule(rule: dict, file_path: str, name: str, snippet: str) -> bool:
    """Check if a chunk matches a single rule dict."""
    if "file_path_suffix" in rule and not file_path.endswith(rule["file_path_suffix"]):
        return False
    if "file_path" in rule and file_path != rule["file_path"]:
        return False
    if "name_eq" in rule and name != rule["name_eq"]:
        return False
    if "name_contains" in rule and rule["name_contains"] not in name:
        return False
    if "snippet_contains" in rule and rule["snippet_contains"] not in snippet:
        return False
    return True


def _score_structured(expected: dict, file_path: str, name: str, snippet: str) -> int:
    """Apply structured primary/secondary matchers from gold_queries_v2 entries."""
    for rule in expected.get("primary", []):
        if _match_rule(rule, file_path, name, snippet):
            return 2
    for rule in expected.get("secondary", []):
        if _match_rule(rule, file_path, name, snippet):
            return 1
    return 0


def _score_external(expected_files: list[str], file_path: str) -> int:
    """Expected-files proxy for external projects (domain-knowledge-free)."""
    if not expected_files or not file_path:
        return 0
    if file_path in expected_files:
        return 2
    # Same directory as any expected file → rel=1
    file_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    for ef in expected_files:
        ef_dir = ef.rsplit("/", 1)[0] if "/" in ef else ""
        if file_dir and file_dir == ef_dir:
            return 1
    return 0


# ---------------------------------------------------------------------------
# Retrieval + fusion
# ---------------------------------------------------------------------------


@dataclass
class QueryRun:
    query_id: str
    project: str
    qtype: str
    query: str
    alpha: float
    mode: str
    rank: int
    chunk_id: str
    file_path: str
    name: str
    snippet: str
    relevance: int
    rrf_score: float
    authority: float | None


def _retrieve(pinfo, query: str, config, embedder) -> tuple[list[str], list[str], dict]:
    """Shared retrieval — runs BM25 + vector + authority map once."""
    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    db = StoreDB(idx_paths.store_db)
    bm25_eng = BM25Engine(idx_paths.tantivy_dir, read_only=True)
    vec_eng = VectorEngine(idx_paths.vectors_dir, embedder.embedding_dim)
    try:
        query_vector = embedder.embed_query(query)
        bm25_ids = [r.chunk_id for r in bm25_eng.search(query, limit=RETRIEVAL_DEPTH)]
        vec_ids = [r.chunk_id for r in vec_eng.search(query_vector, limit=RETRIEVAL_DEPTH)]
        authority = db.get_chunk_authority_scores(pinfo.id)
    finally:
        db.close()
    return bm25_ids, vec_ids, authority


def _enrich(pinfo, chunk_ids: set, config) -> dict[str, dict]:
    """Pull file_path/name/snippet for the given chunk ids."""
    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    db = StoreDB(idx_paths.store_db)
    out = {}
    try:
        for cid in chunk_ids:
            c = db.get_chunk(cid)
            if c is None:
                out[cid] = {"file_path": "", "name": "", "snippet": ""}
                continue
            fr = db.get_file(c.file_id)
            out[cid] = {
                "file_path": fr.relative_path if fr else "",
                "name": c.name or "",
                "snippet": (c.docstring or c.content or "")[:200].replace("\n", " ⏎ "),
            }
    finally:
        db.close()
    return out


def _fuse(bm25_ids, vec_ids, authority_map, query, k, alpha) -> list:
    """Run fusion once with given α (patched on module) and authority map.

    Mirrors the production gate from orchestrator.hybrid_search (M1.2):
    EXACT_SYMBOL queries disable the authority nudge so the definition
    itself doesn't lose to well-connected call-sites.
    """
    original = fusion_mod._AUTHORITY_BOOST_ALPHA
    fusion_mod._AUTHORITY_BOOST_ALPHA = alpha
    try:
        weight, qtype = get_bm25_weight(query)
        effective_authority = None if qtype == QueryType.EXACT_SYMBOL else authority_map
        return reciprocal_rank_fusion(
            bm25_ids, vec_ids, k=k, bm25_weight=weight,
            chunk_authority_scores=effective_authority,
        )[:TOP_K]
    finally:
        fusion_mod._AUTHORITY_BOOST_ALPHA = original


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


def _run_project(gold_path: Path, registry, config, embedder, is_external: bool):
    """Return list[QueryRun] for one gold file (self or external)."""
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    project_name = gold["project"]
    pinfo = registry.get_by_name(project_name)
    if pinfo is None:
        print(f"  ⚠ skipping {project_name} — not registered", file=sys.stderr)
        return []

    rows: list[QueryRun] = []
    for entry in gold["queries"]:
        print(f"  [{entry['id']:4s}] {entry['query'][:60]}")
        bm25_ids, vec_ids, authority = _retrieve(pinfo, entry["query"], config, embedder)

        # OFF is α-independent — compute once.
        fused_off = _fuse(bm25_ids, vec_ids, None, entry["query"], config.search.rrf_k, 0.3)
        chunk_universe = {fr.chunk_id for fr in fused_off}

        # ON for each α.
        fused_on_by_alpha = {}
        for a in ALPHAS:
            fused_on = _fuse(bm25_ids, vec_ids, authority, entry["query"], config.search.rrf_k, a)
            fused_on_by_alpha[a] = fused_on
            chunk_universe.update(fr.chunk_id for fr in fused_on)

        metadata = _enrich(pinfo, chunk_universe, config)

        # Label every chunk that appears in any top-10 list.
        def label(chunk_id: str) -> int:
            meta = metadata.get(chunk_id, {"file_path": "", "name": "", "snippet": ""})
            if is_external:
                return _score_external(entry.get("expected_files", []), meta["file_path"])
            return _score_structured(entry["expected"], meta["file_path"], meta["name"], meta["snippet"])

        def push(mode: str, alpha: float, fused):
            for i, fr in enumerate(fused):
                meta = metadata.get(fr.chunk_id, {"file_path": "", "name": "", "snippet": ""})
                rows.append(QueryRun(
                    query_id=entry["id"],
                    project=project_name,
                    qtype=entry.get("type", "unknown"),
                    query=entry["query"],
                    alpha=alpha,
                    mode=mode,
                    rank=i + 1,
                    chunk_id=fr.chunk_id,
                    file_path=meta["file_path"],
                    name=meta["name"],
                    snippet=meta["snippet"],
                    relevance=label(fr.chunk_id),
                    rrf_score=round(fr.rrf_score, 6),
                    authority=fr.authority,
                ))

        # OFF entered once with a sentinel α=0.0 ("not applicable"); it's shared.
        push("OFF", 0.0, fused_off)
        for a, fused_on in fused_on_by_alpha.items():
            push("ON", a, fused_on)

    return rows


def main() -> None:
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding)

    print("Self-contained (hybrid-search-mcp):")
    self_rows = _run_project(SELF_GOLD, registry, config, embedder, is_external=False)

    print("\nExternal spot check (valuein_homepage):")
    external_rows = _run_project(EXTERNAL_GOLD, registry, config, embedder, is_external=True)

    all_rows = [row.__dict__ for row in self_rows + external_rows]
    OUTPUT_PATH.write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {OUTPUT_PATH} — {len(all_rows)} rows.")


if __name__ == "__main__":
    main()
