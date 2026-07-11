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
        payload = _payload(orch, project, case["probe"], "compact")
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
        payload = _payload(orch, project, case["query"], "compact")
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "confidence": payload.get("confidence", "?"),
            "result_count": len(payload.get("results", [])),
            "compact_tokens": _mcp_wire_tokens(payload),
            "full_tokens": _mcp_wire_tokens(_payload(orch, project, case["query"], "full")),
        })
        print(f"  [{case['id']} {label}] confidence={rows[-1]['confidence']}")
    return rows


def _rate(rows: list[dict], key: str, value) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r[key] == value) / len(rows)


def aggregate(update_rows, absent_rows, present_rows) -> dict:
    all_rows = update_rows + absent_rows + present_rows
    compact = [r["compact_tokens"] for r in all_rows]
    full = [r["full_tokens"] for r in all_rows]
    return {
        "update": {
            "n": len(update_rows),
            "newer_found_rate": sum(1 for r in update_rows if r["newer_found"]) / max(1, len(update_rows)),
            "newer_first_rate": sum(1 for r in update_rows if r["newer_first"]) / max(1, len(update_rows)),
            "stale_only_rate": sum(1 for r in update_rows if r["stale_only"]) / max(1, len(update_rows)),
        },
        "abstention": {
            "n_absent": len(absent_rows),
            "weak_on_absent_rate": _rate(absent_rows, "confidence", "weak"),
            "strong_on_absent_rate": _rate(absent_rows, "confidence", "strong"),
            "n_present": len(present_rows),
            "weak_on_present_rate": _rate(present_rows, "confidence", "weak"),
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
    lines = [
        f"# Memory bench v2 — {report['project']}",
        "",
        f"- Date: {report['date']}",
        f"- Axes: knowledge-update ({u['n']} cases), abstention ({a['n_absent']} absent + {a['n_present']} present), tokens-per-answer",
        "",
        "## Knowledge-update (stale fact superseded by newer qa log)",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| newer_found_rate (new qa in top-10) | {u['newer_found_rate']:.2%} |",
        f"| newer_first_rate (new above old) | {u['newer_first_rate']:.2%} |",
        f"| stale_only_rate (old surfaced, new missed — worst case) | {u['stale_only_rate']:.2%} |",
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
        "## Abstention (confidence contract on absent topics)",
        "",
        "| metric | value | target |",
        "|---|---:|---|",
        f"| weak_on_absent_rate | {a['weak_on_absent_rate']:.2%} | high (correct refusal) |",
        f"| strong_on_absent_rate | {a['strong_on_absent_rate']:.2%} | 0% (false confidence) |",
        f"| weak_on_present_rate | {a['weak_on_present_rate']:.2%} | low (not just pessimistic) |",
        "",
        "| id | absent query | confidence |",
        "|---|---|---|",
    ]
    for r in report["absent_rows"]:
        lines.append(f"| {r['id']} | {r['query']} | {r['confidence']} |")
    lines += [
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
        print(f"[plant] planted {sum(1 for v in planted.values() for p in v.values() if p)} qa files")

        print("\n[update] reindex + probes …")
        reindex(project_path)
        orch, _ = rebuild_orchestrator()
        update_rows = run_update_track(orch, project, spec["update_cases"], planted)

        report = {
            "date": time.strftime("%Y-%m-%d"),
            "project": project,
            "aggregate": aggregate(update_rows, absent_rows, present_rows),
            "update_rows": update_rows,
            "absent_rows": absent_rows,
            "present_rows": present_rows,
        }

        out_json = Path(args.out_json) if args.out_json else Path(__file__).parent / f"memory_bench_v2_{report['date']}.json"
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        out_md = Path(args.out_md) if args.out_md else Path(__file__).parent / f"memory_bench_v2_{report['date']}.md"
        out_md.write_text(format_markdown(report))
        print(f"\nJSON → {out_json}\nMarkdown → {out_md}")

        agg = report["aggregate"]
        print("\n── Headline ──")
        print(f"knowledge-update: newer_first {agg['update']['newer_first_rate']:.0%}, stale_only {agg['update']['stale_only_rate']:.0%}")
        print(f"abstention: weak_on_absent {agg['abstention']['weak_on_absent_rate']:.0%}, strong_on_absent {agg['abstention']['strong_on_absent_rate']:.0%}, weak_on_present {agg['abstention']['weak_on_present_rate']:.0%}")
        t = agg["tokens_per_answer"]
        print(f"tokens/answer: compact {t['compact_mean']:.0f} vs full {t['full_mean']:.0f} (ratio {t['compact_vs_full_ratio']:.2f})")
    finally:
        do_restore()


if __name__ == "__main__":
    main()
