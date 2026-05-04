"""Router-adjacent quality signal helpers.

Phase 2 only owns result confidence and fallback hints. Prompt tool-routing
classification is intentionally left out for the later router phase.
"""

from __future__ import annotations

import re
from collections.abc import Mapping


CONFIDENCE_BANDS = ("strong", "mixed", "weak")

_BACKTICK_RE = re.compile(r"`([^`\s]+)`")
_CAMEL_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*\b")
_PATH_RE = re.compile(r"(?:^|\s)([\w./-]+\.(?:ts|tsx|js|jsx|py|rs|go|sql))\b")
_GLOB_EXT_RE = re.compile(r"\*\.(?:ts|tsx|js|jsx|py|rs|go|sql)\b")
_ERROR_RE = re.compile(r"(?:\bTraceback\b|\bError:|\bException:|\bat\s+line\s+\d+\b)")
_TOKEN_RE = re.compile(r"`[^`]+`|[\w./*-]+\.(?:ts|tsx|js|jsx|py|rs|go|sql)|[A-Za-z_][\w.-]*")


def classify_confidence(
    top_score: float,
    gap: float | None,
    thresholds: Mapping[str, float],
) -> str:
    """Classify a search response into strong/mixed/weak confidence."""
    if top_score == 0:
        return "weak"
    if gap is not None and gap < 0.001:
        return "weak"
    if (
        gap is not None
        and top_score >= thresholds["strong_score"]
        and gap >= thresholds["strong_gap"]
    ):
        return "strong"
    if top_score >= thresholds["weak_score"]:
        return "mixed"
    return "weak"


def has_identifier_shape_token(prompt: str) -> bool:
    """Return True when grep/read is likely the better fallback lane."""
    text = prompt or ""
    if _BACKTICK_RE.search(text):
        return True
    if _GLOB_EXT_RE.search(text) or _PATH_RE.search(text):
        return True
    if _ERROR_RE.search(text):
        return True
    return any(len(m.group(0)) >= 8 for m in _CAMEL_RE.finditer(text))


def distinctive_token(prompt: str) -> str:
    """Pick a compact, recognizable token for a weak-result fallback hint."""
    text = (prompt or "").strip()
    backtick = _BACKTICK_RE.search(text)
    if backtick:
        return backtick.group(1)
    path = _PATH_RE.search(text)
    if path:
        return path.group(1)
    camel = next((m.group(0) for m in _CAMEL_RE.finditer(text) if len(m.group(0)) >= 8), None)
    if camel:
        return camel
    tokens = [m.group(0).strip("`") for m in _TOKEN_RE.finditer(text)]
    if not tokens:
        return text[:32] or "query"
    return max(tokens, key=lambda t: (len(t), t.lower()))[:48]


def fallback_hint(prompt: str) -> str:
    """Return a short alternative-tool hint for a weak hybrid_search result."""
    tool = "grep" if has_identifier_shape_token(prompt) else "wiki"
    token = distinctive_token(prompt)
    return f"weak match -> {tool} `{token}`"
