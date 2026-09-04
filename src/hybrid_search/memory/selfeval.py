"""Self-evaluation from real usage — the zero-touch improvement loop.

Every Stop hook already parses the finished turn's transcript. This module
rides that same pass and scores what actually happened after each
``hybrid_search`` call in the turn:

- **adopted**: a top-N result file was opened afterwards (Read/Edit/Write —
  we record the rank)
- **betrayed**: the agent opened files *outside* the results, or fell back to
  searching on its own (the Grep tool, or `rg`/`grep`/`find` through Bash) —
  the search didn't carry the turn
- **no_followup**: no file opened and no search after (the injected snippets
  were enough, or the results went unused — indistinguishable here)

Rows append to ``.hybrid-search/selfeval/events.jsonl``. Betrayals where we
know what the agent ended up reading also append to ``harvested.jsonl`` as
``{query, gold_paths}`` — real-usage failures become the regression set,
with zero manual labeling.

Two lanes are scored (v1.1), tagged by the row's ``source`` field:

- ``tool`` — explicit ``hybrid_search`` tool calls, read straight off the
  transcript (``extract_turn_events``).
- ``prefetch`` — the UserPromptSubmit pre-fetch, which injects results
  without a tool call. Its served paths can't be recovered from the
  transcript, so the pre-fetch hook drops a *pending* sidecar row at prompt
  time and the Stop hook joins against it (``extract_prefetch_event``).
  Scoring logic is shared: both lanes feed the same ``score_event``.

Rows written before v1.1 carry no ``source`` and are counted as ``tool``.

Everything here is called from hook context: silent-on-failure, never
raises past the public functions.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SELFEVAL_DIR = ".hybrid-search/selfeval"
_EVENTS_FILE = "events.jsonl"
_HARVESTED_FILE = "harvested.jsonl"

# Pre-fetch join state. One file per session so concurrent sessions in the
# same project never consume each other's rows.
_PENDING_DIR = "pending"
_DEFAULT_SESSION = "default"
# Unconsumed pending rows older than this are dropped (and counted as misses).
_PENDING_MAX_AGE_HOURS = 24
# Why a join failed — never silent, or scoring gaps become invisible.
_MISSES_FILE = "misses.jsonl"

# Gold paths that resolve outside the project keep this prefix so the
# regression set can exclude them explicitly instead of carrying a
# machine-specific absolute path.
_EXTERNAL_PREFIX = "external:"
# Bound the suffix walk in _fold_path — deep paths must not cost real time.
_FOLD_MAX_DEPTH = 12

# Bound how much of events.jsonl summarize() reads back (tail lines).
_SUMMARY_READ_LINES = 2000

# Ranks beyond this aren't meaningful "adoption" — the agent scrolled past
# the answer slots. Kept generous; the slot design caps display anyway.
_MAX_TRACKED_PATHS = 10


def _is_search_tool(name: str) -> bool:
    return name == "mcp__hybrid-search__hybrid_search" or name.endswith(
        "__hybrid_search"
    )


# Adoption evidence: the agent actually opened the file. Reading is the
# obvious case, but editing one of the served paths is a *stronger* signal
# of adoption than reading it — and in edit-heavy projects it is far more
# common. Counting only Read reported adopted=0 across 156 pre-fetch turns
# in a project whose tool mix is Bash 1972 · Edit 579 · Read 260.
_OPEN_TOOLS = ("Read", "Edit", "Write", "NotebookEdit")

# Betrayal evidence: the agent went looking on its own. The Grep tool is the
# explicit form; a shell search is the same act with a different spelling,
# and in some projects it is the *only* form that appears.
_SHELL_SEARCH_PREFIXES = ("rg ", "grep ", "ag ", "ack ", "find ", "fd ")


def _open_target(name: str, tool_input: dict) -> str:
    """File path when a tool call opens a file, else ''."""
    if name not in _OPEN_TOOLS:
        return ""
    return (tool_input.get("file_path") or tool_input.get("path") or "").strip()


def _is_shell_search(command: str) -> bool:
    """True when a Bash command is a code search — a Grep in disguise.

    Each ``&&``/``;`` segment is its own command, so ``cd x && rg y`` counts.
    Within a segment only the head matters: a pipe *into* grep is filtering
    another command's output, not searching the codebase for the answer.
    """
    cmd = (command or "").strip().lstrip("!")
    for segment in cmd.replace(";", "&&").split("&&"):
        head = segment.strip().lstrip("(").strip()
        if head.startswith(_SHELL_SEARCH_PREFIXES):
            return True
    return False


def _search_target(name: str, tool_input: dict) -> str | None:
    """Search pattern when a tool call is a code search, else None."""
    if name == "Grep":
        return (tool_input.get("pattern") or "").strip()
    if name == "Bash":
        cmd = (tool_input.get("command") or "").strip()
        if _is_shell_search(cmd):
            return cmd[:120]
    return None


def _result_text(block: dict) -> str:
    """Flatten a tool_result block's content to one string."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text") or ""
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def _parse_result_paths(text: str) -> list[str]:
    """Ordered unique file_paths out of a hybrid_search tool result."""
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    seen: set[str] = set()
    paths: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        fp = r.get("file_path")
        if not isinstance(fp, str) or not fp or fp in seen:
            continue
        seen.add(fp)
        paths.append(fp)
    return paths


def _paths_match(read_path: str, result_path: str) -> bool:
    """Result paths are usually project-relative, Read paths absolute."""
    a = read_path.strip().lstrip("./")
    b = result_path.strip().lstrip("./")
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def extract_turn_events(turn_records: list[dict]) -> list[dict]:
    """Pair each hybrid_search call in a turn with its followup actions.

    ``turn_records`` is the transcript slice for ONE turn (everything after
    the genuine user prompt). Returns one dict per search call:
    ``{"query", "paths", "reads", "greps"}`` where reads/greps are the
    Read file_paths / Grep patterns issued *after* that search and before
    the next one — followups attribute to the most recent search.
    """
    pending: dict[str, dict] = {}  # tool_use_id -> event awaiting its result
    events: list[dict] = []
    current: dict | None = None

    for rec in turn_records:
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        rec_type = rec.get("type")
        for block in content:
            if not isinstance(block, dict):
                continue
            if rec_type == "assistant" and block.get("type") == "tool_use":
                name = block.get("name") or ""
                ti = block.get("tool_input") or block.get("input") or {}
                if _is_search_tool(name):
                    event = {
                        "query": (ti.get("query") or "").strip(),
                        "paths": [],
                        "reads": [],
                        "greps": [],
                    }
                    events.append(event)
                    current = event
                    uid = block.get("id")
                    if isinstance(uid, str) and uid:
                        pending[uid] = event
                elif current is not None:
                    opened = _open_target(name, ti)
                    if opened:
                        current["reads"].append(opened)
                    searched = _search_target(name, ti)
                    if searched is not None:
                        current["greps"].append(searched)
            elif rec_type == "user" and block.get("type") == "tool_result":
                uid = block.get("tool_use_id")
                event = pending.pop(uid, None) if isinstance(uid, str) else None
                if event is not None:
                    event["paths"] = _parse_result_paths(_result_text(block))
    return events


def score_event(event: dict) -> dict:
    """Turn one extracted event into a scored, storable row."""
    paths = event["paths"][:_MAX_TRACKED_PATHS]
    adopted_rank: int | None = None
    outside_reads: list[str] = []
    for read in event["reads"]:
        matched = None
        for rank, rp in enumerate(paths, start=1):
            if _paths_match(read, rp):
                matched = rank
                break
        if matched is not None:
            if adopted_rank is None or matched < adopted_rank:
                adopted_rank = matched
        else:
            outside_reads.append(read)

    grep_count = len(event["greps"])
    if adopted_rank is not None and not outside_reads and grep_count == 0:
        verdict = "adopted"
    elif adopted_rank is not None:
        verdict = "mixed"
    elif outside_reads or grep_count:
        verdict = "betrayed"
    else:
        verdict = "no_followup"

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query": event["query"],
        "n_results": len(event["paths"]),
        "top_paths": paths[:5],
        "adopted_rank": adopted_rank,
        "outside_reads": outside_reads[:5],
        "greps_after": grep_count,
        "verdict": verdict,
    }


def _fold_path(raw: str, project_root: Path) -> str:
    """Make one gold path portable, or mark it external.

    Three cases, in order:

    1. Already under the project root → strip the prefix.
    2. Absolute but elsewhere (a linked worktree of this project, most often)
       → walk the path's suffixes and keep the first one that actually
       **exists** under ``project_root``. Existence is the check: a gold path
       that can't be opened in the main checkout is useless as a fixture, and
       validating beats pattern-matching worktree directory names (which stop
       working the moment the worktree is deleted).
    3. Nothing resolves → prefix with ``external:`` so the regression set can
       drop it deliberately rather than silently carrying a path that is
       machine-specific (``~/.claude/projects/...``) or already gone.
    """
    path = (raw or "").strip()
    if not path:
        return path
    prefix = str(project_root).rstrip("/") + "/"
    if path.startswith(prefix):
        return path[len(prefix):]
    if not path.startswith("/"):
        return path  # already relative — leave it alone
    parts = Path(path).parts[1:]  # drop the leading "/"
    for i in range(max(0, len(parts) - _FOLD_MAX_DEPTH), len(parts)):
        candidate = "/".join(parts[i:])
        if not candidate:
            continue
        try:
            if (project_root / candidate).exists():
                return candidate
        except OSError:
            break
    return _EXTERNAL_PREFIX + path


def _relativize(paths: list[str], project_root: Path) -> list[str]:
    """Store project files root-relative so the regression set is portable."""
    return [_fold_path(p, project_root) for p in paths]


def is_usable_gold(path: str) -> bool:
    """True when a stored gold path can serve as a regression fixture."""
    return bool(path) and not path.startswith(_EXTERNAL_PREFIX)


# ── Pre-fetch lane: pending sidecar ───────────────────────────────────


def _session_slug(session_key: str | None) -> str:
    """Filesystem-safe session file stem."""
    raw = (session_key or "").strip()
    if not raw:
        return _DEFAULT_SESSION
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in raw)
    return safe[:80] or _DEFAULT_SESSION


def _pending_path(project_root: Path, session_key: str | None) -> Path:
    base = project_root / _SELFEVAL_DIR / _PENDING_DIR
    return base / f"{_session_slug(session_key)}.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)  # atomic — a crash never leaves a half-written queue


def record_miss(project_root: Path, reason: str, **fields) -> None:
    """Note a scoring gap. Silent failures here would hide the gaps."""
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason,
            **fields,
        }
        _append_jsonl(project_root / _SELFEVAL_DIR / _MISSES_FILE, row)
    except Exception:
        pass


def record_prefetch(
    project_root: Path,
    *,
    query: str,
    paths: list[str],
    qa_record_id: str | None = None,
    session_key: str | None = None,
) -> bool:
    """Queue one pre-fetch for the Stop hook to score. True when queued.

    Called from UserPromptSubmit, which knows what was served but not yet
    whether it was used. ``qa_record_id`` is the stem of the qa log written
    for the same prompt — it makes the row traceable back to that file.
    """
    try:
        if not query or not paths:
            return False
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "qa_record_id": qa_record_id or "",
            "session_key": _session_slug(session_key),
            "query": query.strip(),
            "top_paths": list(paths)[:_MAX_TRACKED_PATHS],
        }
        _append_jsonl(_pending_path(project_root, session_key), row)
        return True
    except Exception:
        return False


def _pop_pending(
    project_root: Path, session_key: str | None, prompt: str
) -> dict | None:
    """Claim the pending row belonging to this finished turn.

    Match rule: **within one session file, the oldest unconsumed row whose
    query equals the turn's prompt.** The session file removes cross-session
    interference, and FIFO-within-exact-match makes a repeated question
    deterministic — prompts and turns are appended in the same order, so the
    oldest match is always the one this turn started from. A non-matching
    turn (pre-fetch is skipped on non-exploratory prompts) claims nothing.
    """
    target = (prompt or "").strip()
    if not target:
        return None
    candidates = [_pending_path(project_root, session_key)]
    fallback = _pending_path(project_root, None)
    if fallback not in candidates:
        # UserPromptSubmit may not carry a session id; it lands in default.
        candidates.append(fallback)
    for path in candidates:
        rows = _read_jsonl(path)
        for i, row in enumerate(rows):
            if (row.get("query") or "").strip() == target:
                remaining = rows[:i] + rows[i + 1:]
                try:
                    _write_jsonl(path, remaining)
                except OSError:
                    return None
                return row
    return None


def prune_pending(project_root: Path, *, max_age_hours: int = _PENDING_MAX_AGE_HOURS) -> int:
    """Drop stale unconsumed rows. Returns how many were dropped."""
    try:
        base = project_root / _SELFEVAL_DIR / _PENDING_DIR
        if not base.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        dropped = 0
        for path in base.glob("*.jsonl"):
            rows = _read_jsonl(path)
            keep: list[dict] = []
            for row in rows:
                try:
                    ts = datetime.fromisoformat(row["ts"])
                except (ValueError, TypeError, KeyError):
                    continue  # unparseable row is itself stale
                if ts < cutoff:
                    dropped += 1
                    record_miss(
                        project_root,
                        "pending_expired",
                        query=(row.get("query") or "")[:200],
                        qa_record_id=row.get("qa_record_id", ""),
                    )
                else:
                    keep.append(row)
            if len(keep) != len(rows):
                _write_jsonl(path, keep)
        return dropped
    except Exception:
        return 0


def extract_prefetch_event(turn_records: list[dict], pending: dict) -> dict:
    """Build one scorable event from a claimed pending row plus the turn.

    The tool lane attributes followups to the most recent search call. A
    pre-fetch happens once, before the turn starts, so **the whole turn is
    its attribution window** — every Read/Grep in the slice counts.
    """
    reads: list[str] = []
    greps: list[str] = []
    for rec in turn_records:
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if rec.get("type") != "assistant":
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            ti = block.get("tool_input") or block.get("input") or {}
            opened = _open_target(name, ti)
            if opened:
                reads.append(opened)
            searched = _search_target(name, ti)
            if searched is not None:
                greps.append(searched)
    return {
        "query": (pending.get("query") or "").strip(),
        "paths": list(pending.get("top_paths") or []),
        "reads": reads,
        "greps": greps,
    }


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _persist_event(
    project_root: Path, event: dict, source: str, *, qa_record_id: str = ""
) -> dict:
    """Score one event, append it, and harvest a betrayal if there is one.

    ``qa_record_id`` ties the row back to the qa log it came from. It is also
    the idempotency key for the retro scan — a row that carries one is never
    scored twice.
    """
    base = project_root / _SELFEVAL_DIR
    scored = score_event(event)
    row = {
        **scored,
        "outside_reads": _relativize(scored["outside_reads"], project_root),
        "source": source,
    }
    if qa_record_id:
        row["qa_record_id"] = qa_record_id
    _append_jsonl(base / _EVENTS_FILE, row)
    # A betrayal where we saw what the agent actually used is a ready-made
    # benchmark item: the query, and the files that turned out to matter.
    # This is the compounding step.
    if row["verdict"] in ("betrayed", "mixed") and row["outside_reads"]:
        _append_jsonl(
            base / _HARVESTED_FILE,
            {
                "ts": row["ts"],
                "query": row["query"],
                "gold_paths": row["outside_reads"],
                "served_paths": row["top_paths"],
                "verdict": row["verdict"],
                "source": source,
            },
        )
    return row


def record_turn(
    project_root: Path,
    turn_records: list[dict],
    *,
    session_key: str | None = None,
    prompt: str | None = None,
) -> int:
    """Score one finished turn and persist rows. Returns rows written.

    Scores both lanes off the same parse: every ``hybrid_search`` tool call
    in the turn, plus the pre-fetch queued for this prompt (when ``prompt``
    is given and a pending row matches).

    Hook-context entry point: swallows every exception, returns 0 on any
    failure — a scoring bug must never break the user's session.
    """
    try:
        written = 0
        for event in extract_turn_events(turn_records):
            if not event["query"]:
                continue
            _persist_event(project_root, event, "tool")
            written += 1

        if prompt:
            pending = _pop_pending(project_root, session_key, prompt)
            if pending is not None:
                event = extract_prefetch_event(turn_records, pending)
                if event["query"] and event["paths"]:
                    _persist_event(
                        project_root, event, "prefetch",
                        qa_record_id=pending.get("qa_record_id", "") or "",
                    )
                    written += 1
                else:
                    record_miss(
                        project_root,
                        "prefetch_event_empty",
                        query=prompt[:200],
                        qa_record_id=pending.get("qa_record_id", ""),
                    )

        prune_pending(project_root)
        return written
    except Exception:
        return 0


_VERDICTS = ("adopted", "mixed", "betrayed", "no_followup")
_LANES = ("tool", "prefetch")


def _empty_counts() -> dict:
    return {v: 0 for v in _VERDICTS} | {"total": 0}


def summarize(project_root: Path, *, days: int = 7) -> dict | None:
    """Aggregate recent events. None when there is nothing to report.

    ``lanes`` breaks the same counts out by ``source`` so the pre-fetch lane
    (v1.1) can be read separately from explicit tool calls. Rows written
    before v1.1 have no ``source`` and count as ``tool``.
    """
    try:
        events_path = project_root / _SELFEVAL_DIR / _EVENTS_FILE
        if not events_path.is_file():
            return None
        lines = events_path.read_text(encoding="utf-8").splitlines()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        counts = _empty_counts()
        lanes = {lane: _empty_counts() for lane in _LANES}
        for line in lines[-_SUMMARY_READ_LINES:]:
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row["ts"])
            except (ValueError, TypeError, KeyError):
                continue
            if ts < cutoff:
                continue
            verdict = row.get("verdict")
            if verdict not in _VERDICTS:
                continue
            lane = row.get("source") or "tool"
            counts[verdict] += 1
            counts["total"] += 1
            if lane in lanes:
                lanes[lane][verdict] += 1
                lanes[lane]["total"] += 1
        if counts["total"] == 0:
            return None
        harvested_rows = _read_jsonl(project_root / _SELFEVAL_DIR / _HARVESTED_FILE)
        usable_gold = sum(
            1
            for r in harvested_rows
            if any(is_usable_gold(g) for g in (r.get("gold_paths") or []))
        )
        return {
            "days": days,
            "total": counts["total"],
            "harvested_total": len(harvested_rows),
            "harvested_usable": usable_gold,
            "lanes": lanes,
            **{v: counts[v] for v in _VERDICTS},
        }
    except Exception:
        return None


def format_summary_line(project_root: Path, *, days: int = 7) -> str:
    """One-line scorecard for SessionStart injection. '' when silent."""
    stats = summarize(project_root, days=days)
    if stats is None:
        return ""
    lanes = stats.get("lanes") or {}
    lane_bits = " · ".join(
        f"{name} {lanes[name]['total']}" for name in _LANES if lanes.get(name, {}).get("total")
    )
    lane_str = f" ({lane_bits})" if lane_bits else ""
    return (
        f"[selfeval {stats['days']}d] searches {stats['total']}{lane_str} · "
        f"adopted {stats['adopted'] + stats['mixed']} · "
        f"betrayed {stats['betrayed']} · "
        f"harvested {stats['harvested_total']} regression items"
    )


# ── One-shot migrations ───────────────────────────────────────────────
#
# The two functions below exist because v1.1 arrived after the data did.
# They are batch tools (CLI-invoked), not hook-path code: they may be slow
# and they report to stdout, but like everything else here they never raise.

_QA_FRONTMATTER_QUERY = __import__("re").compile(r'^query:\s*"(.*)"\s*$', __import__("re").M)
_QA_RESULT_PATH = __import__("re").compile(r"^### \d+\.\s+`([^`]+)`", __import__("re").M)
_QA_TRIGGER_PREFETCH = "trigger: user_prompt_submit"

# Prompts are clipped differently by the qa writer and the transcript parser,
# so turns are matched on a normalized prefix rather than full equality.
_MATCH_PREFIX_CHARS = 200


def _unescape_frontmatter(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _match_key(text: str) -> str:
    return " ".join((text or "").split())[:_MATCH_PREFIX_CHARS]


def _parse_prefetch_qa(path: Path) -> dict | None:
    """Pull (query, served paths) out of one pre-fetch qa log."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if _QA_TRIGGER_PREFETCH not in text:
        return None
    m = _QA_FRONTMATTER_QUERY.search(text)
    query = _unescape_frontmatter(m.group(1)) if m else ""
    if not query:
        head = [ln for ln in text.splitlines() if ln.startswith("# Q: ")]
        query = head[0][5:].strip() if head else ""
    paths = _QA_RESULT_PATH.findall(text)
    if not query or not paths:
        return None
    # Result headings carry a ":start-end" suffix on code hits; drop it so
    # the path matches what a Read would report.
    cleaned = []
    for p in paths:
        base = p.rsplit(":", 1)[0] if __import__("re").search(r":\d+(-\d+)?$", p) else p
        if base and base not in cleaned:
            cleaned.append(base)
    return {"qa_record_id": path.stem, "query": query, "top_paths": cleaned[:_MAX_TRACKED_PATHS]}


def _transcript_turn_index(project_root: Path) -> dict:
    """Map normalized prompt → (reads, greps) for every turn on disk.

    Reuses the conversation parser rather than re-reading raw transcripts:
    it already groups a session into per-user-turn episodes and records each
    tool call as ``ToolEvent(tool, target)`` — Read targets are file paths,
    Grep targets are patterns, which is exactly what scoring needs.
    """
    index: dict[str, dict] = {}
    try:
        from hybrid_search.index.transcript_source import (
            discover_claude_transcripts,
            parse_claude_transcript,
        )
    except Exception:
        return index
    for tpath in discover_claude_transcripts(project_root):
        try:
            chunks = parse_claude_transcript(tpath)
        except Exception:
            continue
        for chunk in chunks:
            key = _match_key(getattr(chunk, "user_prompt", "") or "")
            if not key or key in index:
                continue
            reads, greps = [], []
            for ev in getattr(chunk, "tools", ()) or ():
                if ev.tool in _OPEN_TOOLS and ev.target:
                    reads.append(ev.target)
                elif ev.tool == "Grep":
                    greps.append(ev.target)
                elif ev.tool == "Bash" and _is_shell_search(ev.target):
                    greps.append(ev.target)
            index[key] = {"reads": reads, "greps": greps}
    return index


def _already_scored_ids(project_root: Path) -> set:
    ids = set()
    for row in _read_jsonl(project_root / _SELFEVAL_DIR / _EVENTS_FILE):
        rid = row.get("qa_record_id")
        if rid:
            ids.add(rid)
    return ids


def reset_retro(project_root: Path) -> int:
    """Drop retro-scored rows so they can be re-derived. Returns rows dropped.

    Retro rows are derived data: they are recomputed from qa logs plus
    transcripts, both of which are still on disk. When the scorer changes,
    the honest move is to re-derive them rather than leave two scoring
    generations mixed in one file. Live rows (no ``qa_record_id``) and
    harvested items are untouched.
    """
    try:
        base = project_root / _SELFEVAL_DIR
        events = _read_jsonl(base / _EVENTS_FILE)
        keep = [r for r in events if not (r.get("source") == "prefetch" and r.get("qa_record_id"))]
        dropped = len(events) - len(keep)
        if dropped:
            _write_jsonl(base / _EVENTS_FILE, keep)
            harvested = _read_jsonl(base / _HARVESTED_FILE)
            kept_h = [r for r in harvested if r.get("source") != "prefetch"]
            if len(kept_h) != len(harvested):
                _write_jsonl(base / _HARVESTED_FILE, kept_h)
        return dropped
    except Exception:
        return 0


def retro_scan(project_root: Path) -> dict:
    """Score pre-fetch turns that predate v1.1, from the qa logs on disk.

    The sidecar only catches pre-fetches from now on; every earlier one is
    already recorded as a qa log with its served paths. This replays those
    against the transcripts and writes them into the same events file, so
    the pre-fetch lane starts with history instead of from zero.

    Idempotent: rows carry ``qa_record_id`` and previously scored ids are
    skipped, so a second run adds nothing.
    """
    stats = {
        "parsed_md": 0, "matched_turns": 0, "scored": 0, "skipped_existing": 0,
        "adopted": 0, "mixed": 0, "betrayed": 0, "no_followup": 0,
    }
    try:
        qa_root = project_root / ".hybrid-search" / "qa"
        if not qa_root.is_dir():
            return stats
        turns = _transcript_turn_index(project_root)
        seen_ids = _already_scored_ids(project_root)
        for md in sorted(qa_root.rglob("*.md")):
            if "consolidated" in md.parts:
                continue
            parsed = _parse_prefetch_qa(md)
            if parsed is None:
                continue
            stats["parsed_md"] += 1
            if parsed["qa_record_id"] in seen_ids:
                stats["skipped_existing"] += 1
                continue
            turn = turns.get(_match_key(parsed["query"]))
            if turn is None:
                record_miss(
                    project_root, "retro_no_transcript_turn",
                    qa_record_id=parsed["qa_record_id"], query=parsed["query"][:200],
                )
                continue
            stats["matched_turns"] += 1
            event = {
                "query": parsed["query"],
                "paths": parsed["top_paths"],
                "reads": turn["reads"],
                "greps": turn["greps"],
            }
            row = _persist_event(
                project_root, event, "prefetch", qa_record_id=parsed["qa_record_id"]
            )
            seen_ids.add(parsed["qa_record_id"])
            stats["scored"] += 1
            stats[row["verdict"]] += 1
        return stats
    except Exception:
        return stats


def migrate_harvested(project_root: Path) -> dict:
    """Re-fold gold paths in harvested.jsonl and report what survives.

    Old rows stored absolute paths for anything outside the project root —
    linked worktrees (which die with the worktree) and ``~/.claude/...``
    (which differs per machine). Neither works as a regression fixture.
    """
    stats = {"rows": 0, "rewritten": 0, "usable_rows": 0, "dropped_paths": 0}
    try:
        path = project_root / _SELFEVAL_DIR / _HARVESTED_FILE
        rows = _read_jsonl(path)
        if not rows:
            return stats
        out = []
        for row in rows:
            stats["rows"] += 1
            gold = row.get("gold_paths") or []
            folded = _relativize(list(gold), project_root)
            if folded != gold:
                stats["rewritten"] += 1
            usable = [g for g in folded if is_usable_gold(g)]
            stats["dropped_paths"] += len(folded) - len(usable)
            if usable:
                stats["usable_rows"] += 1
            out.append({**row, "gold_paths": folded})
        _write_jsonl(path, out)
        return stats
    except Exception:
        return stats
