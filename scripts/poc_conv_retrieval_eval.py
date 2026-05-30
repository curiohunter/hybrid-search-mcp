#!/usr/bin/env python3
"""A-stage GO/NO-GO eval — does the conversation lane actually retrieve?

This is the de-risking step *before* schema/storage (A3). It answers the two
questions that decide whether the conversation indexer is worth building:

  1. RECALL  — for a real cross-tool question, does the right past conversation
               (Claude *or* Codex turn) surface in the conv lane top-k?
  2. LANE SEPARATION — run the *real* code search (live DB) for the same query
               and show both lanes side by side, so we can judge whether conv
               and code answer different needs (the 2026-04-16 design's whole
               premise: separate retrieval, typed late fusion — not one mixed
               index).

No persistence: conv chunks are embedded ad-hoc and cached by content hash so
reruns are cheap. Nothing is written to the real indexes.

    python scripts/poc_conv_retrieval_eval.py [PROJECT_PATH] [--topk 5]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.index.transcript_source import (  # noqa: E402
    ConvChunk,
    collect_project_chunks,
)
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.orchestrator import SearchOrchestrator  # noqa: E402


# Real cross-tool questions a user would ask. `expect` is a best-effort
# auto-judge: a top-k conv chunk PASSES if it matches the source (when given)
# and contains every substring in `contains` (case-insensitive).
EVAL_QUERIES = [
    {
        "q": "코덱스에서 홈 디렉토리가 git 루트로 잡힌 문제 어떻게 분리했지",
        "expect": {"source": "codex", "contains": ["git init"]},
    },
    {
        "q": "왜 이 프로젝트를 독립 .git 저장소로 분리하기로 했어",
        "expect": {"contains": [".git"]},
    },
    {
        "q": "hook cwd 해석 버그 수정한 작업",
        "expect": {"source": "claude", "contains": ["hook_runtime"]},
    },
    {
        "q": "잘못 커밋된 거 되돌린 세션, reset soft 한 내용",
        "expect": {"source": "claude", "contains": ["reset"]},
    },
    {
        "q": "라우터랑 quality signals 구현한 거",
        "expect": {"source": "codex", "contains": ["router"]},
    },
    {
        "q": "테스트 전부 통과했는지 확인한 turn",
        "expect": {"contains": ["pytest"]},
    },
]


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _cache_path(project_path: Path) -> Path:
    cache_dir = Path(__file__).resolve().parent / ".poc_cache"
    cache_dir.mkdir(exist_ok=True)
    digest = hashlib.sha256(str(project_path).encode()).hexdigest()[:12]
    return cache_dir / f"conv_embed_{digest}.npz"


def _embed_chunks(chunks: list[ConvChunk], embedder: Embedder, project_path: Path) -> np.ndarray:
    """Embed chunk bodies, caching by content hash so reruns are cheap."""
    hashes = [hashlib.sha256(c.text.encode("utf-8")).hexdigest() for c in chunks]
    cache_file = _cache_path(project_path)
    cached: dict[str, np.ndarray] = {}
    if cache_file.exists():
        store = np.load(cache_file, allow_pickle=True)
        cached = {h: v for h, v in zip(store["hashes"], store["vectors"])}

    missing = [(h, c.text) for h, c in zip(hashes, chunks) if h not in cached]
    if missing:
        print(f"  embedding {len(missing)} new chunks ({len(cached)} cached)…")
        vectors = embedder.embed_texts([t for _, t in missing])
        for (h, _), vec in zip(missing, vectors):
            cached[h] = np.asarray(vec, dtype=np.float32)
        all_hashes = list(cached.keys())
        np.savez(
            cache_file,
            hashes=np.array(all_hashes),
            vectors=np.stack([cached[h] for h in all_hashes]),
        )
    else:
        print(f"  all {len(chunks)} chunks cached")

    return _l2_normalize(np.stack([cached[h] for h in hashes]))


def _judge(chunk: ConvChunk, expect: dict) -> bool:
    if "source" in expect and chunk.source != expect["source"]:
        return False
    body = chunk.text.lower()
    return all(sub.lower() in body for sub in expect.get("contains", []))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", default=str(Path.cwd()))
    parser.add_argument("--topk", type=int, default=5)
    args = parser.parse_args()

    project_path = Path(args.project).resolve()
    print(f"Project: {project_path}\n")

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orchestrator = SearchOrchestrator(config, registry, embedder)

    print("Collecting conversation chunks…")
    chunks = collect_project_chunks(project_path)
    print(f"  {len(chunks)} chunks ({sum(c.source=='claude' for c in chunks)} claude / "
          f"{sum(c.source=='codex' for c in chunks)} codex)")
    chunk_vecs = _embed_chunks(chunks, embedder, project_path)

    passes = 0
    for item in EVAL_QUERIES:
        query = item["q"]
        expect = item.get("expect", {})
        qvec = _l2_normalize(embedder.embed_query(query).reshape(1, -1))[0]
        sims = chunk_vecs @ qvec
        order = np.argsort(-sims)[: args.topk]

        hit_rank = None
        for rank, idx in enumerate(order, start=1):
            if _judge(chunks[idx], expect):
                hit_rank = rank
                break
        verdict = f"PASS @{hit_rank}" if hit_rank else "MISS"
        if hit_rank:
            passes += 1

        print("\n" + "═" * 80)
        print(f"Q: {query}")
        print(f"   expect={expect}  →  CONV LANE: {verdict}")
        print("── conv lane (top {}) ──".format(args.topk))
        for rank, idx in enumerate(order, start=1):
            c = chunks[idx]
            mark = "✓" if _judge(c, expect) else " "
            print(f"  {mark}{rank}. [{c.source} t{c.turn_index}] sim={sims[idx]:.3f} "
                  f"| {c.user_prompt[:60]}")
            if c.files:
                print(f"       files: {', '.join(c.files[:4])}")

        # Lane separation: the live code index for the same query.
        print("── code lane (live DB, top {}) ──".format(args.topk))
        try:
            resp = orchestrator.hybrid_search(query=query, cwd=str(project_path), limit=args.topk)
            for rank, r in enumerate(resp.results[: args.topk], start=1):
                print(f"   {rank}. [{r.node_type}] {r.rrf_score:.4f} | {r.file_path}")
            print(f"   (confidence={resp.confidence}, qtype={resp.query_type})")
        except Exception as exc:  # pragma: no cover — eval convenience
            print(f"   code lane error: {exc}")

    print("\n" + "═" * 80)
    print(f"CONV RECALL: {passes}/{len(EVAL_QUERIES)} queries surfaced the expected turn "
          f"in top-{args.topk}")


if __name__ == "__main__":
    main()
