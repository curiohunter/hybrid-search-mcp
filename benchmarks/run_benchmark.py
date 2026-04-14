"""Embedding model benchmark — compare gte-multilingual-base, bge-m3, Qwen3-Embedding.

Metrics: Recall@10, MRR, indexing speed, memory usage.
Target: valuein_homepage project with 30 benchmark queries.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Add project to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import Config, EmbeddingConfig, IndexingConfig
from hybrid_search.index.ast_chunker import chunk_code_file
from hybrid_search.index.doc_chunker import chunk_doc_file
from hybrid_search.index.scanner import detect_language, _walk_files, _build_ignore_spec
from hybrid_search.search.vector import VectorEngine

# Models to benchmark
MODELS = [
    {
        "name": "gte-multilingual-base",
        "hf_id": "Alibaba-NLP/gte-multilingual-base",
        "dim": 768,
        "max_tokens": 8192,
    },
    {
        "name": "bge-m3",
        "hf_id": "BAAI/bge-m3",
        "dim": 1024,
        "max_tokens": 8192,
    },
]

# We'll try Qwen3 only if the others finish (it's largest)
MODELS_OPTIONAL = [
    {
        "name": "Qwen3-Embedding",
        "hf_id": "Qwen/Qwen3-Embedding",
        "dim": 1024,
        "max_tokens": 8192,
    },
]


@dataclass
class BenchmarkResult:
    model_name: str
    recall_at_10: float = 0.0
    mrr: float = 0.0
    recall_by_category: dict = field(default_factory=dict)
    mrr_by_category: dict = field(default_factory=dict)
    indexing_time_s: float = 0.0
    files_indexed: int = 0
    chunks_created: int = 0
    memory_mb: float = 0.0
    embedding_time_s: float = 0.0
    error: str | None = None


def load_query_set() -> dict:
    qs_path = Path(__file__).parent / "query_set.json"
    with open(qs_path) as f:
        return json.load(f)


def get_process_memory_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_maxrss / (1024 * 1024)  # macOS returns bytes
    except Exception:
        return 0.0


def chunk_project(project_path: Path) -> list:
    """Chunk all files in the project."""
    config = IndexingConfig()
    ignore_spec = _build_ignore_spec(project_path, config)
    files = _walk_files(project_path, ignore_spec, config)

    all_chunks = []
    project_id = "benchmark"

    for file_path in files:
        language = detect_language(file_path)
        if language is None:
            continue

        try:
            source = file_path.read_text(errors="replace")
            if language in ("markdown", "json", "yaml", "toml"):
                chunks = chunk_doc_file(file_path, project_path, project_id, language, source)
            else:
                chunks = chunk_code_file(file_path, project_path, project_id, language, source)
            all_chunks.extend(chunks)
        except Exception as e:
            print(f"  WARN: Failed to chunk {file_path.name}: {e}")

    return all_chunks


def create_embedder_for_model(model_info: dict, cache_dir: Path):
    """Create an embedder using sentence-transformers for benchmark (no ONNX needed)."""
    from transformers import AutoTokenizer, AutoModel
    import torch

    hf_id = model_info["hf_id"]
    max_tokens = model_info["max_tokens"]

    print(f"  Loading model: {hf_id}...")
    tokenizer = AutoTokenizer.from_pretrained(hf_id, cache_dir=str(cache_dir), trust_remote_code=True)
    model = AutoModel.from_pretrained(hf_id, cache_dir=str(cache_dir), trust_remote_code=True)
    model.eval()

    def embed_batch(texts: list[str], batch_size: int = 16) -> np.ndarray:
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            encoded = tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=min(max_tokens, 512),  # Limit for speed during benchmark
                return_tensors="pt",
            )
            with torch.no_grad():
                outputs = model(**encoded)

            # Mean pooling
            hidden = outputs.last_hidden_state
            mask = encoded["attention_mask"].unsqueeze(-1).float()
            summed = (hidden * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            embeddings = summed / counts

            # L2 normalize
            norms = embeddings.norm(dim=1, keepdim=True).clamp(min=1e-9)
            embeddings = embeddings / norms

            all_embeddings.append(embeddings.numpy().astype(np.float32))

        return np.vstack(all_embeddings)

    return embed_batch


def evaluate_model(
    model_info: dict,
    chunks: list,
    queries: list[dict],
    cache_dir: Path,
) -> BenchmarkResult:
    """Evaluate a single model."""
    result = BenchmarkResult(model_name=model_info["name"])
    dim = model_info["dim"]

    try:
        mem_before = get_process_memory_mb()

        # Load model
        embed_fn = create_embedder_for_model(model_info, cache_dir)

        # Embed all chunks
        print(f"  Embedding {len(chunks)} chunks...")
        embedding_texts = [c.embedding_input for c in chunks]

        t0 = time.monotonic()
        chunk_embeddings = embed_fn(embedding_texts)
        embedding_time = time.monotonic() - t0

        result.embedding_time_s = round(embedding_time, 1)
        result.chunks_created = len(chunks)
        result.memory_mb = round(get_process_memory_mb() - mem_before, 1)

        print(f"  Embeddings done in {embedding_time:.1f}s ({len(chunks)} chunks)")

        # Build vector index
        with tempfile.TemporaryDirectory() as td:
            vec = VectorEngine(Path(td), embedding_dim=dim)
            for i, chunk in enumerate(chunks):
                vec.add(chunk.id, chunk_embeddings[i])

            # Evaluate queries
            recalls = []
            mrrs = []
            by_cat: dict[str, list[float]] = {}
            mrr_by_cat: dict[str, list[float]] = {}

            for q in queries:
                query_text = f"query: {q['query']}"
                query_vec = embed_fn([query_text])[0]

                results = vec.search(query_vec, limit=10)

                # Check recall: did any result match expected files?
                result_files = set()
                for r in results:
                    for chunk in chunks:
                        if chunk.id == r.chunk_id:
                            result_files.add(chunk.file_path)
                            break

                expected = set(q["expected_files"])
                hits = result_files & expected
                recall = len(hits) / len(expected) if expected else 0.0
                recalls.append(recall)

                cat = q["category"]
                by_cat.setdefault(cat, []).append(recall)

                # MRR: rank of first relevant result
                rr = 0.0
                for rank, r in enumerate(results, 1):
                    for chunk in chunks:
                        if chunk.id == r.chunk_id and chunk.file_path in expected:
                            rr = 1.0 / rank
                            break
                    if rr > 0:
                        break
                mrrs.append(rr)
                mrr_by_cat.setdefault(cat, []).append(rr)

            result.recall_at_10 = round(sum(recalls) / len(recalls), 4) if recalls else 0
            result.mrr = round(sum(mrrs) / len(mrrs), 4) if mrrs else 0
            result.recall_by_category = {
                cat: round(sum(v) / len(v), 4) for cat, v in by_cat.items()
            }
            result.mrr_by_category = {
                cat: round(sum(v) / len(v), 4) for cat, v in mrr_by_cat.items()
            }

    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    return result


def main():
    print("=" * 60)
    print("Hybrid Search — Embedding Model Benchmark")
    print("=" * 60)

    qs = load_query_set()
    project_path = Path(qs["project_path"])
    queries = qs["queries"]

    if not project_path.exists():
        print(f"ERROR: Project not found: {project_path}")
        sys.exit(1)

    print(f"\nProject: {project_path}")
    print(f"Queries: {len(queries)} ({sum(1 for q in queries if q['category']=='korean_nl')} KR, "
          f"{sum(1 for q in queries if q['category']=='english_nl')} EN, "
          f"{sum(1 for q in queries if q['category']=='mixed')} MX)")

    # Step 1: Chunk project (shared across all models)
    print("\n--- Step 1: Chunking project ---")
    t0 = time.monotonic()
    chunks = chunk_project(project_path)
    chunk_time = time.monotonic() - t0
    print(f"Chunked {len(chunks)} chunks in {chunk_time:.1f}s")

    # Cache dir for HuggingFace models
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"

    # Step 2: Evaluate each model
    results: list[BenchmarkResult] = []

    all_models = MODELS + MODELS_OPTIONAL
    for i, model_info in enumerate(all_models):
        print(f"\n--- Model {i+1}/{len(all_models)}: {model_info['name']} ---")
        t0 = time.monotonic()
        result = evaluate_model(model_info, chunks, queries, cache_dir)
        result.indexing_time_s = round(time.monotonic() - t0, 1)
        result.files_indexed = len(set(c.file_path for c in chunks))
        results.append(result)

        # Print intermediate results
        if result.error:
            print(f"  ERROR: {result.error}")
        else:
            print(f"  Recall@10: {result.recall_at_10}")
            print(f"  MRR:       {result.mrr}")
            print(f"  By category:")
            for cat, val in result.recall_by_category.items():
                print(f"    {cat}: Recall={val}, MRR={result.mrr_by_category.get(cat, 0)}")
            print(f"  Embedding time: {result.embedding_time_s}s")
            print(f"  Memory delta: {result.memory_mb}MB")

        # Free memory
        gc.collect()

    # Step 3: Summary
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)

    header = f"{'Model':<25} {'Recall@10':>10} {'MRR':>8} {'KR_Recall':>10} {'EN_Recall':>10} {'MX_Recall':>10} {'Emb(s)':>8} {'Mem(MB)':>8}"
    print(header)
    print("-" * len(header))

    for r in results:
        if r.error:
            print(f"{r.model_name:<25} ERROR: {r.error[:50]}")
            continue
        print(
            f"{r.model_name:<25} "
            f"{r.recall_at_10:>10.4f} "
            f"{r.mrr:>8.4f} "
            f"{r.recall_by_category.get('korean_nl', 0):>10.4f} "
            f"{r.recall_by_category.get('english_nl', 0):>10.4f} "
            f"{r.recall_by_category.get('mixed', 0):>10.4f} "
            f"{r.embedding_time_s:>8.1f} "
            f"{r.memory_mb:>8.1f}"
        )

    # Save full results
    output_path = Path(__file__).parent / "benchmark_results.json"
    with open(output_path, "w") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "project": str(project_path),
                "total_chunks": len(chunks),
                "total_files": len(set(c.file_path for c in chunks)),
                "results": [
                    {
                        "model": r.model_name,
                        "recall_at_10": r.recall_at_10,
                        "mrr": r.mrr,
                        "recall_by_category": r.recall_by_category,
                        "mrr_by_category": r.mrr_by_category,
                        "embedding_time_s": r.embedding_time_s,
                        "indexing_time_s": r.indexing_time_s,
                        "memory_mb": r.memory_mb,
                        "chunks": r.chunks_created,
                        "files": r.files_indexed,
                        "error": r.error,
                    }
                    for r in results
                ],
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\nResults saved to: {output_path}")

    # Recommendation
    valid_results = [r for r in results if r.error is None]
    if valid_results:
        best = max(valid_results, key=lambda r: r.recall_at_10)
        best_kr = max(valid_results, key=lambda r: r.recall_by_category.get("korean_nl", 0))
        print(f"\n🏆 Best overall Recall@10: {best.model_name} ({best.recall_at_10})")
        print(f"🏆 Best Korean→English:   {best_kr.model_name} ({best_kr.recall_by_category.get('korean_nl', 0)})")


if __name__ == "__main__":
    main()
