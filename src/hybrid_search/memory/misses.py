"""Missed-recall log — "we talked about this before and it can't find it".

The one signal selfeval cannot produce: a user noticing that the memory
layer failed them. When that happens, ``hybrid-search-mcp miss "<what>"``
appends one row here, with the searches that ran just before, so the case
can later become a regression question for the query-judge runner
(plan ``2026-09-27-open-the-doors`` §3 0-4).

The file lives OUTSIDE every repo, under ``~/.hybrid-search/benchmarks/``:
the rows quote what the user was looking for, and this repository is
public (CLAUDE.md "공개물에 코퍼스를 인용하지 말 것").

A CLI command, not an MCP tool — misses are rare, and every MCP tool costs
context on every turn.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hybrid_search.memory import selfeval

# What the user says they were looking for; longer input is clipped, not
# rejected — a pasted paragraph is still a usable miss.
MAX_QUESTION_CHARS = 2000
# Searches in this window before the miss are the ones that failed it.
RECENT_WINDOW_HOURS = 6
RECENT_LIMIT = 5
_QUERY_CLIP = 300


def misses_path(project: str) -> Path:
    home = os.environ.get("HYBRID_SEARCH_HOME") or (Path.home() / ".hybrid-search")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in project) or "unknown"
    return Path(home) / "benchmarks" / f"misses-{safe}.jsonl"


def _parse_ts(raw: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def recent_queries(
    project_root: Path,
    *,
    now: datetime | None = None,
    hours: int = RECENT_WINDOW_HOURS,
    limit: int = RECENT_LIMIT,
) -> list[dict]:
    """Newest-first searches (tool calls and pre-fetches) before a miss.

    Read from what selfeval already records: scored rows, plus pre-fetches
    still waiting for their turn to finish — a miss is often reported in
    the very turn whose search just failed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    base = project_root / selfeval._SELFEVAL_DIR
    rows = [
        {**r, "source": r.get("source") or "tool"}
        for r in selfeval._read_jsonl(base / selfeval._EVENTS_FILE)
    ]
    pending_dir = base / selfeval._PENDING_DIR
    if pending_dir.is_dir():
        for path in sorted(pending_dir.glob("*.jsonl")):
            if path.name.endswith(selfeval._CLAIMED_SUFFIX):
                continue  # already scored — its row is in events.jsonl
            rows.extend({**r, "source": "prefetch"} for r in selfeval._read_jsonl(path))

    picked: list[tuple[datetime, dict]] = []
    for r in rows:
        ts = _parse_ts(r.get("ts"))
        query = (r.get("query") or "").strip()
        if ts is None or not query or not (cutoff <= ts <= now):
            continue
        picked.append((ts, {
            "ts": ts.isoformat(timespec="seconds"),
            "source": r["source"],
            "query": query[:_QUERY_CLIP],
            "top_paths": list(r.get("top_paths") or [])[:5],
            "verdict": r.get("verdict"),
        }))
    picked.sort(key=lambda p: p[0], reverse=True)
    out: list[dict] = []
    seen: set[str] = set()
    for _, row in picked:
        if row["query"] in seen:
            continue
        seen.add(row["query"])
        out.append(row)
        if len(out) >= limit:
            break
    return out


def record_miss(
    project_root: Path,
    question: str,
    *,
    now: datetime | None = None,
) -> tuple[Path, dict]:
    """Append one miss. Returns (file, row). Raises ValueError on empty input."""
    text = " ".join((question or "").split())
    if not text:
        raise ValueError("describe what you were looking for, e.g. miss \"환불 정책 결정 이유\"")
    now = now or datetime.now(timezone.utc)
    project = project_root.name
    row = {
        "ts": now.isoformat(timespec="seconds"),
        "project": project,
        "project_path": str(project_root),
        "question": text[:MAX_QUESTION_CHARS],
        "recent_queries": recent_queries(project_root, now=now),
    }
    path = misses_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path, row
