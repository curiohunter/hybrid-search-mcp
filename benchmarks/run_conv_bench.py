"""Conversational-axis benchmark — does retrieval surface what we DISCUSSED?

`run_valuein_bench.py` scores paths: a query is answered when the right file
ranks high. That is the correct measure for "where is the code", and it is
structurally unable to measure "what did we decide, and why". A conversation
turn lives at `.conversations/claude/<session-uuid>.jsonl` — an unstable id
that is not even openable — so path scoring either ignores the memory lanes
or rewards them for a prefix match that proves nothing.

This runner scores CONTENT instead: for each question, search top-K and ask
whether the retrieved text actually carries the fact. That is the only claim
the memory layer makes, so it is the only claim worth measuring.

    python benchmarks/run_conv_bench.py --gold <your gold set>
    python benchmarks/run_conv_bench.py --gold <...> --without conv_turn  # ablation

The gold set is **not committed**. Its questions and the phrases that answer
them are quotations from one project's own history — real class names, real
vendors, things people typed while frustrated — so publishing it would
publish the corpus it measures. Write your own: a list of
``{id, topic, query, any_of}`` where ``any_of`` holds phrases you have
verified exist somewhere in that project's index, and keep it beside the
project rather than in this repository. Results are excluded for the same
reason: they quote what was retrieved.

The ablation exists to answer a specific question: the 2026-09-04 conversation
backfill moved valuein from 60 conv chunks to 4,709, and the path-based bench
did not move at all. Dropping the conv lane from the results reproduces the
"before" condition on the same corpus and the same queries, so the backfill's
value can be measured after the fact instead of asserted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.orchestrator import SearchOrchestrator  # noqa: E402

MEMORY_TYPES = frozenset({"qa_log", "conv_turn", "memory_card", "commit"})


def result_text(r) -> str:
    return f"{getattr(r, 'content', '') or ''}\n{getattr(r, 'snippet', '') or ''}"


def score_query(query: dict, results: list) -> dict:
    """Answered = a required phrase appears in the retrieved text."""
    phrases = query.get("any_of") or []
    first_rank: int | None = None
    matched: list[str] = []
    for i, r in enumerate(results, start=1):
        text = result_text(r)
        hits = [p for p in phrases if p in text]
        if hits and first_rank is None:
            first_rank = i
        matched.extend(h for h in hits if h not in matched)
    memory_hits = sum(
        1 for r in results if (getattr(r, "node_type", "") or "") in MEMORY_TYPES
    )
    conv_hits = sum(
        1 for r in results if (getattr(r, "node_type", "") or "") == "conv_turn"
    )
    return {
        "id": query["id"],
        "topic": query.get("topic", ""),
        "query": query["query"],
        "answered": first_rank is not None,
        "first_hit_rank": first_rank,
        "matched_phrases": matched,
        "n_results": len(results),
        "memory_hits": memory_hits,
        "conv_hits": conv_hits,
    }


def aggregate(rows: list[dict]) -> dict:
    n = len(rows) or 1
    answered = [r for r in rows if r["answered"]]
    ranks = [r["first_hit_rank"] for r in answered]
    top3 = sum(1 for r in ranks if r <= 3)
    return {
        "queries": len(rows),
        "answer_found": len(answered) / n,
        "answer_in_top3": top3 / n,
        # MRR over the whole set: a miss contributes 0, so this is comparable
        # across runs even when the answered set changes.
        "mrr": sum(1.0 / r for r in ranks) / n,
        "mean_memory_hits": sum(r["memory_hits"] for r in rows) / n,
        "mean_conv_hits": sum(r["conv_hits"] for r in rows) / n,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(Path(__file__).parent / "conv_gold.json"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "conv_results.json"))
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument(
        "--config",
        default=None,
        help="Path to a hybrid-search config.toml. Point this at a copy whose "
             "general.data_dir is a FROZEN snapshot of the index when "
             "comparing two versions of the code: a live index moves while "
             "you measure (2026-09-08: valuein gained 80 chunks in twenty "
             "minutes because another session was working), and a before/"
             "after taken an hour apart is then comparing two corpora, not "
             "two rankings.",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Run the whole set N times and report mean/min/max per metric. "
             "The embedding backend is not bit-stable (2026-09-08: cosine "
             "0.99998 between two calls on the same string), which is enough "
             "to flip near-ties and move a rank by one. answer_in_top3 is a "
             "threshold on rank, so one flip moves it by 1/N_queries. Any "
             "claimed delta smaller than the spread reported here is noise.",
    )
    ap.add_argument(
        "--without",
        action="append",
        default=[],
        help="Drop results of this node_type before scoring (ablation, repeatable)",
    )
    args = ap.parse_args()

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    dropped = frozenset(args.without)

    # Measure the index, not the machine. The in-flight overlays read the
    # working tree and the running session's transcript, so leaving them on
    # makes a run depend on whatever another session happens to be doing.
    os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")
    config = load_config(Path(args.config) if args.config else None)
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)

    runs: list[list[dict]] = []
    for attempt in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"\n--- run {attempt + 1}/{args.repeat} ---", flush=True)
        rows = []
        for q in gold["queries"]:
            resp = orch.hybrid_search(
                query=q["query"],
                cwd=gold["project_path"],
                # Ask for extra when ablating so the ablated run still gets
                # to fill K slots — otherwise the drop would be measuring a
                # shorter result list rather than a weaker one. The cost is
                # that the ablated arm searches at a different limit, and
                # limit feeds retrieval depth: a question can be answered
                # there purely because the deeper search reached a chunk the
                # normal one never pooled. So read the ablation as
                # indicative, not as a strict ceiling, and compare ranking
                # changes on the un-ablated arm, where both runs are the
                # same search.
                limit=args.limit * (3 if dropped else 1),
            )
            results = [
                r for r in resp.results
                if (getattr(r, "node_type", "") or "") not in dropped
            ][: args.limit]
            row = score_query(q, results)
            row["confidence"] = getattr(resp, "confidence", "")
            rows.append(row)
            mark = "✓" if row["answered"] else "✗"
            rank = row["first_hit_rank"] or "-"
            print(f"{mark} [{q['id']} {row['topic']}] rank={rank} "
                  f"conv={row['conv_hits']} {q['query'][:44]}", flush=True)
        runs.append(rows)

    summaries = [aggregate(r) for r in runs]
    label = f"without {'+'.join(sorted(dropped))}" if dropped else "full index"
    report = {
        "label": label,
        "repeat": len(runs),
        "summary": summaries[-1],
        "summaries": summaries,
        "rows": runs[-1],
    }
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    print(f"\n=== {label} · {len(runs)} run(s) ===")
    for key, fmt in (("answer_found", "5.2f"), ("answer_in_top3", "5.2f"),
                     ("mrr", "5.3f"), ("mean_memory_hits", "5.1f"),
                     ("mean_conv_hits", "5.1f")):
        vals = [s_[key] for s_ in summaries]
        mean = sum(vals) / len(vals)
        spread = "" if len(vals) == 1 else f"  [{min(vals):{fmt}} … {max(vals):{fmt}}]"
        print(f"{key:17s}{mean:{fmt}}{spread}")
    if len(summaries) > 1:
        # The instrument's own noise floor, printed next to the numbers so a
        # reader never has to guess whether a delta cleared it.
        top3 = [s_["answer_in_top3"] for s_ in summaries]
        print(f"\nnoise floor (answer_in_top3 spread over {len(summaries)} runs): "
              f"{max(top3) - min(top3):.2f}")
    print(f"\nReport written to {args.out}")


if __name__ == "__main__":
    main()
