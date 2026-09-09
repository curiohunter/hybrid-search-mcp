"""Structure-aware claim splitting — a probe, not part of the search path.

This lives beside the probes rather than in ``src/`` because it lost the
measurement it was written for. On 2026-09-09 it was compared against plain
sentence splitting under identical conditions and scored no better on the
distilled gold set and WORSE on the raw one (found 0.37 vs 0.51), because its
rules drop sentences that carry answers. It buys a 3.6x smaller unit count and
nothing else. Kept so the comparison stays reproducible; shipping it in the
wheel would hand users dead weight.

Original rationale follows.


The code lane never had the retrieval problem the memory lanes have, and the
reason is boundaries: tree-sitter tells the indexer where a function ends, and
a markdown parser where a section ends, so a code chunk is already "a unit that
can be an answer". A conversation turn is a blob — a user request plus up to
1,800 characters of assistant prose plus tool names — so when the asked-about
fact is one sentence in it, that fact is ~2% of the text the retrievers score,
and both of them miss it (2026-09-08 probes: cos(query, sentence) beats
cos(query, chunk) in every category, and a sentence-level index moved parent
top-3 from 0/10 to 6/10).

This module gives the memory lanes their boundaries. It splits on the grammar
the corpus actually uses rather than on punctuation:

* fenced code and shell transcript are removed first — they are evidence, not
  claims, and the conversation quality gate already treats them that way;
* a bolded assertion, a table row, and a numbered or bulleted item are each one
  claim, because that is how this corpus writes conclusions;
* remaining prose is grouped by paragraph, not by sentence, so a claim keeps
  the clause that qualifies it.

Splitting is deliberately lossless at the parent level: every claim carries the
offset it came from, and the parent chunk stays exactly as it is. Children are
for matching; the parent is still what gets returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Fenced blocks, and the shell-transcript shapes this corpus produces outside
# fences (a command echo followed by its output).
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
_SHELL_LINE_RE = re.compile(
    r"^\s*(?:\$|>|#\s*\$|[A-Za-z0-9_./-]+@[A-Za-z0-9_.-]+[:%$])"
    r"|^\s*(?:echo|grep|rg|ls|cat|sed|awk|curl|git|npm|python3?|psql)\s"
)
# `[claude turn]` / `[codex turn]` envelope and image-paste boilerplate.
_ENVELOPE_RE = re.compile(r"^\[(?:claude|codex) turn\]\s*", re.M)
_IMAGE_RE = re.compile(r"\[Image:[^\]]*\]")

_BOLD_LINE_RE = re.compile(r"\*\*(?P<body>[^*]{8,})\*\*")
_LIST_RE = re.compile(r"^\s*(?:[-*·]|\d+[.)]|[①-⑳])\s+(?P<body>.+)$")
_TABLE_RE = re.compile(r"^\s*\|(?P<body>.+)\|\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(?P<body>.+)$")
_TABLE_RULE_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

CLAIM_MIN_CHARS = 12
CLAIM_MAX_CHARS = 400

# Sections of a structured memory file that are provenance, not assertion. A qa
# log's "## Top results" is a list of what the search returned; splitting it
# yields one "claim" per result path, which is how the first measurement got
# 26 claims per qa log with `list` as the dominant kind. The answer is in the
# excerpt; everything after it is the receipt.
_BODY_SECTIONS = {
    "qa_log": ("## Answer excerpt",),
    "memory_card": ("## Summary", "## Decisions", "## Followups"),
}
_STOP_SECTIONS = ("## Top results", "## Files", "## Evidence", "## When to use")
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)


def body_for_claims(text: str, node_type: str | None) -> str:
    """The part of a memory chunk that can contain an assertion.

    Conversation turns are all body. Structured memory files are not: their
    frontmatter is metadata and their trailing sections are provenance, and
    splitting those produces one claim per file path.
    """
    text = _FRONTMATTER_RE.sub("", text or "", count=1)
    wanted = _BODY_SECTIONS.get(node_type or "")
    if not wanted:
        return text
    parts: list[str] = []
    for heading in wanted:
        if heading not in text:
            continue
        rest = text.split(heading, 1)[1]
        cut = len(rest)
        for stop in _STOP_SECTIONS:
            i = rest.find(stop)
            if 0 <= i < cut:
                cut = i
        nxt = rest.find("\n## ")
        if 0 <= nxt < cut:
            cut = nxt
        parts.append(rest[:cut])
    return "\n".join(parts) if parts else text


@dataclass(frozen=True)
class Claim:
    """One retrievable assertion inside a parent chunk."""

    text: str
    kind: str  # bold | list | table | heading | prose | request


def _strip_evidence(text: str) -> str:
    """Remove fenced blocks, shell transcript lines, and paste boilerplate."""
    out = _FENCE_RE.sub("\n", text or "")
    out = _IMAGE_RE.sub(" ", out)
    out = _ENVELOPE_RE.sub("", out)
    kept = [ln for ln in out.splitlines() if not _SHELL_LINE_RE.search(ln)]
    return "\n".join(kept)


def _clean(body: str) -> str:
    body = body.replace("**", " ").replace("`", "")
    return re.sub(r"\s+", " ", body).strip(" \t|-–—·:;,")


def _emit(body: str, kind: str, out: list[Claim], seen: set[str]) -> None:
    body = _clean(body)
    if not (CLAIM_MIN_CHARS <= len(body) <= CLAIM_MAX_CHARS):
        return
    if not re.search(r"[가-힣A-Za-z]", body):
        return
    if body in seen:
        return
    seen.add(body)
    out.append(Claim(text=body, kind=kind))


def split_claims(text: str, *, request: str | None = None) -> list[Claim]:
    """Split one memory chunk into retrievable claims.

    ``request`` is the user utterance when the caller has it separately; a
    conversation turn's own prompt is a claim of its own kind, because "what
    was asked" is a thing people search for.
    """
    claims: list[Claim] = []
    seen: set[str] = set()
    if request:
        _emit(request, "request", claims, seen)

    body = _strip_evidence(text)
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            _emit(" ".join(paragraph), "prose", claims, seen)
            paragraph.clear()

    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if _TABLE_RULE_RE.match(line):
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush()
            _emit(m.group("body"), "heading", claims, seen)
            continue
        m = _TABLE_RE.match(line)
        if m:
            flush()
            _emit(m.group("body").replace("|", " · "), "table", claims, seen)
            continue
        m = _LIST_RE.match(line)
        if m:
            flush()
            _emit(m.group("body"), "list", claims, seen)
            continue
        bolds = _BOLD_LINE_RE.findall(line)
        if bolds:
            # A bolded assertion is the conclusion of its line; keep the whole
            # line so the assertion keeps its subject, and do not also fold the
            # line into the surrounding paragraph.
            flush()
            _emit(line, "bold", claims, seen)
            continue
        paragraph.append(line.strip())
    flush()
    return claims
