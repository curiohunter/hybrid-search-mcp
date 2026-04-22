"""Agent-in-loop simulator — Phase 5 Step D.

The Step 1 proxy assumes `read_count = primary_rank`: if the expected file
is at rank 3, the agent Reads 3 files. That's not what real agents do. A
real Claude Code session issues the MCP query, scans *snippets*, decides
whether it has enough evidence, and only Reads when it doesn't. It also
stops as soon as it finds the answer — so rank 7 isn't 7 reads; it's
however many reads it took to become confident.

This script models that loop explicitly:

  1. Issue ``hybrid_search(query)``.
  2. Scan snippets in order. If any snippet contains a satisfaction token
     (derived from the gold's ``primary_target`` filename stem and a few
     answer-side anchors), record a "snippet-only" resolution (0 reads).
  3. Otherwise Read the top file. If its content contains the satisfaction
     token, stop (1 read).
  4. Keep Reading down the list up to ``max_reads``. If still not
     satisfied, record a miss and simulate a grep fallback (+1 turn).

Outputs per query:

    - mcp_calls:       always 1 (one hybrid_search invocation)
    - reads:           number of files Read until satisfaction
    - total_bytes:     sum of snippet bytes + file bytes actually opened
    - turns:           tool round-trips (mcp_calls + reads + grep_fallback)
    - satisfied:       did the loop reach a satisfaction token
    - resolution:      "snippet" | "read" | "miss"
    - rank_at_stop:    rank of the result that satisfied (or None)

Aggregated per category + overall. The key headline is
``turns_mean`` and ``bytes_mean``; those are the quantities a real session
would bill against the context budget.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator

DEFAULT_MAX_READS = 5
GREP_FALLBACK_BYTE_PENALTY = 50_000  # "agent runs grep and skims matches"


def derive_satisfaction_tokens(query: dict, strict: bool = False) -> list[str]:
    """Distilled answer tokens the agent would recognize as 'I found it'.

    Two modes:

    - **Loose** (default): primary_target stem + expected_symbols +
      acceptable_module_names. Mirrors a pragmatic agent that says "I see
      the right subsystem in the response, I'm done."
    - **Strict** (``strict=True``): primary_target stem + expected_symbols
      only. Models a rigorous agent that needs the exact plan/design doc
      or symbol file before declaring victory — module cards alone aren't
      enough even if they point to the right subsystem.

    The gap between strict and loose shows how much of the Phase 5 win is
    real agent savings vs. how much relies on "module card is a good
    enough answer for this query type".
    """
    anchors: list[str] = []
    primary = query.get("primary_target") or ""
    if primary:
        p = primary.rstrip("/")
        last = p.rsplit("/", 1)[-1]
        stem = last.rsplit(".", 1)[0] if "." in last else last
        if len(stem) >= 3:
            anchors.append(stem)
    for sym in query.get("expected_symbols", []) or []:
        if len(sym) >= 3:
            anchors.append(sym)
    if not strict:
        for mod in query.get("acceptable_module_names", []) or []:
            if len(mod) >= 3:
                anchors.append(mod)
    # De-dupe while preserving order.
    seen = set()
    out: list[str] = []
    for a in anchors:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def token_satisfied(text: str, tokens: list[str]) -> str | None:
    """Return the first token that appears in text (substring, case-insensitive)
    or None if nothing matches."""
    if not text:
        return None
    low = text.lower()
    for t in tokens:
        if t.lower() in low:
            return t
    return None


def read_file_bytes(project_root: Path, rel_path: str) -> tuple[str, int]:
    """Return (content, byte_size). Bounded read: cap at 200 KB so a single
    huge file doesn't dominate the byte metric — mirrors real agent
    behavior where long files are skimmed, not devoured whole."""
    path = project_root / rel_path.rstrip("/")
    MAX_BYTES = 200_000
    try:
        if path.is_dir():
            # Treat directory "reads" as a concat of up to 5 child files
            parts = []
            total = 0
            for f in sorted(path.iterdir())[:5]:
                if f.is_file() and f.suffix.lower() in (
                    ".md", ".ts", ".tsx", ".py", ".sql", ".js", ".jsx"
                ):
                    try:
                        chunk = f.read_text(errors="ignore")
                    except OSError:
                        continue
                    parts.append(chunk)
                    total += f.stat().st_size
                    if total >= MAX_BYTES:
                        break
            return "\n".join(parts)[:MAX_BYTES], min(total, MAX_BYTES)
        if path.is_file():
            text = path.read_text(errors="ignore")[:MAX_BYTES]
            return text, min(path.stat().st_size, MAX_BYTES)
    except OSError:
        pass
    return "", 0


def simulate_query(
    query: dict,
    project_name: str,
    project_root: Path,
    orch: SearchOrchestrator,
    max_reads: int = DEFAULT_MAX_READS,
    strict: bool = False,
) -> dict:
    """One agent loop iteration over one gold query."""
    tokens = derive_satisfaction_tokens(query, strict=strict)
    t0 = time.perf_counter()
    resp = orch.hybrid_search(query=query["query"], project=project_name, limit=10)
    mcp_elapsed = (time.perf_counter() - t0) * 1000.0

    # Bytes consumed from the MCP response itself (snippets).
    snippet_bytes = sum(len(h.snippet or "") for h in resp.results)

    # Stage 1: can we resolve from snippets alone?
    snippet_blob = "\n".join(
        (h.snippet or "") + "\n" + (h.name or "") + "\n" + (h.file_path or "")
        for h in resp.results
    )
    hit_token = token_satisfied(snippet_blob, tokens)
    if hit_token is not None:
        return {
            "id": query["id"],
            "category": query["category"],
            "query": query["query"],
            "mcp_calls": 1,
            "reads": 0,
            "total_bytes": snippet_bytes,
            "turns": 1,
            "satisfied": True,
            "resolution": "snippet",
            "satisfied_via": hit_token,
            "rank_at_stop": None,
            "mcp_time_ms": mcp_elapsed,
        }

    # Stage 2: Read down the list.
    read_files: list[str] = []
    read_bytes_total = 0
    for rank, hit in enumerate(resp.results[:max_reads], start=1):
        if hit.file_path in read_files:
            continue
        content, byte_size = read_file_bytes(project_root, hit.file_path)
        read_files.append(hit.file_path)
        read_bytes_total += byte_size
        if token_satisfied(content, tokens):
            return {
                "id": query["id"],
                "category": query["category"],
                "query": query["query"],
                "mcp_calls": 1,
                "reads": len(read_files),
                "total_bytes": snippet_bytes + read_bytes_total,
                "turns": 1 + len(read_files),
                "satisfied": True,
                "resolution": "read",
                "satisfied_via": hit.file_path,
                "rank_at_stop": rank,
                "mcp_time_ms": mcp_elapsed,
            }

    # Stage 3: miss. Simulate grep fallback cost.
    return {
        "id": query["id"],
        "category": query["category"],
        "query": query["query"],
        "mcp_calls": 1,
        "reads": len(read_files),
        "total_bytes": snippet_bytes + read_bytes_total + GREP_FALLBACK_BYTE_PENALTY,
        "turns": 1 + len(read_files) + 1,  # mcp + reads + grep fallback
        "satisfied": False,
        "resolution": "miss",
        "satisfied_via": None,
        "rank_at_stop": None,
        "mcp_time_ms": mcp_elapsed,
    }


def aggregate(rows: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    def cat_agg(xs):
        return {
            "queries": len(xs),
            "satisfied_rate": sum(1 for x in xs if x["satisfied"]) / len(xs),
            "snippet_only_rate": sum(1 for x in xs if x["resolution"] == "snippet") / len(xs),
            "miss_rate": sum(1 for x in xs if x["resolution"] == "miss") / len(xs),
            "reads_mean": mean(x["reads"] for x in xs),
            "turns_mean": mean(x["turns"] for x in xs),
            "bytes_mean": mean(x["total_bytes"] for x in xs),
        }

    out = {"per_category": {}, "overall": {}}
    for cat, xs in sorted(by_cat.items()):
        out["per_category"][cat] = cat_agg(xs)
    out["overall"] = cat_agg(rows)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(Path(__file__).parent / "valuein_gold.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "agent_loop_results.json"))
    ap.add_argument("--max-reads", type=int, default=DEFAULT_MAX_READS)
    ap.add_argument(
        "--strict", action="store_true",
        help="Only primary_target filename/symbols satisfy — module name matches don't count.",
    )
    args = ap.parse_args()

    gold = json.load(Path(args.gold).open())
    project_root = Path(gold["project_path"])
    project_name = gold["project"]

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)

    rows: list[dict] = []
    mode = "strict" if args.strict else "loose"
    print(f"Agent loop simulation — max_reads={args.max_reads} mode={mode}")
    for q in gold["queries"]:
        row = simulate_query(
            q, project_name, project_root, orch, args.max_reads, strict=args.strict,
        )
        rows.append(row)
        marker = {
            "snippet": "✓ snippet",
            "read":    f"✓ read@{row['rank_at_stop']}",
            "miss":    "✗ miss",
        }[row["resolution"]]
        print(
            f"  {row['id']:3} {row['category']:12} "
            f"turns={row['turns']} reads={row['reads']} "
            f"bytes={row['total_bytes']/1024:.1f}KB  {marker}",
            flush=True,
        )

    summary = aggregate(rows)
    report = {
        "gold": Path(args.gold).name,
        "project": project_name,
        "max_reads": args.max_reads,
        "mode": mode,
        "summary": summary,
        "per_query": rows,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport written to {args.out}")
    o = summary["overall"]
    print(
        f"OVERALL: satisfied={o['satisfied_rate']:.2f} "
        f"snippet-only={o['snippet_only_rate']:.2f} miss={o['miss_rate']:.2f}  "
        f"turns={o['turns_mean']:.2f} reads={o['reads_mean']:.2f} "
        f"bytes={o['bytes_mean']/1024:.1f}KB"
    )


if __name__ == "__main__":
    main()
