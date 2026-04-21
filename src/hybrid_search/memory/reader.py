"""Q&A log reader — list/show/grep over persisted hybrid_search responses.

Companion to ``qa_log.py`` (writer). Parses the YAML frontmatter + markdown
body format produced by ``qa_log._format_record`` without pulling in a YAML
dependency — the format is fixed and simple.

Layout on disk:
    <project_root>/.hybrid-search/qa/YYYY/MM/DD-HHMMSS-<hash8>.md

Reader surface:
    iter_qa_files(root)       — yields Path, newest mtime first
    parse_qa_index(path)      — returns QAIndex (metadata only) or None
    iter_qa_indexes(root)     — combines the two, swallowing parse errors
    read_qa_body(path)        — returns markdown body (without frontmatter)
    find_qa_by_id(root, tok)  — resolves stem / hash-prefix / YYYY-MM-DD-… id
    grep_qa(root, term)       — yields GrepHit over frontmatter + body
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


QA_DIRNAME = ".hybrid-search/qa"
FRONTMATTER_DELIM = "---"


@dataclass(frozen=True)
class QAIndex:
    """Metadata-only view of a qa log entry. Body is loaded on demand."""

    path: Path
    query: str
    query_type: str
    effective_bm25_weight: float
    query_time_ms: float
    total_chunks_searched: int
    timestamp: datetime | None
    result_count: int

    @property
    def id(self) -> str:
        """Human-friendly id: ``YYYY-MM-DD-HHMMSS-<hash>`` when on YYYY/MM tree,
        else falls back to the file stem. Stable enough to pass to ``qa show``.
        """
        parts = self.path.parts
        if len(parts) >= 3 and parts[-3].isdigit() and parts[-2].isdigit():
            return f"{parts[-3]}-{parts[-2]}-{self.path.stem}"
        return self.path.stem

    @property
    def hash(self) -> str:
        """The ``<hash8>`` suffix of the filename, or '' when absent."""
        stem = self.path.stem
        if "-" in stem:
            return stem.rsplit("-", 1)[-1]
        return ""


@dataclass(frozen=True)
class GrepHit:
    """Single match within a qa log — file + matched line + 1-based line number."""

    index: QAIndex
    line: str
    lineno: int


def qa_dir(project_root: Path) -> Path:
    """Expected qa directory for a project root."""
    return project_root / QA_DIRNAME


def iter_qa_files(project_root: Path) -> Iterator[Path]:
    """Yield all ``*.md`` files under the project's qa tree, newest mtime first.

    Silent on a missing directory — Memory Layer is opt-in.
    """
    root = qa_dir(project_root)
    if not root.is_dir():
        return
    files = [p for p in root.rglob("*.md") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    yield from files


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Either may be empty if malformed."""
    if not text.startswith(FRONTMATTER_DELIM + "\n"):
        return "", text
    end = text.find("\n" + FRONTMATTER_DELIM + "\n", len(FRONTMATTER_DELIM) + 1)
    if end < 0:
        return "", text
    block = text[len(FRONTMATTER_DELIM) + 1 : end]
    body = text[end + len(FRONTMATTER_DELIM) + 2 :]
    return block, body


_ESCAPE_PLACEHOLDER = "\x00__QA_BS__\x00"


def _yaml_unquote(raw: str) -> str:
    """Inverse of ``qa_log._yaml_escape``.

    The writer emits double-quoted scalars only — escapes are ``\\\\`` and
    ``\\"``. Decode with a placeholder to avoid the ``\\\\"`` ambiguity.
    """
    val = raw.strip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        val = val[1:-1]
    return (
        val.replace("\\\\", _ESCAPE_PLACEHOLDER)
        .replace('\\"', '"')
        .replace(_ESCAPE_PLACEHOLDER, "\\")
    )


def _parse_frontmatter(block: str) -> dict[str, str]:
    """Parse the writer's fixed ``key: value`` frontmatter into a flat dict."""
    out: dict[str, str] = {}
    for line in block.split("\n"):
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = _yaml_unquote(val)
    return out


def _parse_timestamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _safe_float(raw: str, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _safe_int(raw: str, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def parse_qa_index(path: Path) -> QAIndex | None:
    """Return a QAIndex for ``path``, or None if frontmatter is missing/invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    block, _ = _split_frontmatter(text)
    if not block:
        return None
    fm = _parse_frontmatter(block)
    if "query" not in fm:
        return None
    return QAIndex(
        path=path,
        query=fm.get("query", ""),
        query_type=fm.get("query_type", "UNKNOWN"),
        effective_bm25_weight=_safe_float(fm.get("effective_bm25_weight", "")),
        query_time_ms=_safe_float(fm.get("query_time_ms", "")),
        total_chunks_searched=_safe_int(fm.get("total_chunks_searched", "")),
        timestamp=_parse_timestamp(fm.get("timestamp", "")),
        result_count=_safe_int(fm.get("result_count", "")),
    )


def iter_qa_indexes(project_root: Path) -> Iterator[QAIndex]:
    """Yield parsed QAIndexes. Files that fail to parse are skipped silently."""
    for path in iter_qa_files(project_root):
        idx = parse_qa_index(path)
        if idx is not None:
            yield idx


def read_qa_body(path: Path) -> str:
    """Return the markdown body of a qa log file (without the frontmatter block)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, body = _split_frontmatter(text)
    return body.lstrip("\n")


def find_qa_by_id(project_root: Path, token: str) -> QAIndex | None:
    """Resolve an id token to a QAIndex.

    Accepts any of:
    - full friendly id: ``2026-04-21-070240-fa332835``
    - file stem: ``21-070240-fa332835``
    - hash prefix (≥4 chars): ``fa33``, ``fa332835``

    Returns the first match in newest-first order. ``None`` if nothing matches.
    """
    if not token:
        return None
    token = token.strip()
    hash_only = len(token) >= 4 and re.fullmatch(r"[0-9a-f]+", token) is not None
    for idx in iter_qa_indexes(project_root):
        if idx.id == token or idx.path.stem == token:
            return idx
        if hash_only and idx.hash.startswith(token):
            return idx
    return None


def grep_qa(
    project_root: Path,
    term: str,
    *,
    case_insensitive: bool = True,
) -> Iterator[GrepHit]:
    """Yield GrepHit for every line containing ``term`` across all qa logs.

    Searches both frontmatter and body. Results come out in newest-first order
    of files, then top-to-bottom within each file.
    """
    if not term:
        return
    needle = term.casefold() if case_insensitive else term
    for idx in iter_qa_indexes(project_root):
        try:
            text = idx.path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.split("\n"), start=1):
            haystack = line.casefold() if case_insensitive else line
            if needle in haystack:
                yield GrepHit(index=idx, line=line.rstrip(), lineno=lineno)
