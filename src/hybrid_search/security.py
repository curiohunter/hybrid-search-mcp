"""MCP input/output trust boundary — defensive sanitizers.

The MCP tool surface receives arbitrary input from an upstream LLM. Everything
that crosses the JSON-RPC boundary must be treated as untrusted. These helpers
are cheap, idempotent, and side-effect free — call them at every entry point.

Design notes:
- ``sanitize_*`` strips control characters and caps length.
- ``clamp_*`` coerces numerics to a declared range and rejects NaN / non-numeric.
- ``validate_project_name`` / ``validate_project_path`` enforce structural rules
  (charset, containment) and raise ``ValueError`` on violations.
- None inputs are passed through unchanged for the optional-parameter case,
  so call sites don't need to guard.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

# C0 + DEL, keeping \t (\x09), \n (\x0a), \r (\x0d) intact so JSON/code survives.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Project names: alphanumeric + dash/underscore/dot, 1..64 chars, no leading dot.
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


def sanitize_query(text: str, max_len: int = 2048) -> str:
    """Strip control chars and cap length for a user-facing search query."""
    if not isinstance(text, str):
        raise TypeError(f"query must be str, got {type(text).__name__}")
    clean = _CONTROL_CHAR_RE.sub("", text)
    return clean[:max_len]


def sanitize_snippet(text: str, max_len: int = 8192) -> str:
    """Strip control chars from code/content before returning it to the LLM.

    Code chunks may contain stray control bytes from minified sources or
    binary-ish payloads. Those can break JSON serialization or inject ANSI
    escape sequences into a downstream terminal renderer.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    clean = _CONTROL_CHAR_RE.sub("", text)
    return clean[:max_len]


def sanitize_file_pattern(pattern: str | None, max_len: int = 256) -> str | None:
    """Length-cap a glob pattern and strip control chars."""
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise TypeError("file_pattern must be str")
    clean = _CONTROL_CHAR_RE.sub("", pattern)
    return clean[:max_len]


def sanitize_node_types(
    items: list[str] | None,
    max_items: int = 32,
    max_item_len: int = 64,
) -> list[str] | None:
    """Defensive normalization of a node_types list.

    Returns ``None`` unchanged. Rejects non-list input. Drops non-str entries
    and entries that become empty after sanitization. Caps both list length
    and per-item length.
    """
    if items is None:
        return None
    if not isinstance(items, list):
        raise TypeError("node_types must be a list")
    out: list[str] = []
    for item in items[:max_items]:
        if not isinstance(item, str):
            continue
        cleaned = _CONTROL_CHAR_RE.sub("", item).strip()[:max_item_len]
        if cleaned:
            out.append(cleaned)
    return out


def validate_project_name(name: str | None) -> str | None:
    """Accept a ``ProjectRegistry`` key. Reject control chars / path separators."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise TypeError("project must be str")
    if not _PROJECT_NAME_RE.match(name):
        raise ValueError(f"invalid project name: {name!r}")
    return name


def validate_project_path(path: str, base: Path) -> Path:
    """Resolve ``path`` under ``base`` and reject any ``..`` escape.

    Raises ``ValueError`` if the resolved path escapes ``base``.
    """
    if not isinstance(path, str):
        raise TypeError("path must be str")
    base_resolved = base.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base_resolved / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"path outside project: {path}") from exc
    return resolved


def sanitize_cwd(cwd: str | None, max_len: int = 4096) -> str | None:
    """Sanitize a ``cwd`` hint used for registry lookup (not disk reads)."""
    if cwd is None:
        return None
    if not isinstance(cwd, str):
        raise TypeError("cwd must be str")
    return _CONTROL_CHAR_RE.sub("", cwd)[:max_len]


def clamp_int(value: int, lo: int, hi: int, *, name: str = "value") -> int:
    """Coerce an int into ``[lo, hi]``. Rejects non-ints (including bool)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def clamp_float(value: float, lo: float, hi: float, *, name: str = "value") -> float:
    """Coerce a float into ``[lo, hi]``. Rejects NaN and non-numeric input."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be float, not bool")
    if not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be number, got {type(value).__name__}")
    v = float(value)
    if math.isnan(v):
        raise ValueError(f"{name} must not be NaN")
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v
