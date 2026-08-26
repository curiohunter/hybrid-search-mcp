"""A local record of every paid API call this tool makes.

Without it, "where did the money go" can only be answered by inference.
That failed repeatedly during the Gemini migration: three different
explanations were offered for one day's spend and two of them were wrong,
because nothing on this side knew what had actually been sent.

The log is append-only JSONL, one line per successful call. It records
what was sent, never the content — the point is accounting, not a copy of
the corpus. Failures to write are swallowed: accounting must never be the
reason indexing stops.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Opt-out for anyone who would rather not keep the record.
DISABLE_ENV = "HYBRID_SEARCH_NO_USAGE_LOG"


def log_path() -> Path:
    root = os.environ.get("HYBRID_SEARCH_HOME") or (Path.home() / ".hybrid-search")
    return Path(root) / "usage.jsonl"


def record(*, kind: str, provider: str, model: str, items: int, tokens: int) -> None:
    """Append one call. ``kind`` is "embed" or "chat"; ``tokens`` is an estimate."""
    if os.environ.get(DISABLE_ENV) == "1":
        return
    row = {
        "ts": round(time.time(), 3),
        "kind": kind,
        "provider": provider,
        "model": model,
        "items": items,
        "tokens": tokens,
        "pid": os.getpid(),
    }
    try:
        path = log_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
    except OSError:
        logger.debug("usage log write failed", exc_info=True)


def summarize(since: float | None = None) -> dict:
    """Totals per (kind, model), optionally since a unix timestamp."""
    totals: dict[tuple[str, str], dict] = {}
    first = last = None
    try:
        with open(log_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn line must not hide the rest
                ts = row.get("ts", 0)
                if since is not None and ts < since:
                    continue
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
                key = (row.get("kind", "?"), row.get("model", "?"))
                agg = totals.setdefault(key, {"calls": 0, "items": 0, "tokens": 0})
                agg["calls"] += 1
                agg["items"] += int(row.get("items") or 0)
                agg["tokens"] += int(row.get("tokens") or 0)
    except OSError:
        pass
    return {"totals": totals, "first": first, "last": last}
