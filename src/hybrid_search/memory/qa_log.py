"""Q&A log — persist MCP search responses as markdown for cross-session recall.

MVP scope:
- Records each hybrid_search response as a markdown file (YAML frontmatter + body).
- Stored per-project under ``<project_root>/.hybrid-search/qa/YYYY/MM/<stamp>-<hash>.md``.
- Opt-in via env var ``HYBRID_SEARCH_QA_LOG=1`` (default off).
- Non-blocking: writes happen on a daemon thread; callers never wait or raise.

Design notes (MVP, not production):
- **Per-project storage**: matches existing ``.hybrid-search/wiki/`` convention,
  avoids cross-project leakage by default. Auto-scoped to the project whose path
  contains ``cwd``; falls back to ``cwd`` itself when no registered project matches.
- **YYYY/MM subdirs**: a single flat dir can balloon to 1000+ entries/month on
  active projects. Year/month keeps ``ls`` usable and matches how humans browse.
- **Timestamp + 8-char query hash**: sortable chronologically, collision-safe,
  no transliteration needed for Korean queries (slug would be lossy).
- **Frontmatter**: structured fields (query, query_type, weights, result ids)
  enable cheap grep-based lookup before we build a proper index.

Future work (explicitly out of scope for MVP):
- Semantic search over qa logs (re-embed the query+snippets).
- Cross-project qa search (aggregate across ``~/.hybrid-search/`` + per-project dirs).
- Link to Claude Code conversation index.
- Auto-cleanup / rotation policy (e.g. prune >90d, cap per month).
- Sensitive-query filter (drop queries matching secret patterns).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


ENV_TOGGLE = "HYBRID_SEARCH_QA_LOG"
"""Env var name. Opt-OUT via ``HYBRID_SEARCH_QA_LOG=0/false/no/off``.

Memory Layer is on by default so the compounding-quality loop works out of
the box: every answered query becomes searchable context for the next
query. Privacy-sensitive users and CI environments opt out explicitly.
"""

# Cap snippets in frontmatter to keep files small and human-readable.
_SNIPPET_MAX_CHARS = 240
# Cap number of results persisted per log entry (top-k).
_MAX_RESULTS = 10

# Queries matching these fragments never hit disk. Covers the most common
# shapes of "please look up the password/token for X" so that a secret
# pasted into a query doesn't get immortalized as a markdown file.
import re as _re
_SENSITIVE_QUERY_RE = _re.compile(
    r"(?i)("
    r"password|passwd|passphrase|"
    r"secret[_\-\s]?key|api[_\-\s]?key|access[_\-\s]?key|"
    r"bearer|private[_\-\s]?key|"
    r"credential|token|"
    r"authorization:|"
    r"AKIA[0-9A-Z]{16}|"      # AWS access key id shape
    r"sk-[A-Za-z0-9]{20,}|"   # openai-style keys
    r"ghp_[A-Za-z0-9]{30,}|"  # github PAT
    r"xoxb-|xoxp-"            # slack
    r")"
)


@dataclass(frozen=True)
class QARecord:
    """Immutable snapshot of a single search exchange.

    v2 (2026-04-23): optional trigger / tools_used / answer_chars fields
    carry non-MCP turn context when the record is produced by the Stop
    hook instead of the hybrid_search tool handler. Legacy writers that
    omit them remain valid — all three default to None / empty.
    """
    query: str
    query_type: str
    effective_bm25_weight: float
    query_time_ms: float
    total_chunks_searched: int
    results: list[dict[str, Any]]
    timestamp: datetime
    project_root: Path
    # v2
    trigger: str | None = None              # "mcp_tool" | "stop_hook" | "user_prompt_submit"
    tools_used: tuple[str, ...] = ()        # names of tools Claude invoked this turn
    answer_chars: int | None = None         # length of Claude's final text response
    client: str | None = None               # "claude" | "codex" | None for legacy records


def is_enabled() -> bool:
    """Return True unless the user explicitly opted out.

    Default-on keeps the Memory Layer's compounding-quality loop alive for
    new users without configuration. The opt-out values below are the
    same ones accepted by every CLI flag in this codebase.
    """
    val = os.environ.get(ENV_TOGGLE, "").strip().lower()
    if val in {"0", "false", "no", "off"}:
        return False
    return True


def is_sensitive_query(query: str) -> bool:
    """True when the query looks like it carries a secret.

    False positives here are harmless (the query just isn't logged); false
    negatives leak a secret into a markdown file that may land in git.
    Bias the regex toward over-matching — password-adjacent language is
    not worth memorializing anyway.
    """
    return bool(query and _SENSITIVE_QUERY_RE.search(query))


def _hash_query(query: str) -> str:
    """Short, stable identifier for a query. 8 hex chars → 4B collision space."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]


def _truncate(text: str | None, limit: int = _SNIPPET_MAX_CHARS) -> str:
    if not text:
        return ""
    flat = text.replace("\n", " ").replace("\r", " ").strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def _yaml_escape(value: str) -> str:
    """Minimal YAML escaping for double-quoted scalars."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _resolve_project_root(
    cwd: str | None,
    project_infos: Iterable[Any] | None = None,
) -> Path | None:
    """Pick the directory that should own this log entry.

    Priority: registered project whose path contains cwd → cwd itself → None.
    ``project_infos`` is an iterable of objects with a ``.path`` attribute
    (matches ``ProjectInfo``) — kept loosely typed so we don't import it here.
    """
    if cwd:
        try:
            cwd_path = Path(cwd).resolve()
        except (OSError, ValueError):
            return None

        if project_infos:
            for pinfo in project_infos:
                try:
                    project_path = Path(pinfo.path).resolve()
                except (OSError, ValueError, AttributeError):
                    continue
                try:
                    cwd_path.relative_to(project_path)
                    return project_path
                except ValueError:
                    pass

        return cwd_path
    return None


def _format_record(record: QARecord) -> str:
    """Render record as YAML frontmatter + markdown body."""
    ts_iso = record.timestamp.isoformat(timespec="seconds")
    lines = [
        "---",
        f'query: "{_yaml_escape(record.query)}"',
        f"query_type: {record.query_type}",
        f"effective_bm25_weight: {record.effective_bm25_weight}",
        f"query_time_ms: {record.query_time_ms}",
        f"total_chunks_searched: {record.total_chunks_searched}",
        f"timestamp: {ts_iso}",
        f"result_count: {len(record.results)}",
    ]
    if record.trigger:
        lines.append(f"trigger: {record.trigger}")
    if record.tools_used:
        tools_str = ", ".join(f'"{_yaml_escape(t)}"' for t in record.tools_used)
        lines.append(f"tools_used: [{tools_str}]")
    if record.answer_chars is not None:
        lines.append(f"answer_chars: {record.answer_chars}")
    if record.client:
        lines.append(f"client: {record.client}")
    lines += [
        "---",
        "",
        f"# Q: {record.query}",
        "",
        f"- **query_type**: {record.query_type}",
        f"- **bm25_weight**: {record.effective_bm25_weight}",
        f"- **time**: {record.query_time_ms} ms",
        f"- **chunks_searched**: {record.total_chunks_searched}",
    ]
    if record.trigger:
        lines.append(f"- **trigger**: {record.trigger}")
    if record.tools_used:
        lines.append(f"- **tools**: {', '.join(record.tools_used)}")
    if record.answer_chars is not None:
        lines.append(f"- **answer_chars**: {record.answer_chars}")
    if record.client:
        lines.append(f"- **client**: {record.client}")
    lines += [
        "",
        "## Top results",
        "",
    ]
    if not record.results:
        lines.append("_(no results)_")
    for idx, r in enumerate(record.results, start=1):
        chunk_id = r.get("chunk_id", "?")
        path = r.get("file_path", "?")
        start = r.get("start_line")
        end = r.get("end_line")
        name = r.get("name") or r.get("qualified_name") or ""
        loc = f":{start}-{end}" if start and end else ""
        snippet = _truncate(r.get("snippet") or r.get("content"))
        header = f"### {idx}. `{path}{loc}`"
        if name:
            header += f" — {name}"
        lines.append(header)
        lines.append(f"- chunk_id: `{chunk_id}`")
        if snippet:
            lines.append("")
            lines.append(f"> {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_atomic(target: Path, content: str) -> None:
    """Write then rename — avoids partial reads if another process tails the dir."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)


def _build_path(root: Path, ts: datetime, query_hash: str) -> Path:
    """``<root>/.hybrid-search/qa/YYYY/MM/<HHMMSS>-<hash>.md``."""
    return (
        root
        / ".hybrid-search"
        / "qa"
        / f"{ts.year:04d}"
        / f"{ts.month:02d}"
        / f"{ts.strftime('%d-%H%M%S')}-{query_hash}.md"
    )


def _persist(record: QARecord) -> Path | None:
    """Serialize and write the record. Returns the file path or None on failure."""
    try:
        query_hash = _hash_query(record.query)
        path = _build_path(record.project_root, record.timestamp, query_hash)
        content = _format_record(record)
        _write_atomic(path, content)
        return path
    except Exception as exc:  # pragma: no cover — logged, never raised
        logger.debug("qa_log write failed: %s", exc)
        return None


def record(
    *,
    query: str,
    response: Any,
    cwd: str | None,
    project_infos: Iterable[Any] | None = None,
    async_write: bool = True,
    trigger: str = "mcp_tool",
) -> Path | None:
    """Record a hybrid_search response to disk (fire-and-forget).

    Contract:
    - Returns immediately when the toggle is off.
    - Never raises; failures are logged at DEBUG.
    - When ``async_write`` is True (default), disk I/O happens on a daemon
      thread so the hot search path is untouched.
    - When ``async_write`` is False (used by tests), returns the file path.
    - ``trigger`` is persisted in the frontmatter (``mcp_tool`` /
      ``user_prompt_submit``) so downstream analytics can tell which
      code path produced the record.
    """
    if not is_enabled():
        return None
    if is_sensitive_query(query):
        # Never persist secrets to disk. The search still runs and returns
        # normally to the caller; we just don't immortalize the query.
        return None

    try:
        root = _resolve_project_root(cwd, project_infos)
        if root is None:
            return None

        # Flatten the response to plain dicts so the writer thread doesn't
        # keep references to live search objects.
        raw_results = getattr(response, "results", []) or []
        results_payload: list[dict[str, Any]] = []
        for r in raw_results[:_MAX_RESULTS]:
            results_payload.append({
                "chunk_id": getattr(r, "chunk_id", None),
                "file_path": getattr(r, "file_path", None),
                "project": getattr(r, "project", None),
                "name": getattr(r, "name", None),
                "qualified_name": getattr(r, "qualified_name", None),
                "node_type": getattr(r, "node_type", None),
                "start_line": getattr(r, "start_line", None),
                "end_line": getattr(r, "end_line", None),
                "snippet": getattr(r, "snippet", None),
            })

        rec = QARecord(
            query=query,
            query_type=getattr(response, "query_type", "UNKNOWN"),
            effective_bm25_weight=float(getattr(response, "effective_bm25_weight", 0.0)),
            query_time_ms=float(getattr(response, "query_time_ms", 0.0)),
            total_chunks_searched=int(getattr(response, "total_chunks_searched", 0)),
            results=results_payload,
            timestamp=datetime.now(timezone.utc),
            project_root=root,
            trigger=trigger,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("qa_log prepare failed: %s", exc)
        return None

    if async_write:
        threading.Thread(target=_persist, args=(rec,), daemon=True).start()
        return None
    return _persist(rec)


# Dedup window for Stop-hook writes. When the hybrid_search MCP tool already
# saved a record ≤ this many seconds ago with the same query hash, Stop hook
# skips to avoid double-persistence of the same turn.
_DEDUP_WINDOW_SECONDS = 5


def _recent_qa_hash_exists(
    project_root: Path,
    query_hash: str,
    within_seconds: int = _DEDUP_WINDOW_SECONDS,
) -> bool:
    """True when a qa file with the given hash was written in the last N seconds.

    Cheap directory scan — qa dirs are small (at most thousands of entries
    even on very active projects; auto-prune caps at 2000). Skips the scan
    silently if the qa dir doesn't exist.
    """
    qa_root = project_root / ".hybrid-search" / "qa"
    if not qa_root.is_dir():
        return False
    try:
        now_ts = datetime.now(timezone.utc).timestamp()
    except Exception:
        return False
    threshold = now_ts - within_seconds
    for year in qa_root.iterdir():
        if not year.is_dir() or not year.name.isdigit():
            continue
        for month in year.iterdir():
            if not month.is_dir() or not month.name.isdigit():
                continue
            for md in month.glob(f"*-{query_hash}.md"):
                try:
                    if md.stat().st_mtime >= threshold:
                        return True
                except OSError:
                    continue
    return False


def record_turn(
    *,
    query: str,
    cwd: str | None,
    tools_used: Iterable[str] = (),
    answer_chars: int | None = None,
    trigger: str = "stop_hook",
    project_infos: Iterable[Any] | None = None,
    async_write: bool = False,
    dedup: bool = True,
    client: str | None = None,
) -> Path | None:
    """Persist a conversation turn that did NOT go through the MCP tool.

    Called by the Claude Code Stop hook so every turn — including those
    where Claude only used Grep/Read/Bash — ends up in qa_log. Uses a
    short dedup window against the MCP tool's save path so turns that DID
    also fire the MCP don't end up double-saved.

    Never raises.
    """
    if not is_enabled():
        return None
    if is_sensitive_query(query):
        return None
    try:
        root = _resolve_project_root(cwd, project_infos)
        if root is None:
            return None

        if dedup and _recent_qa_hash_exists(root, _hash_query(query)):
            return None

        rec = QARecord(
            query=query,
            query_type="TURN",
            effective_bm25_weight=0.0,
            query_time_ms=0.0,
            total_chunks_searched=0,
            results=[],
            timestamp=datetime.now(timezone.utc),
            project_root=root,
            trigger=trigger,
            tools_used=tuple(tools_used),
            answer_chars=answer_chars,
            client=client,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("qa_log record_turn prepare failed: %s", exc)
        return None

    if async_write:
        threading.Thread(target=_persist, args=(rec,), daemon=True).start()
        return None
    return _persist(rec)
