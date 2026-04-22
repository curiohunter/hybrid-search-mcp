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


def score_query(
    result_paths: list[str],
    result_module_names: list[str | None],
    query: dict,
    snippet_bytes: int,
    project_root: Path,
    limit: int,
) -> dict:
    """Retrieval quality + agent-cost proxies (Phase 5 Step 1, Step B).

    Step B: ``acceptable_module_names`` on the gold query lets a module-card
    hit count as the primary target — fixing the structure-category top-1=0
    symptom where the right answer was a subsystem card rather than a file.
    """
    expected = query.get("expected_files", [])
    primary = query.get("primary_target")
    acceptable_modules = set(query.get("acceptable_module_names", []))

    # File-based primary rank (pre-Step-B behavior)
    file_primary_rank = rank_of(result_paths, primary) if primary else None

    # Module-name primary rank: first result whose module_name is in the
    # acceptable set. Grep track has all-None module names → always miss here.
    module_primary_rank: int | None = None
    if acceptable_modules:
        for i, mname in enumerate(result_module_names, start=1):
            if mname and mname in acceptable_modules:
                module_primary_rank = i
                break

    # Take whichever lands first — file or module.
    candidates = [r for r in (file_primary_rank, module_primary_rank) if r is not None]
    primary_rank = min(candidates) if candidates else None

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

    # Agent cost proxy:
    # - read_count_estimate: how many files agent must open to reach primary.
    #   If primary is in top-K, cost = rank (agent opens rank-th file).
    #   If missed, penalty = limit + 1 (agent exhausts top-K, switches tool).
    read_count_estimate = primary_rank if primary_rank is not None else (limit + 1)

    # - read_to_primary_cost_bytes: actual size of the file at primary_rank.
    #   What agent pays in tokens for the Read that surfaces primary.
    read_cost_bytes = 0
    if primary_rank is not None and primary_rank <= len(result_paths):
        rp = result_paths[primary_rank - 1]
        candidate = project_root / rp.rstrip("/")
        try:
            if candidate.is_file():
                read_cost_bytes = candidate.stat().st_size
            elif candidate.is_dir():
                # Directory primary: approximate as sum of top-level code/doc files (upper bound).
                for f in candidate.iterdir():
                    if f.is_file() and f.suffix.lower() in (".ts", ".tsx", ".md", ".sql", ".py", ".js"):
                        read_cost_bytes += f.stat().st_size
        except OSError:
            pass

    return {
        "primary_hit_rank": primary_rank,
        "primary_hit_via_module": (
            module_primary_rank is not None
            and (file_primary_rank is None or module_primary_rank <= file_primary_rank)
        ),
        "any_hit_rank": any_rank,
        "recall_at_10": recall,
        "mrr": mrr,
        "hits": hits,
        "total_expected": len(expected),
        "snippet_bytes": snippet_bytes,
        "read_count_estimate": read_count_estimate,
        "read_cost_bytes": read_cost_bytes,
        "context_pack_bytes": snippet_bytes + read_cost_bytes,
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


def grep_baseline(
    query: str, project_root: Path, files: list[Path], limit: int = 10
) -> tuple[list[str], list[str | None], float, int]:
    """Returns (top-N paths, parallel module_names (all None), elapsed_ms, snippet_bytes).

    Grep's "snippet bytes" = 0: it returns paths only (no content).
    Agent would follow up with Read(primary) → accounted for in read_cost_bytes.
    Module-name track is always None for grep (grep can't know about modules).
    """
    tokens = tokenize(query)
    if not tokens:
        return [], [], 0.0, 0
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
    top = [path for path, _ in ranked[:limit]]
    return top, [None] * len(top), elapsed, 0


def hybrid_track(
    query: str, project_name: str, orch: SearchOrchestrator, limit: int = 10
) -> tuple[list[str], list[str | None], float, int]:
    """Returns (top-N paths, parallel module_names, elapsed_ms, snippet_bytes).

    snippet_bytes = sum of all hit.snippet lengths — the content the agent
    receives directly in the MCP response, without having to Read follow-up files.
    module_names: same length as paths, entry is the module name (from
    ``hit.name`` when ``hit.node_type == "module"``), else None. Lets the
    scorer accept module-card hits as primary_target when the gold query
    declares acceptable_module_names (Step B).
    """
    t0 = time.perf_counter()
    resp = orch.hybrid_search(query=query, project=project_name, limit=limit)
    elapsed = (time.perf_counter() - t0) * 1000.0
    seen: list[str] = []
    module_names: list[str | None] = []
    snippet_bytes = 0
    for hit in resp.results:
        snippet_bytes += len(hit.snippet or "")
        if hit.file_path in seen:
            continue
        seen.append(hit.file_path)
        module_names.append(hit.name if hit.node_type == "module" else None)
    return seen, module_names, elapsed, snippet_bytes


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
            "primary_via_module_rate": (
                sum(1 for s in scores if s.get("primary_hit_via_module")) / len(rows)
            ),
            "snippet_bytes_mean": mean(s["snippet_bytes"] for s in scores),
            "read_count_estimate_mean": mean(s["read_count_estimate"] for s in scores),
            "read_cost_bytes_mean": mean(s["read_cost_bytes"] for s in scores),
            "context_pack_bytes_mean": mean(s["context_pack_bytes"] for s in scores),
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
        hres, hmods, htime, hsnip = hybrid_track(q["query"], project_name, orch, limit=args.limit)
        gres, gmods, gtime, gsnip = grep_baseline(q["query"], project_root, files, limit=args.limit)
        hscore = score_query(hres, hmods, q, hsnip, project_root, args.limit)
        gscore = score_query(gres, gmods, q, gsnip, project_root, args.limit)
        per_query.append({
            "id": qid,
            "category": cat,
            "query": q["query"],
            "tracks": {
                "hybrid": {**hscore, "time_ms": htime, "top_results": hres},
                "grep":   {**gscore, "time_ms": gtime, "top_results": gres},
            },
        })
        print(
            f"    hybrid rank={hscore['primary_hit_rank']} recall={hscore['recall_at_10']:.2f} "
            f"reads={hscore['read_count_estimate']} pack={hscore['context_pack_bytes']/1024:.1f}KB {htime:.0f}ms",
            flush=True,
        )
        print(
            f"    grep   rank={gscore['primary_hit_rank']} recall={gscore['recall_at_10']:.2f} "
            f"reads={gscore['read_count_estimate']} pack={gscore['context_pack_bytes']/1024:.1f}KB {gtime:.0f}ms",
            flush=True,
        )

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
    h = summary["overall"]["hybrid"]
    g = summary["overall"]["grep"]
    print(f"Overall primary-top5  — hybrid: {h['primary_top5']:.2f}  grep: {g['primary_top5']:.2f}", flush=True)
    print(f"Overall recall@10     — hybrid: {h['recall_at_10_mean']:.2f}  grep: {g['recall_at_10_mean']:.2f}", flush=True)
    print(f"Overall read_count    — hybrid: {h['read_count_estimate_mean']:.2f}  grep: {g['read_count_estimate_mean']:.2f}", flush=True)
    print(f"Overall context_pack  — hybrid: {h['context_pack_bytes_mean']/1024:.1f}KB  grep: {g['context_pack_bytes_mean']/1024:.1f}KB", flush=True)


if __name__ == "__main__":
    main()
