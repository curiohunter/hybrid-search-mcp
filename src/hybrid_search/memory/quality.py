"""Memory quality gate — decide what is worth remembering.

The Stop hook records every turn, but a turn "query" is only recall-worthy
when it is an actual user question. In practice the transcript extractor
also hands us model-output debris — divider lines, bullet fragments,
harness notifications — and before this gate existed ~75% of stored TURN
entries were that debris (2026-07-09 self-pollution audit). Junk entries
poison two consumers: hook injection (irrelevant "past Q&A" context) and
the retrieval index (near-duplicate noise that collapses confidence gaps).
"""

from __future__ import annotations

import re

# Markers of harness/system chatter that never represents a user question.
_HARNESS_MARKERS = (
    "<task-notification>",
    "<teammate-message",
    "<system-reminder>",
    "<command-name>",
    "<local-command-stdout>",
    "[SYSTEM NOTIFICATION",
    "Another Claude session sent",
)

# A query starting with one of these is a fragment of rendered model output
# (markdown furniture, dividers, box-drawing), not something a user typed.
_JUNK_LEADING_CHARS = '─━═•·|>#*`~╭╰│"'

_TOKEN_RE = re.compile(r"[\w가-힣]+")


def is_junk_query(query: str | None) -> bool:
    """True when ``query`` is output debris rather than a user question."""
    q = (query or "").strip()
    if len(q) < 4:
        return True
    if q[0] in _JUNK_LEADING_CHARS:
        return True
    if any(marker in q for marker in _HARNESS_MARKERS):
        return True
    # Path-only fragments ("src/foo/bar.py") carry no question.
    if len(q.split()) == 1 and "/" in q:
        return True
    # Divider/ASCII-art lines: almost no alphanumeric signal.
    informative = sum(1 for c in q if c.isalnum())
    if informative / len(q) < 0.3:
        return True
    return False


def query_tokens(text: str | None) -> set[str]:
    """Casefolded word tokens (≥2 chars) for cheap near-dup comparison."""
    return {t for t in _TOKEN_RE.findall((text or "").casefold()) if len(t) >= 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
