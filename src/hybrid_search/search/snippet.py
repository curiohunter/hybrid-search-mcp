"""Snippet generation — hit-aware preview around query matches.

Earlier versions returned the first 5 lines (or first 200 chars of docstring),
forcing callers to Read the file again to see the actual matching context.
This module centers the snippet on the line where the query first hits.
"""

from __future__ import annotations

import re


SNIPPET_MAX_CHARS = 400
CONTEXT_LINES = 5
DOCSTRING_FALLBACK_CHARS = 400
HEAD_FALLBACK_LINES = 10
_MIN_ENGLISH_TOKEN = 3
_MIN_KOREAN_TOKEN = 2


_ALNUM_RE = re.compile(r"[A-Za-z0-9_]+")
_KOREAN_RE = re.compile(r"[\uac00-\ud7a3]+")


def _query_tokens(query: str | None) -> list[str]:
    """Extract searchable tokens; English >=3 chars (lowercased), Korean >=2."""
    if not query:
        return []
    eng = [t.lower() for t in _ALNUM_RE.findall(query) if len(t) >= _MIN_ENGLISH_TOKEN]
    kor = [t for t in _KOREAN_RE.findall(query) if len(t) >= _MIN_KOREAN_TOKEN]
    return eng + kor


def _find_hit_line(lower_lines: list[str], tokens: list[str]) -> int | None:
    for i, line in enumerate(lower_lines):
        if any(t in line for t in tokens):
            return i
    return None


def make_snippet(
    docstring: str | None,
    content: str | None,
    query: str | None = None,
    *,
    node_type: str | None = None,
) -> str:
    """Build a snippet, preferring a hit-centered window over head-fallback."""
    if node_type == "memory_card":
        content = _strip_frontmatter(content)

    if content:
        tokens = _query_tokens(query)
        if tokens:
            lines = content.split("\n")
            lower_lines = [ln.lower() for ln in lines]
            hit = _find_hit_line(lower_lines, tokens)
            if hit is not None:
                start = max(0, hit - CONTEXT_LINES)
                end = min(len(lines), hit + CONTEXT_LINES + 1)
                window = "\n".join(lines[start:end])
                return window[:SNIPPET_MAX_CHARS]

    if docstring:
        return docstring[:DOCSTRING_FALLBACK_CHARS]

    if content:
        lines = content.strip().split("\n")
        return "\n".join(lines[:HEAD_FALLBACK_LINES])[:SNIPPET_MAX_CHARS]

    return ""


def _strip_frontmatter(content: str | None) -> str | None:
    """Drop YAML frontmatter from memory-card markdown before previewing."""
    if not content:
        return content
    text = content.lstrip()
    if not text.startswith("---\n"):
        return content
    end = text.find("\n---", 4)
    if end < 0:
        return content
    rest = text[end + len("\n---"):]
    return rest.lstrip("\n")
