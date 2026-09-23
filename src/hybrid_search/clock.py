"""The one clock ranking reads — and the way a measurement pins it.

Ranking ages memory records against "now" (the recency boost's half-life,
the "12d ago" label). A frozen index snapshot freezes the corpus but not
that clock, so the same snapshot measured ten days later scores differently
with identical code and corpus (2026-09-23: pinning the clock to the
snapshot date moved Set A top3 0.20 → 0.35 on its own).

A measurement pins the clock by setting ``HYBRID_SEARCH_NOW`` to an
ISO-8601 instant. ``cycle.py``'s ``freeze()`` writes that instant into the
snapshot (``stamp_snapshot``), and every runner that takes ``--config``
reads it back from beside the config (``pin_to_snapshot``), so a snapshot
carries its own clock and a rerun cannot forget it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

NOW_ENV = "HYBRID_SEARCH_NOW"
SNAPSHOT_CLOCK_FILE = "frozen_at"


def parse_instant(value: str) -> datetime:
    """ISO-8601 → aware UTC datetime. A bare date or naive time is UTC."""
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now() -> datetime:
    """The pinned instant when a measurement set one, else the wall clock.

    An unparseable pin falls back to the wall clock with a warning rather
    than failing the search: the variable only matters to measurement runs,
    and ``pin_to_snapshot`` validates before it sets it.
    """
    pinned = os.environ.get(NOW_ENV, "").strip()
    if pinned:
        try:
            return parse_instant(pinned)
        except ValueError:
            logger.warning("%s=%r is not ISO-8601; using the wall clock",
                           NOW_ENV, pinned)
    return datetime.now(timezone.utc)


def stamp_snapshot(snapshot_dir: Path, when: datetime | None = None) -> str:
    """Record the instant a snapshot was taken, inside the snapshot."""
    instant = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    text = instant.isoformat(timespec="seconds")
    (Path(snapshot_dir) / SNAPSHOT_CLOCK_FILE).write_text(text + "\n",
                                                          encoding="utf-8")
    return text


def pin_to_snapshot(config_path: Path | str | None) -> str | None:
    """Pin the clock to the snapshot that ``config_path`` points into.

    Returns the pinned instant, or None when nothing could be pinned (live
    index, or a snapshot taken before snapshots carried a clock). An
    explicit ``HYBRID_SEARCH_NOW`` already in the environment wins — that
    is how an old snapshot is measured at a chosen instant.
    """
    existing = os.environ.get(NOW_ENV, "").strip()
    if existing:
        parse_instant(existing)  # fail loudly: a bad pin is a bad measurement
        return existing
    if config_path is None:
        return None
    marker = Path(config_path).expanduser().parent / SNAPSHOT_CLOCK_FILE
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parse_instant(text)
    os.environ[NOW_ENV] = text
    return text
