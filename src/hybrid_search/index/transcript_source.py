"""A1+A2 PoC — discover agent transcripts and turn them into conversation chunks.

This module is the *read + chunk* half of the conversation indexer. It is
deliberately storage-free: it reads Claude Code and Codex JSONL transcripts
for a project and returns ``ConvChunk`` objects so we can eyeball whether the
"decision episode" boundary holds in real data before wiring persistence and
retrieval (Phase A3+).

Two sources, one chunk shape:

- Claude Code: ``~/.claude/projects/<slug>/*.jsonl``
  records ``{type: user|assistant, message: {role, content}}`` where content
  is a string or a list of ``text``/``thinking``/``tool_use``/``tool_result``
  blocks.
- Codex: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
  records ``{type: response_item, payload: {...}}`` where payload is a
  ``message`` (role user/assistant), ``function_call``/``function_call_output``
  (tool calls), or ``reasoning`` (encrypted — skipped).

The shared insight (2026-04-16 design): a tool call is the natural bridge from
"why" (conversation) to "where" (code), so every chunk carries the tools fired
and the files/commands they touched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# A path-like token: a/b/c.ext or bare name.ext (no whitespace). Used to pull
# real file references out of shell commands and tool targets.
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,6}")

Source = Literal["claude", "codex"]

USER_PROMPT_MAX = 1_500
ASSISTANT_EXCERPT_MAX = 1_800
MAX_TOOLS_PER_CHUNK = 40


@dataclass(frozen=True)
class ToolEvent:
    """A single tool invocation inside a turn — the conv↔code bridge."""

    tool: str
    target: str  # file path, command, or pattern — best-effort


@dataclass(frozen=True)
class ConvChunk:
    """One decision episode: a user turn plus the agent activity it triggered."""

    source: Source
    project_path: str
    session_id: str
    turn_index: int
    timestamp: str
    user_prompt: str
    assistant_excerpt: str
    tools: tuple[ToolEvent, ...] = field(default_factory=tuple)
    files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        """Composed body that A3/A4 would embed + BM25-index."""
        lines = [f"[{self.source} turn] {self.user_prompt}"]
        if self.assistant_excerpt:
            lines.append(self.assistant_excerpt)
        if self.tools:
            tool_str = ", ".join(f"{t.tool}({t.target})" if t.target else t.tool for t in self.tools)
            lines.append(f"tools: {tool_str}")
        if self.files:
            lines.append(f"files: {', '.join(self.files)}")
        return "\n".join(lines)

    @property
    def char_len(self) -> int:
        return len(self.text)


# ── Discovery (A1) ────────────────────────────────────────────────────


def claude_slug_for(project_path: Path) -> str:
    """Claude Code encodes the project path as the dir name.

    It replaces ``/``, ``_`` and ``.`` with ``-`` (e.g. ``claude_project`` →
    ``claude-project``), so a naive ``/``-only swap misses the directory.
    """
    resolved = str(project_path.resolve())
    return re.sub(r"[/._]", "-", resolved)


def discover_claude_transcripts(
    project_path: Path,
    claude_root: Path | None = None,
) -> list[Path]:
    """Locate Claude Code transcripts for ``project_path`` by slug."""
    root = claude_root or (Path.home() / ".claude" / "projects")
    slug_dir = root / claude_slug_for(project_path)
    if not slug_dir.is_dir():
        return []
    return sorted(slug_dir.glob("*.jsonl"))


def discover_codex_sessions(
    project_path: Path,
    codex_root: Path | None = None,
) -> list[Path]:
    """Locate Codex sessions whose ``session_meta.cwd`` matches the project.

    Codex stores every session under a date tree regardless of project, so we
    read just the first line (session_meta) of each file to filter by cwd.
    """
    root = codex_root or (Path.home() / ".codex" / "sessions")
    if not root.is_dir():
        return []
    target = str(project_path.resolve())
    matches: list[Path] = []
    for path in root.rglob("*.jsonl"):
        cwd = _codex_session_cwd(path)
        if cwd == target:
            matches.append(path)
    return sorted(matches)


def discover_recent_transcripts(
    project_path: Path,
    *,
    max_files: int = 4,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
    codex_scan_cap: int = 40,
    codex_day_dirs: int = 3,
) -> list[tuple[Source, Path]]:
    """Bounded, mtime-first transcript discovery for the query-time overlay.

    ``collect_project_chunks`` reads the first line of *every* Codex session to
    filter by cwd — fine for a background reindex, far too slow for a per-query
    overlay (a busy machine has thousands of sessions). This variant restricts
    the Codex scan to the most recent ``codex_day_dirs`` ``YYYY/MM/DD`` folders
    (so it never stats the whole tree), sorts those by mtime, and only sniffs
    the cwd of the freshest ``codex_scan_cap`` files. Returns at most
    ``max_files`` ``(source, path)`` pairs, newest first across both agents —
    enough to cover the live session whose tail turns lag the index.
    """
    target = str(project_path.resolve())
    candidates: list[tuple[float, Source, Path]] = []

    for path in discover_claude_transcripts(project_path, claude_root):
        mtime = _safe_mtime(path)
        if mtime is not None:
            candidates.append((mtime, "claude", path))

    codex_dir = codex_root or (Path.home() / ".codex" / "sessions")
    if codex_dir.is_dir():
        codex_paths = [
            (mt, p)
            for p in _recent_codex_files(codex_dir, codex_day_dirs)
            if (mt := _safe_mtime(p)) is not None
        ]
        codex_paths.sort(key=lambda pair: -pair[0])
        for mtime, path in codex_paths[:codex_scan_cap]:
            if _codex_session_cwd(path) == target:
                candidates.append((mtime, "codex", path))

    candidates.sort(key=lambda c: -c[0])
    return [(source, path) for _mt, source, path in candidates[:max_files]]


def _recent_codex_files(codex_dir: Path, day_dirs: int) -> list[Path]:
    """Codex JSONL files from the newest ``YYYY/MM/DD`` folders only.

    The date-named tree sorts chronologically by path, so the most recent days
    are found by name without stat-ing every file. Falls back to a full
    ``rglob`` when the layout isn't the expected date tree.
    """
    dated = sorted(
        (d for d in codex_dir.glob("[0-9]*/[0-9]*/[0-9]*") if d.is_dir()),
        key=lambda d: d.as_posix(),
        reverse=True,
    )[:day_dirs]
    if not dated:
        return list(codex_dir.rglob("*.jsonl"))
    files: list[Path] = []
    for day in dated:
        files.extend(day.glob("*.jsonl"))
    return files


def _safe_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _codex_session_cwd(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return None
    if not first:
        return None
    try:
        record = json.loads(first)
    except (ValueError, TypeError):
        return None
    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload") or {}
    return payload.get("cwd")


# ── Shared parsing helpers ────────────────────────────────────────────


def _load_jsonl(path: Path) -> list[dict]:
    """Decode a JSONL file, skipping unparseable lines. Never raises."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _dedup_files(events: list[ToolEvent]) -> tuple[str, ...]:
    """Extract real file paths from tool targets, order-preserving, deduped.

    A tool target may be a clean file path (Claude ``Edit``) or a whole shell
    command (Codex ``exec_command``). We scan each target for path-like tokens
    (``foo/bar.py``) so a command like ``sed -n '1,220p' README.md`` yields
    ``README.md`` rather than the command string.
    """
    seen: list[str] = []
    for ev in events:
        for token in _PATH_TOKEN_RE.findall(ev.target or ""):
            if token not in seen:
                seen.append(token)
    return tuple(seen)


# ── Claude Code parsing (A2) ──────────────────────────────────────────


def _claude_user_text(record: dict) -> str | None:
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        s = content.strip()
        if not s or s.startswith(("<local-command-", "<command-", "<system-reminder>")):
            return None
        return s
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                txt = (block.get("text") or "").strip()
                if txt and not txt.startswith("<system-reminder>"):
                    return txt
    return None


def _claude_assistant_blocks(record: dict) -> tuple[str, list[ToolEvent]]:
    msg = record.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return "", []
    texts: list[str] = []
    tools: list[ToolEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            txt = (block.get("text") or "").strip()
            if txt:
                texts.append(txt)
        elif btype == "tool_use":
            tools.append(_claude_tool_event(block))
    return "\n".join(texts), tools


def _claude_tool_event(block: dict) -> ToolEvent:
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    target = (
        inp.get("file_path")
        or inp.get("path")
        or inp.get("command")
        or inp.get("pattern")
        or inp.get("query")
        or ""
    )
    return ToolEvent(tool=name, target=_clip(str(target), 120))


def parse_claude_transcript(path: Path) -> list[ConvChunk]:
    """Group a Claude transcript into per-user-turn decision episodes."""
    records = _load_jsonl(path)
    session_id = path.stem
    project_path = ""
    chunks: list[ConvChunk] = []

    idx = 0
    turn = 0
    n = len(records)
    while idx < n:
        record = records[idx]
        if not project_path and record.get("cwd"):
            project_path = record["cwd"]
        if record.get("type") != "user":
            idx += 1
            continue
        prompt = _claude_user_text(record)
        if prompt is None:
            idx += 1
            continue
        timestamp = record.get("timestamp", "")
        # Walk forward over assistant activity until the next genuine user turn.
        texts: list[str] = []
        tools: list[ToolEvent] = []
        j = idx + 1
        while j < n:
            nxt = records[j]
            if nxt.get("type") == "user" and _claude_user_text(nxt) is not None:
                break
            if nxt.get("type") == "assistant":
                txt, evs = _claude_assistant_blocks(nxt)
                if txt:
                    texts.append(txt)
                tools.extend(evs)
            j += 1
        chunks.append(
            _build_chunk(
                source="claude",
                project_path=project_path,
                session_id=session_id,
                turn=turn,
                timestamp=timestamp,
                prompt=prompt,
                assistant_text="\n".join(texts),
                tools=tools,
            )
        )
        turn += 1
        idx = j
    return chunks


# ── Codex parsing (A2) ────────────────────────────────────────────────


def _codex_message_text(payload: dict) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text")
    ]
    joined = "\n".join(p for p in parts if p).strip()
    return joined or None


def _codex_tool_event(payload: dict) -> ToolEvent:
    name = payload.get("name") or "tool"
    target = ""
    raw_args = payload.get("arguments")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
            target = args.get("cmd") or args.get("command") or args.get("path") or ""
        except (ValueError, TypeError):
            target = raw_args
    return ToolEvent(tool=name, target=_clip(str(target), 120))


def parse_codex_session(path: Path) -> list[ConvChunk]:
    """Group a Codex session into per-user-turn decision episodes."""
    records = _load_jsonl(path)
    session_id = path.stem
    project_path = ""
    chunks: list[ConvChunk] = []

    # Flatten to (kind, text/tool) events in order.
    events: list[tuple[str, object, str]] = []  # (kind, value, timestamp)
    for record in records:
        if record.get("type") == "session_meta":
            project_path = (record.get("payload") or {}).get("cwd", project_path)
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload") or {}
        ts = record.get("timestamp", "")
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue  # skip developer/system scaffolding
            text = _codex_message_text(payload)
            if text:
                events.append((f"msg:{role}", text, ts))
        elif ptype in ("function_call", "custom_tool_call"):
            events.append(("tool", _codex_tool_event(payload), ts))

    # Build episodes: a user message opens a turn; assistant text + tools attach.
    turn = 0
    i = 0
    m = len(events)
    while i < m:
        kind, value, ts = events[i]
        if kind != "msg:user":
            i += 1
            continue
        prompt = str(value)
        texts: list[str] = []
        tools: list[ToolEvent] = []
        k = i + 1
        while k < m and events[k][0] != "msg:user":
            ekind, evalue, _ = events[k]
            if ekind == "msg:assistant":
                texts.append(str(evalue))
            elif ekind == "tool" and isinstance(evalue, ToolEvent):
                tools.append(evalue)
            k += 1
        chunks.append(
            _build_chunk(
                source="codex",
                project_path=project_path,
                session_id=session_id,
                turn=turn,
                timestamp=ts,
                prompt=prompt,
                assistant_text="\n".join(texts),
                tools=tools,
            )
        )
        turn += 1
        i = k
    return chunks


# ── Chunk assembly ────────────────────────────────────────────────────


def _build_chunk(
    *,
    source: Source,
    project_path: str,
    session_id: str,
    turn: int,
    timestamp: str,
    prompt: str,
    assistant_text: str,
    tools: list[ToolEvent],
) -> ConvChunk:
    capped_tools = tuple(tools[:MAX_TOOLS_PER_CHUNK])
    return ConvChunk(
        source=source,
        project_path=project_path,
        session_id=session_id,
        turn_index=turn,
        timestamp=timestamp,
        user_prompt=_clip(prompt, USER_PROMPT_MAX),
        assistant_excerpt=_clip(assistant_text, ASSISTANT_EXCERPT_MAX),
        tools=capped_tools,
        files=_dedup_files(list(capped_tools)),
    )


def collect_project_chunks(
    project_path: Path,
    *,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
) -> list[ConvChunk]:
    """Discover + parse all transcripts for a project across both agents."""
    chunks: list[ConvChunk] = []
    for path in discover_claude_transcripts(project_path, claude_root):
        chunks.extend(parse_claude_transcript(path))
    for path in discover_codex_sessions(project_path, codex_root):
        chunks.extend(parse_codex_session(path))
    return chunks
