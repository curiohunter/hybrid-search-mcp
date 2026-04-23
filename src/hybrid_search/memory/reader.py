"""Q&A log reader — list/show/grep/prune over persisted hybrid_search responses.

Companion to ``qa_log.py`` (writer). Parses the YAML frontmatter + markdown
body format produced by ``qa_log._format_record`` without pulling in a YAML
dependency — the format is fixed and simple.

Layout on disk:
    <project_root>/.hybrid-search/qa/YYYY/MM/DD-HHMMSS-<hash8>.md

Reader surface:
    iter_qa_files(root)            — yields Path, newest mtime first
    parse_qa_index(path)           — returns QAIndex (metadata only) or None
    iter_qa_indexes(root)          — combines the two, swallowing parse errors
    read_qa_body(path)             — returns markdown body (without frontmatter)
    find_qa_by_id(root, tok)       — resolves stem / hash-prefix / YYYY-MM-DD-… id
    grep_qa(root, term)            — yields GrepHit over frontmatter + body
    parse_duration(spec)           — "30d"/"12h"/"2w"/"3m" → timedelta
    select_older_than(root, cutoff)— files with mtime < cutoff
    prune_older_than(root, cutoff) — delete + rmdir empty YYYY/MM. dry_run支持
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


QA_DIRNAME = ".hybrid-search/qa"
FRONTMATTER_DELIM = "---"


@dataclass(frozen=True)
class QAIndex:
    """Metadata-only view of a qa log entry. Body is loaded on demand.

    v2 fields (``trigger``, ``tools_used``, ``answer_chars``) are optional
    and default empty when reading legacy MCP-only frontmatter produced by
    v0.2.x.
    """

    path: Path
    query: str
    query_type: str
    effective_bm25_weight: float
    query_time_ms: float
    total_chunks_searched: int
    timestamp: datetime | None
    result_count: int
    trigger: str | None = None
    tools_used: tuple[str, ...] = ()
    answer_chars: int | None = None

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


def _parse_tools_used(raw: str) -> tuple[str, ...]:
    """Decode the v2 ``tools_used`` frontmatter value.

    Writer emits a JSON-ish list of double-quoted scalars,
    e.g. ``[\"Grep\", \"Read\"]``. Cheap hand-roll parse to avoid a YAML dep.
    """
    if not raw:
        return ()
    val = raw.strip()
    if val.startswith("[") and val.endswith("]"):
        val = val[1:-1]
    out: list[str] = []
    for tok in val.split(","):
        tok = tok.strip()
        if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
            tok = tok[1:-1]
        if tok:
            out.append(_yaml_unquote(f'"{tok}"'))
    return tuple(out)


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
    answer_chars_raw = fm.get("answer_chars", "")
    return QAIndex(
        path=path,
        query=fm.get("query", ""),
        query_type=fm.get("query_type", "UNKNOWN"),
        effective_bm25_weight=_safe_float(fm.get("effective_bm25_weight", "")),
        query_time_ms=_safe_float(fm.get("query_time_ms", "")),
        total_chunks_searched=_safe_int(fm.get("total_chunks_searched", "")),
        timestamp=_parse_timestamp(fm.get("timestamp", "")),
        result_count=_safe_int(fm.get("result_count", "")),
        trigger=fm.get("trigger") or None,
        tools_used=_parse_tools_used(fm.get("tools_used", "")),
        answer_chars=_safe_int(answer_chars_raw) if answer_chars_raw else None,
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


# ── retention (Sprint 4) ──────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([dhwm])\s*$", re.IGNORECASE)
_MONTH_DAYS = 30  # rough — retention policies don't need calendar accuracy


def parse_duration(spec: str) -> timedelta:
    """Parse a retention duration: ``30d`` / ``12h`` / ``2w`` / ``3m``.

    Months are treated as 30 days — good enough for "prune stuff older than
    three months" intent; callers wanting calendar accuracy should pass an
    explicit date instead.

    Raises ``ValueError`` on malformed input.
    """
    m = _DURATION_RE.match(spec)
    if not m:
        raise ValueError(
            f"invalid duration {spec!r}; expected <N>[d|h|w|m], e.g. '30d'"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "w":
        return timedelta(weeks=n)
    return timedelta(days=n * _MONTH_DAYS)


def resolve_cutoff(
    *,
    older_than: str | None = None,
    before: str | None = None,
    now: datetime | None = None,
) -> datetime:
    """Resolve CLI-ish ``--older-than`` / ``--before`` args to a UTC cutoff.

    Files with mtime strictly less than the cutoff are considered expired.
    """
    if (older_than is None) == (before is None):
        raise ValueError("pass exactly one of older_than= / before=")
    ref = now if now is not None else datetime.now(timezone.utc)
    if older_than is not None:
        return ref - parse_duration(older_than)
    assert before is not None
    try:
        cutoff = datetime.fromisoformat(before)
    except ValueError as exc:
        raise ValueError(f"invalid date {before!r}: {exc}") from exc
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff


def select_older_than(project_root: Path, cutoff: datetime) -> list[Path]:
    """Return qa files with ``mtime < cutoff``, newest-expired first."""
    cutoff_ts = cutoff.timestamp()
    expired: list[tuple[float, Path]] = []
    for path in iter_qa_files(project_root):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            expired.append((mtime, path))
    expired.sort(key=lambda pair: pair[0], reverse=True)
    return [p for _, p in expired]


@dataclass(frozen=True)
class PruneResult:
    deleted: list[Path]
    skipped: list[Path]  # files that failed to delete — reported, not raised
    dirs_removed: list[Path]


def _rmdir_empty_ancestors(path: Path, stop: Path) -> list[Path]:
    """Remove ``path`` and empty parent dirs up to (but not including) ``stop``."""
    removed: list[Path] = []
    cur = path
    stop = stop.resolve()
    while True:
        try:
            cur_resolved = cur.resolve()
        except OSError:
            break
        if cur_resolved == stop:
            break
        try:
            cur.rmdir()
        except OSError:
            break  # non-empty or gone already
        removed.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return removed


def prune_older_than(
    project_root: Path,
    cutoff: datetime,
    *,
    dry_run: bool = False,
) -> PruneResult:
    """Delete qa files older than ``cutoff``. When ``dry_run`` is True the
    result lists the same files without touching disk.

    Empty ``YYYY/MM`` dirs are rmdir'd after a successful real prune — the
    top-level ``.hybrid-search/qa/`` root is preserved as an anchor.
    """
    expired = select_older_than(project_root, cutoff)
    if dry_run:
        return PruneResult(deleted=list(expired), skipped=[], dirs_removed=[])

    return _delete_files(project_root, expired)


def select_over_count(project_root: Path, keep_n: int) -> list[Path]:
    """Return qa files beyond the ``keep_n`` newest by mtime (oldest-first).

    Pair with ``prune_keep_latest`` for count-ceiling eviction — keeps the
    fresh tail, drops the long-tail. Returns an empty list when total ≤ keep_n.
    """
    if keep_n < 0:
        keep_n = 0
    ranked: list[tuple[float, Path]] = []
    for path in iter_qa_files(project_root):  # already newest-first
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        ranked.append((mtime, path))
    if len(ranked) <= keep_n:
        return []
    # Keep the top keep_n (newest); the rest are candidates for eviction.
    # iter_qa_files already returned newest-first, so slice after keep_n.
    evict = [p for _, p in ranked[keep_n:]]
    return evict


def prune_keep_latest(
    project_root: Path,
    keep_n: int,
    *,
    dry_run: bool = False,
) -> PruneResult:
    """Keep only the ``keep_n`` newest qa files; delete the rest.

    This is the count ceiling half of the journald-style retention policy:
    pair with ``prune_older_than`` to enforce both age and count limits on
    the same pass (whichever triggers first).
    """
    victims = select_over_count(project_root, keep_n)
    if dry_run:
        return PruneResult(deleted=list(victims), skipped=[], dirs_removed=[])
    return _delete_files(project_root, victims)


def auto_prune(
    project_root: Path,
    *,
    retention_days: int | None = None,
    max_files: int | None = None,
    dry_run: bool = False,
) -> PruneResult:
    """Combined age-and-count prune. Either ceiling binds independently.

    - ``retention_days`` — delete anything older than this many days.
    - ``max_files`` — after age-prune, keep at most this many of the newest.

    Passing ``None`` for either disables that ceiling. Returns a merged
    ``PruneResult`` listing every file that would be (or was) removed.
    """
    merged_deleted: list[Path] = []
    merged_skipped: list[Path] = []
    merged_dirs: list[Path] = []

    if retention_days is not None and retention_days > 0:
        cutoff = resolve_cutoff(older_than=f"{retention_days}d")
        age_result = prune_older_than(project_root, cutoff, dry_run=dry_run)
        merged_deleted.extend(age_result.deleted)
        merged_skipped.extend(age_result.skipped)
        merged_dirs.extend(age_result.dirs_removed)

    if max_files is not None and max_files >= 0:
        count_result = prune_keep_latest(project_root, max_files, dry_run=dry_run)
        # Avoid double-counting: filter out files already removed in the age pass.
        already = set(merged_deleted)
        for p in count_result.deleted:
            if p in already:
                continue
            merged_deleted.append(p)
        merged_skipped.extend(p for p in count_result.skipped if p not in already)
        merged_dirs.extend(count_result.dirs_removed)

    return PruneResult(
        deleted=merged_deleted,
        skipped=merged_skipped,
        dirs_removed=merged_dirs,
    )


def _delete_files(project_root: Path, victims: list[Path]) -> PruneResult:
    """Shared deletion path: unlink, rmdir empty YYYY/MM, preserve qa root."""
    deleted: list[Path] = []
    skipped: list[Path] = []
    dirs_removed: list[Path] = []
    qa_root = qa_dir(project_root)
    for path in victims:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            skipped.append(path)
            continue
        dirs_removed.extend(_rmdir_empty_ancestors(path.parent, qa_root))
    return PruneResult(deleted=deleted, skipped=skipped, dirs_removed=dirs_removed)
