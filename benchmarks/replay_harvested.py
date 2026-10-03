"""Read back what real use recorded — and replay the failures against the index.

selfeval has been writing two files in every project since v1.1:
``events.jsonl`` (what happened after each search) and ``harvested.jsonl``
(the question, and the files the agent ended up opening instead of the ones
it was served). The second was meant to be "the regression set nobody had to
label". Nothing ever ran it. A week of real use produced 186 items and no
number.

This runner does the two readings that were missing:

**usage** — the selfeval verdicts, restricted to prompts a person typed. The
harness delivers task notifications as prompts; they were more than half of
one week's scored pre-fetches and one of them was adopted, so a rate that
includes them measures the harness. A betrayal whose every outside read is a
file the index could never have served (a scratchpad, another worktree's new
file) is counted apart as ``unservable`` — the agent went elsewhere, but not
because the search missed.

**replay** — each harvested question is asked again, today, and scored by
the rank of the files that turned out to matter. Items whose gold files are
not in the index at all are reported as ``unindexed`` rather than as misses:
that is a coverage gap, and ranking cannot fix it.

``--since`` splits the replay in two. Items harvested before it are the
``carried`` set — the same questions the previous reading scored, so the two
figures compare. Items after it are ``fresh``: failures the code has never
been measured against.

    python benchmarks/replay_harvested.py                       # live index
    python benchmarks/replay_harvested.py --config $SNAP/config.toml \\
        --since 2026-09-23 --out usage.json

stdout prints counts only. The output file quotes real prompts and paths
(CLAUDE.md "공개물에 코퍼스를 인용하지 말 것") — it defaults to
``~/.hybrid-search/benchmarks/usage/`` and must never be committed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_search import clock  # noqa: E402
from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.memory import selfeval  # noqa: E402
from hybrid_search.memory.quality import is_harness_noise  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.orchestrator import SearchOrchestrator  # noqa: E402
from hybrid_search.storage.db import StoreDB  # noqa: E402
from hybrid_search.storage.indexes import IndexPaths, get_project_dir  # noqa: E402

_OUT_DIR = Path("~/.hybrid-search/benchmarks/usage").expanduser()
_LANES = ("tool", "prefetch")
_DEFAULT_WINDOW_DAYS = 7
_EXTERNAL = "external:"


def parse_since(text: str | None) -> datetime | None:
    """A date or an ISO instant, as an aware datetime. None stays None."""
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ts(row: dict) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(row["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_unservable(row: dict) -> bool:
    """A betrayal the index could not have prevented."""
    reads = row.get("outside_reads") or []
    return (
        row.get("verdict") == "betrayed"
        and not row.get("greps_after")
        and bool(reads)
        and all(r.startswith(_EXTERNAL) for r in reads)
    )


def usage_counts(rows: list[dict], since: datetime | None) -> dict:
    """Per-lane verdict counts over prompts a person typed, from ``since``."""
    empty = {"total": 0, "adopted": 0, "betrayed": 0, "unservable": 0, "no_followup": 0}
    lanes = {lane: dict(empty) for lane in _LANES}
    harness = 0
    for row in rows:
        stamp = _ts(row)
        if stamp is None or (since is not None and stamp < since):
            continue
        lane = row.get("source") or "tool"
        verdict = row.get("verdict")
        if lane not in lanes or verdict is None:
            continue
        if is_harness_noise(row.get("query")):
            harness += 1
            continue
        counts = lanes[lane]
        counts["total"] += 1
        if verdict in ("adopted", "mixed"):
            counts["adopted"] += 1
        elif _is_unservable(row):
            counts["unservable"] += 1
        elif verdict == "betrayed":
            counts["betrayed"] += 1
        else:
            counts["no_followup"] += 1
    return {"lanes": lanes, "harness_excluded": harness}


def build_items(rows: list[dict], indexed: set[str]) -> tuple[list[dict], dict]:
    """Harvested rows → replayable items, one per distinct question.

    Returns the items plus what was set aside and why. An item keeps only the
    gold paths the index actually holds; with none left it is ``unindexed``.
    """
    aside = {"harness": 0, "no_usable_gold": 0, "unindexed": 0}
    by_query: dict[str, list[str]] = {}
    for row in rows:
        query = (row.get("query") or "").strip()
        if not query:
            continue
        if is_harness_noise(query):
            aside["harness"] += 1
            continue
        gold = [g for g in row.get("gold_paths") or [] if selfeval.is_usable_gold(g)]
        if not gold:
            aside["no_usable_gold"] += 1
            continue
        known = by_query.setdefault(query, [])
        known.extend(g for g in gold if g not in known)
    items: list[dict] = []
    for query, gold in by_query.items():
        held = [g for g in gold if g in indexed]
        if not held:
            aside["unindexed"] += 1
            continue
        items.append({"query": query, "gold": held})
    return items, aside


def gold_rank(result_paths: list[str], gold: list[str]) -> int | None:
    """1-based rank of the first result that is one of the gold files."""
    for rank, path in enumerate(result_paths, start=1):
        if any(selfeval._paths_match(g, path) for g in gold):
            return rank
    return None


def replay_metrics(ranks: list[int | None]) -> dict:
    """found / top3 / top5 / mrr over one set of replayed items."""
    n = len(ranks)
    if not n:
        return {"n": 0}
    hits = [r for r in ranks if r is not None]
    return {
        "n": n,
        "found": round(len(hits) / n, 4),
        "top3": round(sum(1 for r in hits if r <= 3) / n, 4),
        "top5": round(sum(1 for r in hits if r <= 5) / n, 4),
        "mrr": round(sum(1 / r for r in hits) / n, 4),
    }


def _result_paths(response) -> list[str]:
    paths: list[str] = []
    for hit in getattr(response, "results", None) or []:
        path = (getattr(hit, "file_path", "") or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def measure_project(orch, pinfo, indexed: set[str], since: datetime | None,
                    limit: int) -> tuple[dict, list[dict]]:
    """(numbers for the record, per-item detail for the private output)."""
    root = Path(pinfo.path)
    base = root / selfeval._SELFEVAL_DIR
    events = selfeval._dedup_rows(selfeval._read_jsonl(base / selfeval._EVENTS_FILE))
    window = since or datetime.now(timezone.utc) - timedelta(days=_DEFAULT_WINDOW_DAYS)
    numbers: dict = {"usage": usage_counts(events, window)}

    harvested = selfeval.harvested(root)
    cache: dict[str, list[str]] = {}
    detail: list[dict] = []

    def replay(rows: list[dict], label: str) -> tuple[dict, dict]:
        items, aside = build_items(rows, indexed)
        ranks: list[int | None] = []
        for item in items:
            query = item["query"]
            if query not in cache:
                response = orch.hybrid_search(query=query, project=pinfo.name, limit=limit)
                cache[query] = _result_paths(response)
            rank = gold_rank(cache[query], item["gold"])
            ranks.append(rank)
            detail.append({"project": pinfo.name, "set": label, **item,
                           "rank": rank, "top": cache[query][:limit]})
        return replay_metrics(ranks), aside

    numbers["replay"] = {}
    numbers["replay"]["all"], numbers["set_aside"] = replay(harvested, "all")
    if since is not None:
        before = [r for r in harvested if (_ts(r) or since) < since]
        after = [r for r in harvested if (_ts(r) or since) >= since]
        numbers["replay"]["carried"], _ = replay(before, "carried")
        numbers["replay"]["fresh"], _ = replay(after, "fresh")
    return numbers, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None,
                    help="config.toml — point it at a frozen snapshot when comparing readings")
    ap.add_argument("--project", action="append", default=[],
                    help="project name (repeatable). Default: every registered "
                         "project that has harvested items")
    ap.add_argument("--since", default=None,
                    help="date or ISO instant of the previous reading: splits "
                         "the replay into carried/fresh and starts the usage window")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # Measure the index, not whatever another session has open right now.
    os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")
    clock.pin_to_snapshot(args.config)
    config = load_config(Path(args.config) if args.config else None)
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
    since = parse_since(args.since)

    record: dict = {}
    detail: list[dict] = []
    for pinfo in registry.list_all():
        if args.project and pinfo.name not in args.project:
            continue
        harvested_file = Path(pinfo.path) / selfeval._SELFEVAL_DIR / selfeval._HARVESTED_FILE
        store = IndexPaths(get_project_dir(config.projects_dir, pinfo.id)).store_db
        if not harvested_file.is_file() or not store.exists():
            continue
        indexed = StoreDB(store).get_all_file_paths(pinfo.id)
        numbers, rows = measure_project(orch, pinfo, indexed, since, args.limit)
        record[pinfo.name] = numbers
        detail.extend(rows)
        print(f"{pinfo.name}: {json.dumps(numbers, ensure_ascii=False)}", flush=True)

    out = Path(args.out) if args.out else _OUT_DIR / f"{date.today().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"since": args.since, "limit": args.limit,
                    "projects": record, "items": detail},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"기록: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
