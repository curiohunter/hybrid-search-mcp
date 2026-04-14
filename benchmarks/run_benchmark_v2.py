"""Embedding model benchmark v2 — using sentence-transformers for reliable inference.

Models: multilingual-e5-base, bge-m3
(gte-multilingual-base excluded due to custom modeling code incompatibility)
"""

from __future__ import annotations

import gc
import json
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.index.ast_chunker import chunk_code_file
from hybrid_search.index.doc_chunker import chunk_doc_file
from hybrid_search.index.scanner import detect_language, _walk_files, _build_ignore_spec
from hybrid_search.config import IndexingConfig
from hybrid_search.search.vector import VectorEngine

MODELS = [
    {
        "name": "multilingual-e5-small",
        "hf_id": "intfloat/multilingual-e5-small",
        "dim": 384,
    },
    {
        "name": "multilingual-e5-base",
        "hf_id": "intfloat/multilingual-e5-base",
        "dim": 768,
    },
    {
        "name": "bge-m3",
        "hf_id": "BAAI/bge-m3",
        "dim": 1024,
    },
]

# Only index these directories (benchmark-relevant subset)
BENCHMARK_DIRS = {
    "services", "types", "lib", "hooks",
    "app/(auth)", "app/api", "app/(dashboard)",
}

# Prioritize code files over docs for the benchmark
BENCHMARK_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".py"}


@dataclass
class BenchResult:
    model_name: str
    recall_at_10: float = 0.0
    mrr: float = 0.0
    recall_by_cat: dict = field(default_factory=dict)
    mrr_by_cat: dict = field(default_factory=dict)
    embedding_time_s: float = 0.0
    chunks: int = 0
    files: int = 0
    memory_mb: float = 0.0
    error: str | None = None
    per_query: list = field(default_factory=list)


def get_mem_mb() -> float:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    except Exception:
        return 0.0


def chunk_project(project_path: Path) -> list:
    config = IndexingConfig()
    ignore_spec = _build_ignore_spec(project_path, config)
    files = _walk_files(project_path, ignore_spec, config)

    # Filter to benchmark-relevant files only
    filtered = []
    for fp in files:
        rel = str(fp.relative_to(project_path))
        # Include if in benchmark dirs
        in_dir = any(rel.startswith(d) or f"/{d}/" in f"/{rel}" for d in BENCHMARK_DIRS)
        # Include code files from benchmark dirs
        if in_dir and fp.suffix.lower() in BENCHMARK_EXTENSIONS:
            filtered.append(fp)

    all_chunks = []
    pid = "bench"
    for fp in filtered:
        lang = detect_language(fp)
        if not lang:
            continue
        try:
            src = fp.read_text(errors="replace")
            if lang in ("markdown", "json", "yaml", "toml"):
                all_chunks.extend(chunk_doc_file(fp, project_path, pid, lang, src))
            else:
                all_chunks.extend(chunk_code_file(fp, project_path, pid, lang, src))
        except Exception:
            pass

    print(f"  Filtered: {len(filtered)} files from {len(files)} total")
    return all_chunks


def evaluate(model_info: dict, chunks: list, queries: list[dict]) -> BenchResult:
    from sentence_transformers import SentenceTransformer

    res = BenchResult(model_name=model_info["name"])
    dim = model_info["dim"]

    try:
        mem0 = get_mem_mb()
        print(f"  Loading {model_info['hf_id']}...")
        model = SentenceTransformer(model_info["hf_id"], trust_remote_code=True)

        # Embed all chunks
        print(f"  Embedding {len(chunks)} chunks...")
        emb_inputs = [c.embedding_input for c in chunks]

        t0 = time.monotonic()
        chunk_embs = model.encode(emb_inputs, batch_size=32, normalize_embeddings=True, show_progress_bar=True)
        res.embedding_time_s = round(time.monotonic() - t0, 1)
        res.chunks = len(chunks)
        res.files = len(set(c.file_path for c in chunks))
        res.memory_mb = round(get_mem_mb() - mem0, 1)

        print(f"  Done in {res.embedding_time_s}s")

        # Build vector index
        with tempfile.TemporaryDirectory() as td:
            vec = VectorEngine(Path(td), embedding_dim=dim)
            for i, ch in enumerate(chunks):
                vec.add(ch.id, chunk_embs[i])

            # Build chunk_id → file_path map for fast lookup
            id_to_file = {ch.id: ch.file_path for ch in chunks}
            id_to_name = {ch.id: ch.name for ch in chunks}

            recalls, mrrs = [], []
            by_cat: dict[str, list] = {}
            mrr_cat: dict[str, list] = {}

            for q in queries:
                # Embed query with "query: " prefix
                q_emb = model.encode([f"query: {q['query']}"], normalize_embeddings=True)[0]
                hits = vec.search(q_emb, limit=10)

                hit_files = [id_to_file.get(h.chunk_id, "") for h in hits]
                hit_names = [id_to_name.get(h.chunk_id, "") for h in hits]
                expected_files = set(q["expected_files"])
                expected_syms = set(q.get("expected_symbols", []))

                # Recall: file-level match
                matched_files = set(hit_files) & expected_files
                # Also check if any chunk name contains expected symbol
                sym_matches = set()
                for h_name in hit_names:
                    for sym in expected_syms:
                        if sym.lower() in h_name.lower():
                            sym_matches.add(sym)

                # Combined recall: file OR symbol match
                file_recall = len(matched_files) / len(expected_files) if expected_files else 0
                sym_recall = len(sym_matches) / len(expected_syms) if expected_syms else 0
                recall = max(file_recall, sym_recall)

                recalls.append(recall)
                cat = q["category"]
                by_cat.setdefault(cat, []).append(recall)

                # MRR
                rr = 0.0
                for rank, h in enumerate(hits, 1):
                    h_file = id_to_file.get(h.chunk_id, "")
                    h_name = id_to_name.get(h.chunk_id, "")
                    file_match = h_file in expected_files
                    sym_match = any(s.lower() in h_name.lower() for s in expected_syms)
                    if file_match or sym_match:
                        rr = 1.0 / rank
                        break
                mrrs.append(rr)
                mrr_cat.setdefault(cat, []).append(rr)

                res.per_query.append({
                    "id": q["id"],
                    "query": q["query"],
                    "recall": round(recall, 4),
                    "rr": round(rr, 4),
                    "top3_files": hit_files[:3],
                    "top3_names": hit_names[:3],
                    "top3_scores": [round(h.score, 4) for h in hits[:3]],
                })

            res.recall_at_10 = round(np.mean(recalls), 4)
            res.mrr = round(np.mean(mrrs), 4)
            res.recall_by_cat = {c: round(np.mean(v), 4) for c, v in by_cat.items()}
            res.mrr_by_cat = {c: round(np.mean(v), 4) for c, v in mrr_cat.items()}

        del model
        gc.collect()

    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    return res


def main():
    print("=" * 65)
    print("Hybrid Search — Embedding Model Benchmark v2")
    print("=" * 65)

    qs = json.loads((Path(__file__).parent / "query_set.json").read_text())
    proj = Path(qs["project_path"])
    queries = qs["queries"]

    print(f"Project: {proj}")
    print(f"Queries: {len(queries)}")

    # Chunk
    print("\n--- Chunking ---")
    t0 = time.monotonic()
    chunks = chunk_project(proj)
    print(f"{len(chunks)} chunks in {time.monotonic()-t0:.1f}s")

    # Evaluate
    results: list[BenchResult] = []
    for i, m in enumerate(MODELS):
        print(f"\n--- [{i+1}/{len(MODELS)}] {m['name']} ---")
        r = evaluate(m, chunks, queries)
        results.append(r)
        if r.error:
            print(f"  ERROR: {r.error}")
        else:
            print(f"  Recall@10: {r.recall_at_10}  MRR: {r.mrr}")
            for c, v in r.recall_by_cat.items():
                print(f"    {c}: Recall={v}  MRR={r.mrr_by_cat[c]}")

    # Summary table
    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    hdr = f"{'Model':<22} {'R@10':>6} {'MRR':>6} {'KR':>6} {'EN':>6} {'MX':>6} {'t(s)':>6} {'MB':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        if r.error:
            print(f"{r.model_name:<22} ERROR")
            continue
        print(
            f"{r.model_name:<22} "
            f"{r.recall_at_10:>6.3f} "
            f"{r.mrr:>6.3f} "
            f"{r.recall_by_cat.get('korean_nl',0):>6.3f} "
            f"{r.recall_by_cat.get('english_nl',0):>6.3f} "
            f"{r.recall_by_cat.get('mixed',0):>6.3f} "
            f"{r.embedding_time_s:>6.0f} "
            f"{r.memory_mb:>6.0f}"
        )

    # Failed queries analysis
    for r in results:
        if r.error:
            continue
        failed = [q for q in r.per_query if q["recall"] == 0]
        if failed:
            print(f"\n{r.model_name} — {len(failed)} queries with Recall=0:")
            for q in failed:
                print(f"  {q['id']}: \"{q['query']}\"")
                print(f"    → got: {q['top3_files']}")

    # Save
    out = Path(__file__).parent / "benchmark_results_v2.json"
    with open(out, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project": str(proj),
            "chunks": len(chunks),
            "files": len(set(c.file_path for c in chunks)),
            "results": [
                {k: v for k, v in r.__dict__.items()}
                for r in results
            ],
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved: {out}")

    # Winner
    valid = [r for r in results if not r.error]
    if valid:
        best = max(valid, key=lambda r: r.recall_at_10)
        best_kr = max(valid, key=lambda r: r.recall_by_cat.get("korean_nl", 0))
        print(f"\nBest overall: {best.model_name} (R@10={best.recall_at_10})")
        print(f"Best Korean:  {best_kr.model_name} (KR R@10={best_kr.recall_by_cat.get('korean_nl',0)})")


if __name__ == "__main__":
    main()
