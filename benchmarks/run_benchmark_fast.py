"""Fast embedding benchmark — minimal chunk set for quick model comparison.

Strategy: query-relevant files (~30) + 200 random distractors = ~500 chunks.
Models: e5-small, e5-base, Qwen3-Embedding
"""

from __future__ import annotations

import gc
import json
import random
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
    {"name": "e5-small", "hf_id": "intfloat/multilingual-e5-small", "dim": 384},
    {"name": "e5-base", "hf_id": "intfloat/multilingual-e5-base", "dim": 768},
]

# Qwen3 — try if available, skip on error
MODELS_TRY = [
    {"name": "Qwen3-Embedding-0.6B", "hf_id": "Qwen/Qwen3-Embedding-0.6B", "dim": 1024},
]

DISTRACTOR_COUNT = 200


@dataclass
class Result:
    model: str
    recall_at_10: float = 0.0
    mrr: float = 0.0
    cat: dict = field(default_factory=dict)
    embed_s: float = 0.0
    n_chunks: int = 0
    error: str | None = None
    details: list = field(default_factory=list)


def load_qs():
    return json.loads((Path(__file__).parent / "query_set.json").read_text())


def build_chunk_set(project_path: Path, expected_files: set[str]) -> list:
    """Chunk only query-relevant files + random distractors."""
    config = IndexingConfig()
    ignore_spec = _build_ignore_spec(project_path, config)
    all_files = _walk_files(project_path, ignore_spec, config)

    relevant, others = [], []
    for fp in all_files:
        if fp.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".py"}:
            continue
        rel = str(fp.relative_to(project_path))
        if rel in expected_files:
            relevant.append(fp)
        else:
            others.append(fp)

    # Add random distractors
    random.seed(42)
    distractors = random.sample(others, min(DISTRACTOR_COUNT, len(others)))

    pid = "bench"
    chunks = []
    for fp in relevant + distractors:
        lang = detect_language(fp)
        if not lang:
            continue
        try:
            src = fp.read_text(errors="replace")
            chunks.extend(chunk_code_file(fp, project_path, pid, lang, src))
        except Exception:
            pass

    print(f"  Relevant files: {len(relevant)}, distractors: {len(distractors)}, chunks: {len(chunks)}")
    return chunks


def evaluate(model_info: dict, chunks: list, queries: list[dict]) -> Result:
    from sentence_transformers import SentenceTransformer

    res = Result(model=model_info["name"])
    dim = model_info["dim"]

    try:
        print(f"  Loading {model_info['hf_id']}...")
        model = SentenceTransformer(model_info["hf_id"], trust_remote_code=True)

        emb_inputs = [c.embedding_input for c in chunks]
        t0 = time.monotonic()
        chunk_embs = model.encode(emb_inputs, batch_size=32, normalize_embeddings=True, show_progress_bar=True)
        res.embed_s = round(time.monotonic() - t0, 1)
        res.n_chunks = len(chunks)

        # Build index
        with tempfile.TemporaryDirectory() as td:
            vec = VectorEngine(Path(td), embedding_dim=dim)
            for i, ch in enumerate(chunks):
                vec.add(ch.id, chunk_embs[i])

            id_to_file = {ch.id: ch.file_path for ch in chunks}
            id_to_name = {ch.id: ch.name for ch in chunks}

            recalls, mrrs = [], []
            by_cat: dict[str, dict] = {}

            for q in queries:
                q_emb = model.encode([f"query: {q['query']}"], normalize_embeddings=True)[0]
                hits = vec.search(q_emb, limit=10)

                hit_files = [id_to_file.get(h.chunk_id, "") for h in hits]
                hit_names = [id_to_name.get(h.chunk_id, "") for h in hits]
                exp_files = set(q["expected_files"])
                exp_syms = set(q.get("expected_symbols", []))

                # Recall: file OR symbol match
                file_hit = len(set(hit_files) & exp_files) / len(exp_files) if exp_files else 0
                sym_hit = sum(
                    1 for s in exp_syms
                    if any(s.lower() in n.lower() for n in hit_names)
                ) / len(exp_syms) if exp_syms else 0
                recall = max(file_hit, sym_hit)
                recalls.append(recall)

                # MRR
                rr = 0.0
                for rank, h in enumerate(hits, 1):
                    hf = id_to_file.get(h.chunk_id, "")
                    hn = id_to_name.get(h.chunk_id, "")
                    if hf in exp_files or any(s.lower() in hn.lower() for s in exp_syms):
                        rr = 1.0 / rank
                        break
                mrrs.append(rr)

                cat = q["category"]
                by_cat.setdefault(cat, {"r": [], "m": []})
                by_cat[cat]["r"].append(recall)
                by_cat[cat]["m"].append(rr)

                res.details.append({
                    "id": q["id"],
                    "q": q["query"],
                    "recall": round(recall, 2),
                    "rr": round(rr, 2),
                    "top3": [f"{id_to_name.get(h.chunk_id, '?')} ({h.score:.3f})" for h in hits[:3]],
                })

            res.recall_at_10 = round(np.mean(recalls), 4)
            res.mrr = round(np.mean(mrrs), 4)
            res.cat = {
                c: {"recall": round(np.mean(v["r"]), 4), "mrr": round(np.mean(v["m"]), 4)}
                for c, v in by_cat.items()
            }

        del model
        gc.collect()

    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    return res


def main():
    print("=" * 60)
    print("Fast Embedding Benchmark")
    print("=" * 60)

    qs = load_qs()
    proj = Path(qs["project_path"])
    queries = qs["queries"]

    # Collect all expected files
    exp_files = set()
    for q in queries:
        exp_files.update(q["expected_files"])
    print(f"Expected files in queries: {len(exp_files)}")

    # Build minimal chunk set
    print("\n--- Chunking ---")
    chunks = build_chunk_set(proj, exp_files)

    # Run models
    all_models = MODELS + MODELS_TRY
    results: list[Result] = []

    for i, m in enumerate(all_models):
        print(f"\n--- [{i+1}/{len(all_models)}] {m['name']} ---")
        r = evaluate(m, chunks, queries)
        results.append(r)
        if r.error:
            print(f"  ERROR: {r.error}")
        else:
            print(f"  Recall@10={r.recall_at_10}  MRR={r.mrr}  ({r.embed_s}s)")
            for c, v in r.cat.items():
                print(f"    {c}: R@10={v['recall']}  MRR={v['mrr']}")

    # Summary
    print("\n" + "=" * 60)
    print(f"{'Model':<20} {'R@10':>6} {'MRR':>6} {'KR':>6} {'EN':>6} {'MX':>6} {'sec':>5}")
    print("-" * 60)
    for r in results:
        if r.error:
            print(f"{r.model:<20} ERROR: {r.error[:40]}")
            continue
        kr = r.cat.get("korean_nl", {}).get("recall", 0)
        en = r.cat.get("english_nl", {}).get("recall", 0)
        mx = r.cat.get("mixed", {}).get("recall", 0)
        print(f"{r.model:<20} {r.recall_at_10:>6.3f} {r.mrr:>6.3f} {kr:>6.3f} {en:>6.3f} {mx:>6.3f} {r.embed_s:>5.0f}")

    # Failed queries
    for r in results:
        if r.error:
            continue
        failed = [d for d in r.details if d["recall"] == 0]
        if failed:
            print(f"\n{r.model} — {len(failed)} misses:")
            for d in failed:
                print(f"  {d['id']}: \"{d['q']}\" → {d['top3'][0] if d['top3'] else '?'}")

    # Save
    out = Path(__file__).parent / "benchmark_results_fast.json"
    with open(out, "w") as f:
        json.dump({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "chunks": len(chunks),
            "results": [{k: v for k, v in r.__dict__.items()} for r in results],
        }, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved: {out}")

    valid = [r for r in results if not r.error]
    if valid:
        best = max(valid, key=lambda r: r.recall_at_10)
        best_kr = max(valid, key=lambda r: r.cat.get("korean_nl", {}).get("recall", 0))
        print(f"\nBest overall: {best.model} (R@10={best.recall_at_10})")
        print(f"Best Korean→English: {best_kr.model} (KR={best_kr.cat.get('korean_nl', {}).get('recall', 0)})")


if __name__ == "__main__":
    main()
