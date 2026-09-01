"""Self-evaluation from real usage — the zero-touch improvement loop.

Every Stop hook already parses the finished turn's transcript. This module
rides that same pass and scores what actually happened after each
``hybrid_search`` call in the turn:

- **adopted**: a top-N result file was Read afterwards (we record the rank)
- **betrayed**: the agent Read files *outside* the results, or fell back to
  Grep — the search didn't carry the turn
- **no_followup**: no Read/Grep after the search (the injected snippets
  were enough, or the results went unused — indistinguishable here)

Rows append to ``.hybrid-search/selfeval/events.jsonl``. Betrayals where we
know what the agent ended up reading also append to ``harvested.jsonl`` as
``{query, gold_paths}`` — real-usage failures become the regression set,
with zero manual labeling.

Scope (v1): only explicit ``hybrid_search`` tool calls are scored. The
UserPromptSubmit pre-fetch injects results without a tool call, so its
adoption can't be read off the transcript alone; scoring it needs a join
against the qa_log saved at prompt time (v1.1).

Everything here is called from hook context: silent-on-failure, never
raises past the public functions.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SELFEVAL_DIR = ".hybrid-search/selfeval"
_EVENTS_FILE = "events.jsonl"
_HARVESTED_FILE = "harvested.jsonl"

# Bound how much of events.jsonl summarize() reads back (tail lines).
_SUMMARY_READ_LINES = 2000

# Ranks beyond this aren't meaningful "adoption" — the agent scrolled past
# the answer slots. Kept generous; the slot design caps display anyway.
_MAX_TRACKED_PATHS = 10


def _is_search_tool(name: str) -> bool:
    return name == "mcp__hybrid-search__hybrid_search" or name.endswith(
        "__hybrid_search"
    )


def _result_text(block: dict) -> str:
    """Flatten a tool_result block's content to one string."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text") or ""
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def _parse_result_paths(text: str) -> list[str]:
    """Ordered unique file_paths out of a hybrid_search tool result."""
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    seen: set[str] = set()
    paths: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        fp = r.get("file_path")
        if not isinstance(fp, str) or not fp or fp in seen:
            continue
        seen.add(fp)
        paths.append(fp)
    return paths


def _paths_match(read_path: str, result_path: str) -> bool:
    """Result paths are usually project-relative, Read paths absolute."""
    a = read_path.strip().lstrip("./")
    b = result_path.strip().lstrip("./")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def extract_turn_events(turn_records: list[dict]) -> list[dict]:
    """Pair each hybrid_search call in a turn with its followup actions.

    ``turn_records`` is the transcript slice for ONE turn (everything after
    the genuine user prompt). Returns one dict per search call:
    ``{"query", "paths", "reads", "greps"}`` where reads/greps are the
    Read file_paths / Grep patterns issued *after* that search and before
    the next one — followups attribute to the most recent search.
    """
    pending: dict[str, dict] = {}  # tool_use_id -> event awaiting its result
    events: list[dict] = []
    current: dict | None = None

    for rec in turn_records:
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        rec_type = rec.get("type")
        for block in content:
            if not isinstance(block, dict):
                continue
            if rec_type == "assistant" and block.get("type") == "tool_use":
                name = block.get("name") or ""
                ti = block.get("tool_input") or block.get("input") or {}
                if _is_search_tool(name):
                    event = {
                        "query": (ti.get("query") or "").strip(),
                        "paths": [],
                        "reads": [],
                        "greps": [],
                    }
                    events.append(event)
                    current = event
                    uid = block.get("id")
                    if isinstance(uid, str) and uid:
                        pending[uid] = event
                elif name == "Read" and current is not None:
                    fp = (ti.get("file_path") or "").strip()
                    if fp:
                        current["reads"].append(fp)
                elif name == "Grep" and current is not None:
                    pat = (ti.get("pattern") or "").strip()
                    current["greps"].append(pat)
            elif rec_type == "user" and block.get("type") == "tool_result":
                uid = block.get("tool_use_id")
                event = pending.pop(uid, None) if isinstance(uid, str) else None
                if event is not None:
                    event["paths"] = _parse_result_paths(_result_text(block))
    return events


def score_event(event: dict) -> dict:
    """Turn one extracted event into a scored, storable row."""
    paths = event["paths"][:_MAX_TRACKED_PATHS]
    adopted_rank: int | None = None
    outside_reads: list[str] = []
    for read in event["reads"]:
        matched = None
        for rank, rp in enumerate(paths, start=1):
            if _paths_match(read, rp):
                matched = rank
                break
        if matched is not None:
            if adopted_rank is None or matched < adopted_rank:
                adopted_rank = matched
        else:
            outside_reads.append(read)

    grep_count = len(event["greps"])
    if adopted_rank is not None and not outside_reads and grep_count == 0:
        verdict = "adopted"
    elif adopted_rank is not None:
        verdict = "mixed"
    elif outside_reads or grep_count:
        verdict = "betrayed"
    else:
        verdict = "no_followup"

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": event["query"],
        "n_results": len(event["paths"]),
        "top_paths": paths[:5],
        "adopted_rank": adopted_rank,
        "outside_reads": outside_reads[:5],
        "greps_after": grep_count,
        "verdict": verdict,
    }


def _relativize(paths: list[str], project_root: Path) -> list[str]:
    """Store project files root-relative so the regression set is portable."""
    prefix = str(project_root).rstrip("/") + "/"
    return [p[len(prefix):] if p.startswith(prefix) else p for p in paths]


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_turn(project_root: Path, turn_records: list[dict]) -> int:
    """Score one finished turn and persist rows. Returns rows written.

    Hook-context entry point: swallows every exception, returns 0 on any
    failure — a scoring bug must never break the user's session.
    """
    try:
        events = extract_turn_events(turn_records)
        if not events:
            return 0
        base = project_root / _SELFEVAL_DIR
        written = 0
        for event in events:
            if not event["query"]:
                continue
            scored = score_event(event)
            row = {
                **scored,
                "outside_reads": _relativize(scored["outside_reads"], project_root),
            }
            _append_jsonl(base / _EVENTS_FILE, row)
            written += 1
            # A betrayal where we saw what the agent actually used is a
            # ready-made benchmark item: the query, and the files that
            # turned out to matter. This is the compounding step.
            if row["verdict"] in ("betrayed", "mixed") and row["outside_reads"]:
                _append_jsonl(
                    base / _HARVESTED_FILE,
                    {
                        "ts": row["ts"],
                        "query": row["query"],
                        "gold_paths": row["outside_reads"],
                        "served_paths": row["top_paths"],
                        "verdict": row["verdict"],
                    },
                )
        return written
    except Exception:
        return 0


def summarize(project_root: Path, *, days: int = 7) -> dict | None:
    """Aggregate recent events. None when there is nothing to report."""
    try:
        events_path = project_root / _SELFEVAL_DIR / _EVENTS_FILE
        if not events_path.is_file():
            return None
        lines = events_path.read_text(encoding="utf-8").splitlines()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        counts = {"adopted": 0, "mixed": 0, "betrayed": 0, "no_followup": 0}
        total = 0
        for line in lines[-_SUMMARY_READ_LINES:]:
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row["ts"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts < cutoff:
                continue
            verdict = row.get("verdict")
            if verdict in counts:
                counts[verdict] += 1
                total += 1
        if total == 0:
            return None
        harvested_path = project_root / _SELFEVAL_DIR / _HARVESTED_FILE
        harvested = 0
        if harvested_path.is_file():
            harvested = sum(
                1 for line in harvested_path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
        return {"days": days, "total": total, "harvested_total": harvested, **counts}
    except Exception:
        return None


def format_summary_line(project_root: Path, *, days: int = 7) -> str:
    """One-line scorecard for SessionStart injection. '' when silent."""
    stats = summarize(project_root, days=days)
    if stats is None:
        return ""
    return (
        f"[selfeval {stats['days']}d] searches {stats['total']} · "
        f"adopted {stats['adopted'] + stats['mixed']} · "
        f"betrayed {stats['betrayed']} · "
        f"harvested {stats['harvested_total']} regression items"
    )
