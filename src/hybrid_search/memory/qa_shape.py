"""What a qa record owns — the one place that decides it.

Two records are written per turn: one when the question arrives (the
pre-fetch or the MCP tool call), one when it is answered (the Stop hook).
Only the second owns anything beyond its question. The first one's body is
``## Top results`` — snippets of OTHER chunks, quoted.

"Does it carry an answer" used to be ``"## Answer excerpt" in content``.
A pre-fetch record that quotes another qa record carries that string inside
the quotation, so it read as answered, and ``split("## Answer excerpt")[1]``
handed back the rest of the quotation list as its "answer" — which then
fed topic matching everywhere (2026-09-23: 11 of 323 records here, 49 of
~2,000 in the largest dogfood corpus).

The heading at the START of a line is the owned answer. Snippets are
flattened to one line when written (``qa_log._truncate``), so a quotation
can never produce one. The frontmatter ``answer_excerpt_chars`` field is
not the signal: Reflector notes and older records carry an answer without it.
"""

from __future__ import annotations

import re

_ANSWER_HEADING = re.compile(r"^## Answer excerpt[ \t]*$", re.MULTILINE)
_RESULTS_HEADING = re.compile(r"^## Top results[ \t]*$", re.MULTILINE)


def _answer_heading(content: str) -> re.Match[str] | None:
    """The owned answer heading — above the quoted results, never inside."""
    results = _RESULTS_HEADING.search(content)
    head = content[: results.start()] if results else content
    return _ANSWER_HEADING.search(head)


def owns_answer(content: str | None) -> bool:
    """True when this qa record carries an answer of its own."""
    return bool(content) and _answer_heading(content) is not None


def answer_excerpt(content: str | None) -> str:
    """The record's own answer text, or ``""`` when it owns none."""
    if not content:
        return ""
    match = _answer_heading(content)
    if match is None:
        return ""
    rest = content[match.end():]
    results = _RESULTS_HEADING.search(rest)
    return (rest[: results.start()] if results else rest).strip()
