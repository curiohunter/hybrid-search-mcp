"""Phase 4 benchmark — hybrid_search vs grep baseline on valuein_gold.json.

Two tracks:
  - hybrid: SearchOrchestrator.hybrid_search (production config, limit=10)
  - grep:   naive token-bag ranking (count keyword matches per file)

Per query metrics:
  - primary_hit_rank: rank of primary_target in results (1-indexed, None if miss)
  - any_hit_rank:     rank of first file matching any expected_files entry
  - recall_at_10:     #expected hit / len(expected_files)
  - time_ms:          wall clock

Aggregates per category + overall. Writes JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator


IGNORED_DIR_PREFIXES = (
    "node_modules",
    ".hybrid-search",
    ".next",
    ".git",
    "dist",
    "build",
    "mindvault-out",
)


def load_gold(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def matches_expected(result_path: str, expected_entry: str) -> bool:
    """Case-sensitive path match. Trailing slash = directory prefix."""
    if expected_entry.endswith("/"):
        return result_path.startswith(expected_entry) or result_path == expected_entry.rstrip("/")
    return result_path == expected_entry or result_path.endswith("/" + expected_entry)


def rank_of(results: list[str], expected: str) -> int | None:
    for i, rp in enumerate(results, start=1):
        if matches_expected(rp, expected):
            return i
    return None


def score_query(result_paths: list[str], query: dict) -> dict:
    expected = query.get("expected_files", [])
    primary = query.get("primary_target")
    primary_rank = rank_of(result_paths, primary) if primary else None
    any_rank = None
    hits = 0
    for exp in expected:
        r = rank_of(result_paths, exp)
        if r is not None:
            hits += 1
            if any_rank is None or r < any_rank:
                any_rank = r
    recall = hits / max(1, len(expected))
    mrr = (1.0 / any_rank) if any_rank else 0.0
    return {
        "primary_hit_rank": primary_rank,
        "any_hit_rank": any_rank,
        "recall_at_10": recall,
        "mrr": mrr,
        "hits": hits,
        "total_expected": len(expected),
    }


KOREAN_STOPWORDS = {
    "어떻게", "어떻", "어떤", "무엇", "무엇인가", "왜", "이유", "어디", "어디에",
    "하나", "되나", "있나", "나요", "무슨", "이것", "그것",
}
ENGLISH_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "how", "what", "why",
    "where", "of", "to", "for", "in", "on", "does", "do",
}


def tokenize(query: str) -> list[str]:
    raw = re.findall(r"[0-9A-Za-z_\-]+|[가-힣]+", query)
    toks = []
    for t in raw:
        if len(t) < 2:
            continue
        lo = t.lower()
        if lo in KOREAN_STOPWORDS or lo in ENGLISH_STOPWORDS:
            continue
        toks.append(t)
    return toks


def should_skip(rel_path: str) -> bool:
    first = rel_path.split(os.sep, 1)[0]
    return first in IGNORED_DIR_PREFIXES or first.startswith(".")


def scan_project_files(project_root: Path) -> list[Path]:
    out = []
    for root, dirs, files in os.walk(project_root):
        rel_root = os.path.relpath(root, project_root)
        if rel_root != "." and should_skip(rel_root):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORED_DIR_PREFIXES]
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in (
                ".ts", ".tsx", ".js", ".jsx", ".py", ".md", ".sql", ".html", ".css", ".json", ".yml", ".yaml", ".sh",
            ):
                out.append(p)
    return out


def grep_baseline(query: str, project_root: Path, files: list[Path], limit: int = 10) -> tuple[list[str], float]:
    tokens = tokenize(query)
    if not tokens:
        return [], 0.0
    patterns = [re.compile(re.escape(t), re.IGNORECASE) for t in tokens]
    scores: dict[str, int] = {}
    t0 = time.perf_counter()
    for p in files:
        try:
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        score = 0
        for pat in patterns:
            score += len(pat.findall(text))
        if score > 0:
            rel = str(p.relative_to(project_root))
            scores[rel] = score
    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    elapsed = (time.perf_counter() - t0) * 1000.0
    return [path for path, _ in ranked[:limit]], elapsed


def hybrid_track(query: str, project_name: str, orch: SearchOrchestrator, limit: int = 10) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    resp = orch.hybrid_search(query=query, project=project_name, limit=limit)
    elapsed = (time.perf_counter() - t0) * 1000.0
    seen = []
    for hit in resp.results:
        if hit.file_path not in seen:
            seen.append(hit.file_path)
    return seen, elapsed


def aggregate(per_query: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = {}
    for row in per_query:
        by_cat.setdefault(row["category"], []).append(row)

    def cat_agg(rows, track):
        scores = [r["tracks"][track] for r in rows]
        return {
            "queries": len(rows),
            "primary_hit_rate": sum(1 for s in scores if s["primary_hit_rank"] is not None) / len(rows),
            "any_hit_rate": sum(1 for s in scores if s["any_hit_rank"] is not None) / len(rows),
            "recall_at_10_mean": mean(s["recall_at_10"] for s in scores),
            "mrr_mean": mean(s["mrr"] for s in scores),
            "time_ms_mean": mean(s["time_ms"] for s in scores),
            "primary_top1": sum(1 for s in scores if s["primary_hit_rank"] == 1) / len(rows),
            "primary_top5": sum(1 for s in scores if s["primary_hit_rank"] is not None and s["primary_hit_rank"] <= 5) / len(rows),
        }

    summary = {"per_category": {}, "overall": {}}
    for cat, rows in sorted(by_cat.items()):
        summary["per_category"][cat] = {
            "hybrid": cat_agg(rows, "hybrid"),
            "grep": cat_agg(rows, "grep"),
        }
    summary["overall"] = {
        "hybrid": cat_agg(per_query, "hybrid"),
        "grep": cat_agg(per_query, "grep"),
    }
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(Path(__file__).parent / "valuein_gold.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "valuein_results.json"))
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    gold = load_gold(Path(args.gold))
    project_root = Path(gold["project_path"])
    project_name = gold["project"]

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)

    print(f"Scanning {project_root} for grep baseline …", flush=True)
    files = scan_project_files(project_root)
    print(f"  {len(files)} files to index-for-grep", flush=True)

    per_query = []
    for q in gold["queries"]:
        qid = q["id"]
        cat = q["category"]
        print(f"[{qid} {cat}] {q['query']}", flush=True)
        hres, htime = hybrid_track(q["query"], project_name, orch, limit=args.limit)
        gres, gtime = grep_baseline(q["query"], project_root, files, limit=args.limit)
        hscore = score_query(hres, q)
        gscore = score_query(gres, q)
        per_query.append({
            "id": qid,
            "category": cat,
            "query": q["query"],
            "tracks": {
                "hybrid": {**hscore, "time_ms": htime, "top_results": hres},
                "grep":   {**gscore, "time_ms": gtime, "top_results": gres},
            },
        })
        print(f"    hybrid primary_rank={hscore['primary_hit_rank']}  recall={hscore['recall_at_10']:.2f}  {htime:.0f}ms", flush=True)
        print(f"    grep   primary_rank={gscore['primary_hit_rank']}  recall={gscore['recall_at_10']:.2f}  {gtime:.0f}ms", flush=True)

    summary = aggregate(per_query)
    report = {
        "gold": str(Path(args.gold).name),
        "project": project_name,
        "project_path": str(project_root),
        "limit": args.limit,
        "summary": summary,
        "per_query": per_query,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport written to {out_path}", flush=True)
    print(f"Overall primary-top5 — hybrid: {summary['overall']['hybrid']['primary_top5']:.2f}  grep: {summary['overall']['grep']['primary_top5']:.2f}", flush=True)
    print(f"Overall recall@10    — hybrid: {summary['overall']['hybrid']['recall_at_10_mean']:.2f}  grep: {summary['overall']['grep']['recall_at_10_mean']:.2f}", flush=True)


if __name__ == "__main__":
    main()
