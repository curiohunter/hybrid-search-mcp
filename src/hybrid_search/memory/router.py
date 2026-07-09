"""Router-adjacent quality signal helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


CONFIDENCE_BANDS = ("strong", "mixed", "weak")


@dataclass(frozen=True)
class RouterDecision:
    tool: str
    reason: str


_BACKTICK_RE = re.compile(r"`([^`\s]+)`")
_CAMEL_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*\b")
_PATH_RE = re.compile(r"(?:^|\s)([\w./-]+\.(?:ts|tsx|js|jsx|py|rs|go|sql))\b")
_GLOB_EXT_RE = re.compile(r"\*\.(?:ts|tsx|js|jsx|py|rs|go|sql)\b")
_ERROR_RE = re.compile(r"(?:\bTraceback\b|\bError:|\bException:|\bat\s+line\s+\d+\b)")
_TOKEN_RE = re.compile(r"`[^`]+`|[\w./*-]+\.(?:ts|tsx|js|jsx|py|rs|go|sql)|[A-Za-z_][\w.-]*")
_MEMORY_TOKENS_KO = ("지난번", "이전에", "왜 이렇게 결정")
_MEMORY_TOKENS_EN_RE = re.compile(
    r"\b(?:last\s+time|previously|earlier\s+we\s+decided)\b",
    re.IGNORECASE,
)
_EXPLORATORY_TOKENS_EN_RE = re.compile(
    r"\b(?:why|how\s+does|flow\s+of|where\s+is\s+.+?\s+handled)\b",
    re.IGNORECASE,
)


# A runner-up within this fraction of the top score is a tie. Relative, not
# absolute: RRF scores live on a fixed ~1/(k+rank) scale whose absolute
# spread shrinks as the corpus grows, so an absolute floor (the old
# ``gap < 0.001``) misreads dense-but-correct heads on large projects as
# weak. See docs: 2026-07-09 self-pollution audit.
_TIE_RATIO = 0.02


def classify_confidence(
    top_score: float,
    gap: float | None,
    thresholds: Mapping[str, float],
    *,
    coherent: bool = False,
) -> str:
    """Classify a search response into strong/mixed/weak confidence.

    ``gap`` should be the *effective* gap — distance from the top hit to the
    first result anchored in a different file — so sibling chunks of one file
    don't register as ambiguity. ``coherent`` marks a head whose top results
    share one directory/module: a near-tie there means several good answers
    to an exploratory question, not a confused ranking.
    """
    if top_score == 0:
        return "weak"
    if gap is not None and gap < top_score * _TIE_RATIO:
        if coherent or top_score >= thresholds["strong_score"]:
            return "mixed"
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


def classify_prompt(prompt: str) -> RouterDecision:
    """Classify a user prompt into a suggested retrieval tool."""
    text = prompt or ""
    if has_identifier_shape_token(text):
        if _BACKTICK_RE.search(text):
            return RouterDecision("grep", "exact identifier")
        if _ERROR_RE.search(text):
            return RouterDecision("grep", "error trace")
        if _GLOB_EXT_RE.search(text) or _PATH_RE.search(text):
            return RouterDecision("grep", "file path")
        if any(len(m.group(0)) >= 8 for m in _CAMEL_RE.finditer(text)):
            return RouterDecision("grep", "exact identifier")
        return RouterDecision("grep", "exact identifier")

    if any(tok in text for tok in _MEMORY_TOKENS_KO):
        return RouterDecision("memory", "history reference")
    if _MEMORY_TOKENS_EN_RE.search(text):
        return RouterDecision("memory", "history reference")

    from hybrid_search.memory.hook_runtime import _EXPLORATORY_TOKENS_KO

    if any(tok in text for tok in _EXPLORATORY_TOKENS_KO):
        return RouterDecision("hybrid_search", "exploratory NL")
    if _EXPLORATORY_TOKENS_EN_RE.search(text):
        return RouterDecision("hybrid_search", "exploratory NL")

    return RouterDecision("hybrid_search", "default")


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


def fallback_hint(prompt: str, top_hit: str | None = None) -> str:
    """Return a short alternative-tool hint for a weak hybrid_search result.

    ``top_hit`` (the best result's file path) turns "weak = discard" into
    "weak = low-trust candidate": the caller gets a concrete place to start
    reading even when the ranking as a whole is ambiguous.
    """
    tool = "grep" if has_identifier_shape_token(prompt) else "wiki"
    token = distinctive_token(prompt)
    hint = f"weak match -> {tool} `{token}`"
    if top_hit:
        hint += f" (top hit: {top_hit})"
    return hint
