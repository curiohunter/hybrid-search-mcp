"""Install-time warnings that `setup` and `doctor` share.

Two silent failures cost real data before they had a message:

* Claude Code deletes transcripts older than ``cleanupPeriodDays`` (30 by
  default), so conversation memory has a 30-day lifetime unless the user
  raises it. We only tell them — it is their setting, not ours to edit.
* ``discover_modules`` once failed on every run while the pipeline carried
  on, leaving a project with zero modules and no sign of it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_CLEANUP_DAYS = 30


def claude_cleanup_days(settings_path: Path | None = None) -> int | None:
    """Return the explicit ``cleanupPeriodDays``, or None when it is unset."""
    path = settings_path or Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    value = data.get("cleanupPeriodDays") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def retention_notice(settings_path: Path | None = None) -> str | None:
    """Warn when transcripts will be deleted after the 30-day default."""
    days = claude_cleanup_days(settings_path)
    if days is not None and days > DEFAULT_CLEANUP_DAYS:
        return None
    lifetime = (
        f"cleanupPeriodDays is {days}" if days is not None else "cleanupPeriodDays is not set (default 30)"
    )
    return (
        f"{lifetime}: Claude Code deletes conversation transcripts after that, "
        "so conversation memory only reaches back that far. "
        'To keep it longer, set "cleanupPeriodDays" in ~/.claude/settings.json '
        "(a large number such as 3650; 0 turns retention off entirely)."
    )


def module_count(store_db: Path) -> int | None:
    """Count modules in an index, or None when the index cannot be read."""
    if not store_db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM modules").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else 0


def modules_notice(store_db: Path) -> str | None:
    """Warn when an indexed project has no modules."""
    if module_count(store_db) != 0:
        return None
    return (
        "The index has 0 modules, so module-level search is off. "
        "Run `hybrid-search-mcp reindex --cwd . --force`; if it stays 0, "
        "module discovery is failing."
    )
