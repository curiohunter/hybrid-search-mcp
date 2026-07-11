"""Memory bench v2 — LongMemEval-style axes on top of the compounding bench.

Three tracks the v1 compounding bench does not cover:

  Update (knowledge-update):
    Plant two qa logs on the same topic with CONFLICTING facts — old one
    backdated ~90 days, new one ~2 days. The probe question must surface
    the newer qa log above the stale one. This is the axis temporal-KG
    systems (Zep/Graphiti) win on; here we measure where BM25+vector
    actually stands instead of guessing.

  Abstention:
    Ask about topics verified ABSENT from the project (grep 0 hits).
    The confidence contract should answer weak — never strong. Present
    controls (topics that DO exist) guard against a contract that is
    just pessimistic everywhere.

  Tokens-per-answer:
    Serialize every response exactly the way the MCP server does
    (json.dumps(..., ensure_ascii=False, indent=2)) and count tokens at
    detail=compact vs detail=full. This is the "right 7K tokens instead
    of 100K" headline number.

Planting uses qa_log's private QARecord/_persist on purpose: record()
stamps datetime.now, and the update track needs controlled timestamps.
The run restores the original qa directory on completion or SIGINT.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from run_compounding_bench import (  # noqa: E402
    clear_qa_dir,
    rebuild_orchestrator,
    reindex,
    restore_qa_dir,
)

from hybrid_search.memory.qa_log import QARecord, _persist  # noqa: E402
from hybrid_search.tools.hybrid_search import handle_hybrid_search  # noqa: E402


def _count_tokens(text: str) -> int:
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _payload(orch, project: str, query: str, detail: str) -> dict:
    return handle_hybrid_search(
        orchestrator=orch,
        query=query,
        project=project,
        limit=10,
        detail=detail,
    )


def _timed_payload(orch, project: str, query: str, detail: str) -> tuple[dict, float]:
    """Payload plus END-TO-END wall time. The response's own query_time_ms
    stops before _make_response, so it excludes the confidence pipeline —
    including the corpus-absent LIKE scan (~150 ms on a genuine miss).
    Latency headlines must come from this outer clock."""
    started = time.perf_counter()
    payload = _payload(orch, project, query, detail)
    return payload, (time.perf_counter() - started) * 1000


def _mcp_wire_tokens(payload: dict) -> int:
    """Token count of the payload exactly as the MCP server serializes it."""
    return _count_tokens(json.dumps(payload, ensure_ascii=False, indent=2))


def _result_paths(payload: dict) -> list[str]:
    return [r.get("file_path", "") for r in payload.get("results", [])]


def _rank_of_tail(paths: list[str], planted_rel: str | None) -> int | None:
    if not planted_rel:
        return None
    tail = planted_rel.split("/")[-1]
    for i, p in enumerate(paths, start=1):
        if p == planted_rel or p.endswith(tail):
            return i
    return None


def plant_qa(project_path: Path, query: str, answer: str, ts: datetime) -> str | None:
    rec = QARecord(
        query=query,
        query_type="KOREAN_NL",
        effective_bm25_weight=0.15,
        query_time_ms=0.0,
        total_chunks_searched=0,
        results=[],
        timestamp=ts,
        project_root=project_path,
        trigger="bench_v2",
        answer_chars=len(answer),
        answer_excerpt=answer,
    )
    path = _persist(rec)
    if path is None:
        return None
    try:
        return str(path.relative_to(project_path))
    except ValueError:
        return str(path)


def run_update_track(orch, project: str, cases: list[dict], planted: dict) -> list[dict]:
    rows = []
    for case in cases:
        payload, e2e_ms = _timed_payload(orch, project, case["probe"], "compact")
        paths = _result_paths(payload)
        old_rank = _rank_of_tail(paths, planted[case["id"]]["old"])
        new_rank = _rank_of_tail(paths, planted[case["id"]]["new"])
        newer_first = new_rank is not None and (old_rank is None or new_rank < old_rank)
        rows.append({
            "id": case["id"],
            "topic": case["topic"],
            "old_rank": old_rank,
            "new_rank": new_rank,
            "newer_found": new_rank is not None,
            "newer_first": newer_first,
            "stale_only": old_rank is not None and new_rank is None,
            "confidence": payload.get("confidence"),
            "query_time_ms": e2e_ms,
            "compact_tokens": _mcp_wire_tokens(payload),
            "full_tokens": _mcp_wire_tokens(_payload(orch, project, case["probe"], "full")),
            "top_paths": paths[:5],
        })
        print(
            f"  [{case['id']}] new@{new_rank} old@{old_rank} "
            f"{'OK newer-first' if rows[-1]['newer_first'] else 'MISS'}"
        )
    return rows


def run_confidence_track(orch, project: str, cases: list[dict], label: str) -> list[dict]:
    rows = []
    for case in cases:
        payload, e2e_ms = _timed_payload(orch, project, case["query"], "compact")
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "confidence": payload.get("confidence", "?"),
            "result_count": len(payload.get("results", [])),
            "query_time_ms": e2e_ms,
            "compact_tokens": _mcp_wire_tokens(payload),
            "full_tokens": _mcp_wire_tokens(_payload(orch, project, case["query"], "full")),
        })
        print(f"  [{case['id']} {label}] confidence={rows[-1]['confidence']}")
    return rows


def run_adversarial_track(orch, project: str, cases: list[dict], planted: dict) -> list[dict]:
    """Old EXACT-topic qa vs fresh ADJACENT-topic qa sharing generic nouns.

    The probe asks about the exact topic. Success = the old exact answer
    is NOT displaced below the fresh adjacent one — recency must never
    beat relevance across topics.
    """
    rows = []
    for case in cases:
        payload, e2e_ms = _timed_payload(orch, project, case["probe"], "compact")
        paths = _result_paths(payload)
        exact_rank = _rank_of_tail(paths, planted[case["id"]]["exact"])
        adjacent_rank = _rank_of_tail(paths, planted[case["id"]]["adjacent"])
        ok = exact_rank is not None and (adjacent_rank is None or exact_rank < adjacent_rank)
        rows.append({
            "id": case["id"],
            "exact_rank": exact_rank,
            "adjacent_rank": adjacent_rank,
            "exact_first": ok,
            "query_time_ms": e2e_ms,
            "compact_tokens": _mcp_wire_tokens(payload),
            "full_tokens": _mcp_wire_tokens(_payload(orch, project, case["probe"], "full")),
        })
        print(f"  [{case['id']} adversarial] exact@{exact_rank} adjacent@{adjacent_rank} {'OK' if ok else 'DISPLACED'}")
    return rows


def _rate(rows: list[dict], key: str, value) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r[key] == value) / len(rows)


def _confidence_counts(rows: list[dict]) -> dict:
    return {
        level: sum(1 for r in rows if r["confidence"] == level)
        for level in ("strong", "mixed", "weak")
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def aggregate(update_rows, absent_rows, present_rows, adversarial_rows) -> dict:
    all_rows = update_rows + absent_rows + present_rows + adversarial_rows
    compact = [r["compact_tokens"] for r in all_rows]
    full = [r["full_tokens"] for r in all_rows]
    latencies = [r["query_time_ms"] for r in all_rows if r.get("query_time_ms")]
    return {
        "update": {
            "n": len(update_rows),
            "newer_found_rate": sum(1 for r in update_rows if r["newer_found"]) / max(1, len(update_rows)),
            "newer_first_rate": sum(1 for r in update_rows if r["newer_first"]) / max(1, len(update_rows)),
            "stale_only_rate": sum(1 for r in update_rows if r["stale_only"]) / max(1, len(update_rows)),
        },
        "adversarial": {
            "n": len(adversarial_rows),
            "exact_first_rate": sum(1 for r in adversarial_rows if r["exact_first"]) / max(1, len(adversarial_rows)),
            # Decomposition — "3/3 exact first" alone overstates: ranking
            # competition only happens when BOTH are retrieved.
            "exact_found": sum(1 for r in adversarial_rows if r["exact_rank"] is not None),
            "both_found": sum(1 for r in adversarial_rows if r["exact_rank"] is not None and r["adjacent_rank"] is not None),
            "exact_first_given_both": sum(
                1 for r in adversarial_rows
                if r["exact_rank"] is not None and r["adjacent_rank"] is not None
                and r["exact_rank"] < r["adjacent_rank"]
            ),
            "adjacent_not_retrieved": sum(1 for r in adversarial_rows if r["adjacent_rank"] is None),
        },
        "abstention": {
            "n_absent": len(absent_rows),
            "n_present": len(present_rows),
            # Full distribution, not cherry-picked rates: an all-mixed
            # classifier scores 0% strong_on_absent AND 0% weak_on_present
            # while being useless — the matrix keeps us honest.
            "confidence_matrix": {
                "absent": _confidence_counts(absent_rows),
                "present": _confidence_counts(present_rows),
            },
            "weak_on_absent_rate": _rate(absent_rows, "confidence", "weak"),
            "strong_on_absent_rate": _rate(absent_rows, "confidence", "strong"),
            "weak_on_present_rate": _rate(present_rows, "confidence", "weak"),
            "strong_on_present_rate": _rate(present_rows, "confidence", "strong"),
        },
        "latency": {
            "n_queries": len(latencies),
            # End-to-end (outer perf_counter around the handler), so the
            # confidence pipeline incl. corpus-absent scans is included.
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            # Derived, not instrumented: each hybrid_search call embeds the
            # query once; every case runs compact + full -> 2 calls/case.
            "expected_embedding_calls": 2 * len(all_rows),
        },
        "tokens_per_answer": {
            "compact_mean": mean(compact) if compact else 0,
            "compact_median": median(compact) if compact else 0,
            "full_mean": mean(full) if full else 0,
            "full_median": median(full) if full else 0,
            "compact_vs_full_ratio": (mean(compact) / mean(full)) if full and mean(full) else 0,
        },
    }


def format_markdown(report: dict) -> str:
    agg = report["aggregate"]
    u, a, t = agg["update"], agg["abstention"], agg["tokens_per_answer"]
    adv, lat = agg["adversarial"], agg["latency"]
    matrix = a["confidence_matrix"]
    lines = [
        f"# Memory bench v2 — {report['project']}",
        "",
        f"- Date: {report['date']}",
        f"- Scope: ONE production codebase; update/adversarial cases are synthetic",
        f"  and hand-authored (n={u['n']} / n={adv['n']}) — treat rates as case counts,",
        "  not population estimates.",
        f"- Axes: knowledge-update ({u['n']}), adversarial recency ({adv['n']}), "
        f"abstention ({a['n_absent']} absent + {a['n_present']} present), tokens, latency",
        "",
        "## Knowledge-update (stale fact superseded by newer qa log)",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| newer_found_rate (new qa in top-10) | {sum(1 for r in report['update_rows'] if r['newer_found'])}/{u['n']} |",
        f"| newer_first_rate (new above old) | {sum(1 for r in report['update_rows'] if r['newer_first'])}/{u['n']} |",
        f"| stale_only_rate (old surfaced, new missed — worst case) | {sum(1 for r in report['update_rows'] if r['stale_only'])}/{u['n']} |",
        "",
        "| id | topic | new rank | old rank | newer first |",
        "|---|---|---:|---:|---|",
    ]
    for r in report["update_rows"]:
        lines.append(
            f"| {r['id']} | {r['topic']} | {r['new_rank'] or '—'} | {r['old_rank'] or '—'} | "
            f"{'✅' if r['newer_first'] else '❌'} |"
        )
    lines += [
        "",
        "## Adversarial recency (old exact-topic vs fresh adjacent-topic)",
        "",
        "Recency must never beat relevance across topics: the old answer that",
        "exactly matches the probe has to stay above a fresher Q&A that merely",
        "shares generic nouns.",
        "",
        f"exact_first: **{sum(1 for r in report['adversarial_rows'] if r['exact_first'])}/{adv['n']}** — decomposed: "
        f"exact found {adv['exact_found']}/{adv['n']}, both found {adv['both_found']}/{adv['n']}, "
        f"exact first given both {adv['exact_first_given_both']}/{max(1, adv['both_found'])}, "
        f"adjacent not retrieved {adv['adjacent_not_retrieved']}/{adv['n']}",
        "",
        "| id | exact (old) rank | adjacent (fresh) rank | exact first |",
        "|---|---:|---:|---|",
    ]
    for r in report["adversarial_rows"]:
        lines.append(
            f"| {r['id']} | {r['exact_rank'] or '—'} | {r['adjacent_rank'] or '—'} | "
            f"{'✅' if r['exact_first'] else '❌'} |"
        )
    lines += [
        "",
        "## Abstention — full confidence distribution",
        "",
        "An all-mixed classifier would score 0% on both headline error rates;",
        "the matrix is what keeps the claim honest.",
        "",
        "| probes | strong | mixed | weak |",
        "|---|---:|---:|---:|",
        f"| verified-absent (n={a['n_absent']}) | {matrix['absent']['strong']} | {matrix['absent']['mixed']} | {matrix['absent']['weak']} |",
        f"| verified-present (n={a['n_present']}) | {matrix['present']['strong']} | {matrix['present']['mixed']} | {matrix['present']['weak']} |",
        "",
        "| id | absent query | confidence |",
        "|---|---|---|",
    ]
    for r in report["absent_rows"]:
        lines.append(f"| {r['id']} | {r['query']} | {r['confidence']} |")
    lines += [
        "",
        "## Latency & cost",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| end-to-end search latency p50 | {lat['p50_ms']:.0f} ms |",
        f"| end-to-end search latency p95 | {lat['p95_ms']:.0f} ms |",
        f"| expected embedding API calls (derived, whole run) | {lat['expected_embedding_calls']} (1 per search; compact+full = 2/case) |",
        "",
        "## Tokens per answer (MCP wire payload, o200k_base)",
        "",
        "| detail | mean | median |",
        "|---|---:|---:|",
        f"| compact (default) | {t['compact_mean']:.0f} | {t['compact_median']:.0f} |",
        f"| full | {t['full_mean']:.0f} | {t['full_median']:.0f} |",
        "",
        f"compact/full ratio: **{t['compact_vs_full_ratio']:.2f}** — progressive disclosure saving.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(Path(__file__).parent / "memory_bench_v2_cases.json"))
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--out-md", default=None)
    args = ap.parse_args()

    with open(args.cases) as f:
        spec = json.load(f)

    project = spec["project"]
    project_path = Path(spec["project_path"])
    now = datetime.now(timezone.utc)

    backup: Path | None = None
    restored = {"done": False}

    def do_restore():
        if restored["done"]:
            return
        restore_qa_dir(project_path, backup)
        restored["done"] = True
        print("Restored original qa directory.")

    def sigint_handler(signum, frame):
        print("\nInterrupted — restoring qa dir …")
        do_restore()
        sys.exit(130)

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        print("[cold] clearing qa dir + reindex …")
        backup = clear_qa_dir(project_path)
        reindex(project_path)
        orch, _ = rebuild_orchestrator()

        print("\n[abstention] absent-topic queries on clean memory …")
        absent_rows = run_confidence_track(orch, project, spec["abstention_cases"], "absent")
        print("\n[abstention] present controls …")
        present_rows = run_confidence_track(orch, project, spec["present_controls"], "present")

        print("\n[plant] old (−90d) + new (−2d) conflicting qa logs …")
        planted: dict[str, dict[str, str | None]] = {}
        for case in spec["update_cases"]:
            planted[case["id"]] = {
                "old": plant_qa(project_path, case["old"]["query"], case["old"]["answer"], now - timedelta(days=90)),
                "new": plant_qa(project_path, case["new"]["query"], case["new"]["answer"], now - timedelta(days=2)),
            }
        adv_planted: dict[str, dict[str, str | None]] = {}
        for case in spec.get("adversarial_recency_cases", []):
            adv_planted[case["id"]] = {
                "exact": plant_qa(project_path, case["exact"]["query"], case["exact"]["answer"], now - timedelta(days=90)),
                "adjacent": plant_qa(project_path, case["adjacent"]["query"], case["adjacent"]["answer"], now - timedelta(days=2)),
            }
        n_planted = sum(1 for v in (*planted.values(), *adv_planted.values()) for p in v.values() if p)
        print(f"[plant] planted {n_planted} qa files")

        print("\n[update] reindex + probes …")
        reindex(project_path)
        orch, _ = rebuild_orchestrator()
        update_rows = run_update_track(orch, project, spec["update_cases"], planted)

        print("\n[adversarial] exact-old vs adjacent-fresh probes …")
        adversarial_rows = run_adversarial_track(
            orch, project, spec.get("adversarial_recency_cases", []), adv_planted
        )

        report = {
            "date": time.strftime("%Y-%m-%d"),
            "project": project,
            "aggregate": aggregate(update_rows, absent_rows, present_rows, adversarial_rows),
            "update_rows": update_rows,
            "adversarial_rows": adversarial_rows,
            "absent_rows": absent_rows,
            "present_rows": present_rows,
        }

        out_json = Path(args.out_json) if args.out_json else Path(__file__).parent / f"memory_bench_v2_{report['date']}.json"
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        out_md = Path(args.out_md) if args.out_md else Path(__file__).parent / f"memory_bench_v2_{report['date']}.md"
        out_md.write_text(format_markdown(report))
        print(f"\nJSON → {out_json}\nMarkdown → {out_md}")

        agg = report["aggregate"]
        u, adv, a = agg["update"], agg["adversarial"], agg["abstention"]
        m = a["confidence_matrix"]
        print("\n── Headline (one codebase; synthetic update/adversarial cases) ──")
        print(f"knowledge-update: newer_first {u['newer_first_rate']:.0%} (n={u['n']}), stale_only {u['stale_only_rate']:.0%}")
        print(f"adversarial recency: exact_first {adv['exact_first_rate']:.0%} (n={adv['n']})")
        print(
            f"abstention matrix — absent(n={a['n_absent']}): "
            f"strong {m['absent']['strong']} / mixed {m['absent']['mixed']} / weak {m['absent']['weak']}; "
            f"present(n={a['n_present']}): "
            f"strong {m['present']['strong']} / mixed {m['present']['mixed']} / weak {m['present']['weak']}"
        )
        lat = agg["latency"]
        print(f"latency (e2e): p50 {lat['p50_ms']:.0f}ms p95 {lat['p95_ms']:.0f}ms, expected embedding calls {lat['expected_embedding_calls']}")
        print(f"adversarial decomposed: both_found {adv['both_found']}/{adv['n']}, exact_first_given_both {adv['exact_first_given_both']}/{max(1, adv['both_found'])}")
        t = agg["tokens_per_answer"]
        print(f"tokens/answer: compact {t['compact_mean']:.0f} vs full {t['full_mean']:.0f} (ratio {t['compact_vs_full_ratio']:.2f})")
    finally:
        do_restore()


if __name__ == "__main__":
    main()
