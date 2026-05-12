"""Claude Code hooks — memory injection before Grep/Read and on SessionStart.

Two entry points, both read a JSON payload from stdin and emit a JSON
``hookSpecificOutput`` envelope on stdout. Claude Code injects the
``additionalContext`` string into the model's context window before the
tool runs (PreToolUse) or at the start of the session (SessionStart).

The stdin contract (from Claude Code hook docs):
    {
      "hook_event_name": "PreToolUse" | "SessionStart",
      "tool_name": "Grep" | "Read" | ...,           # PreToolUse only
      "tool_input": { "pattern": "...", ... },      # PreToolUse only
      "cwd": "/path/to/project",
      ...
    }

Output (exit 0):
    {
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "[memory] 2 past Q&A match 'parseConfig': ..."
      }
    }

Everything is silent-on-failure: a crashed hook MUST NOT break the user's
tool call. All exceptions are swallowed and mapped to exit 0 with no
``additionalContext``.

Token hygiene:
- `additionalContext` is capped at `_MAX_CONTEXT_CHARS` so the context
  window doesn't balloon across many Grep calls.
- Weak signals (stopword-only patterns, very short paths) short-circuit
  without any output.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Cap injected context so a chatty session with 20 Greps doesn't spend
# thousands of tokens on stale memory. 800 is generous enough for 2-3
# trimmed Q&A references, tight enough to stay invisible.
_MAX_CONTEXT_CHARS = 800

# PreToolUse input patterns that shouldn't trigger memory lookup. Short
# generic words have no retrieval value and just pollute context.
_NOISE_PATTERNS = frozenset({
    "",
    " ",
    "*",
    ".",
    "/",
    "\\",
    "-",
    "_",
    "=",
    "+",
    "test",
    "todo",
    "fixme",
    "foo",
    "bar",
})


def _extract_term(event: dict) -> str | None:
    """Pull the searchable term out of a PreToolUse payload.

    - Grep → ``tool_input.pattern``
    - Read → filename portion of ``tool_input.file_path``
    - Others → None (hook is a no-op)
    """
    tool = event.get("tool_name") or ""
    ti = event.get("tool_input") or {}
    if tool == "Grep":
        pat = (ti.get("pattern") or "").strip()
        return pat or None
    if tool == "Read":
        fp = (ti.get("file_path") or "").strip()
        if not fp:
            return None
        return Path(fp).name or fp
    return None


def _looks_like_noise(term: str) -> bool:
    """Filter queries that would return junk or no hits anyway."""
    if not term:
        return True
    t = term.strip().lower()
    if t in _NOISE_PATTERNS:
        return True
    if len(t) < 3:
        return True
    # All-punctuation patterns (e.g., `.*`, `//`, `=>`) have no signal.
    if not any(c.isalnum() for c in t):
        return True
    return False


def _format_pretooluse_context(term: str, hits: list) -> str:
    """Render up to 3 qa_log hits as a compact markdown snippet."""
    from hybrid_search.memory import reader

    seen_paths: set[str] = set()
    unique: list[reader.GrepHit] = []
    for h in hits:
        p = str(h.index.path)
        if p in seen_paths:
            continue
        seen_paths.add(p)
        unique.append(h)
        if len(unique) >= 3:
            break

    if not unique:
        return ""

    lines = [f"[hybrid-search memory] {len(unique)} past Q&A match '{term}':"]
    for h in unique:
        ts = h.index.timestamp.date().isoformat() if h.index.timestamp else "?"
        q = (h.index.query or "").strip()
        if len(q) > 90:
            q = q[:87] + "…"
        lines.append(f"- {ts} — {q}  (id: {h.index.id})")
    lines.append(
        "Consider `mcp__hybrid-search__hybrid_search` before proceeding with raw Grep/Read."
    )
    return "\n".join(lines)


def _format_session_start_context(indexes: list) -> str:
    """Summarize the 20 most recent qa logs for one-time session injection."""
    if not indexes:
        return ""
    lines = [
        f"[hybrid-search memory] You have {len(indexes)} recent past Q&A in this project.",
        "Before running Grep/Read for information you might have asked about,",
        "call `mcp__hybrid-search__hybrid_search` — it searches past Q&A alongside code/docs.",
        "Recent topics:",
    ]
    for idx in indexes[:20]:
        ts = idx.timestamp.date().isoformat() if idx.timestamp else "?"
        q = (idx.query or "").strip()
        if len(q) > 80:
            q = q[:77] + "…"
        lines.append(f"- {ts} — {q}")
    return "\n".join(lines)


def _resolve_project_root(event: dict) -> Path | None:
    """Pick the project root from the hook event's ``cwd``. None if missing."""
    from hybrid_search.memory import hook_runtime

    return hook_runtime.resolve_project_root(event)


def _handle_pretooluse(event: dict) -> dict | None:
    term = _extract_term(event)
    if term is None or _looks_like_noise(term):
        return None
    root = _resolve_project_root(event)
    if root is None:
        return None

    from hybrid_search.memory import reader

    try:
        hits = list(reader.grep_qa(root, term))
    except Exception:
        return None
    if not hits:
        return None

    ctx = _format_pretooluse_context(term, hits)
    if not ctx:
        return None
    ctx = ctx[:_MAX_CONTEXT_CHARS]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": ctx,
        }
    }


def _handle_session_start(event: dict) -> dict | None:
    root = _resolve_project_root(event)
    if root is None:
        return None

    from hybrid_search.memory import hook_runtime

    ctx = hook_runtime.build_session_context(root)
    if not ctx:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }


# ── Stop hook — transcript parsing + qa_log save ──────────────────────

# Hard cap on how much of the transcript we load. Real transcripts are
# rarely this big, but misconfigured sessions can grow to many MB.
_TRANSCRIPT_READ_LIMIT_BYTES = 8 * 1024 * 1024

# Upper bound on number of transcript lines we inspect from the tail.
_TRANSCRIPT_TAIL_LINES = 400

# Stop hook never blocks Claude. Even on failure, return exit 0 silently.
# Collect a bounded assistant excerpt; qa_log applies the final storage cap.
_ANSWER_EXCERPT_COLLECT_MAX_CHARS = 4000


def _read_transcript_tail(transcript_path: Path) -> list[dict]:
    """Return the tail of a JSONL transcript as a list of decoded records.

    Handles the common issues: file missing, file truncated, JSON lines that
    fail to parse (skipped). Always returns a list — empty when unreadable.
    """
    try:
        stat = transcript_path.stat()
    except OSError:
        return []
    try:
        if stat.st_size > _TRANSCRIPT_READ_LIMIT_BYTES:
            # Seek near the tail; losing some context here is fine.
            with transcript_path.open("rb") as f:
                f.seek(-_TRANSCRIPT_READ_LIMIT_BYTES, 2)
                raw = f.read().decode("utf-8", errors="replace")
                # Drop the first (likely partial) line after the seek.
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        else:
            raw = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = raw.splitlines()[-_TRANSCRIPT_TAIL_LINES:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def _extract_user_text(user_rec: dict) -> str | None:
    """Pull a single user-authored prompt string out of a transcript record.

    Skips tool_result messages, local-command stdout, and system-reminder
    blocks — none of those are "the question" we want to remember.
    """
    msg = user_rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        s = content.strip()
        if not s:
            return None
        # Claude Code embeds local command output / system reminders with
        # recognisable prefixes. Skip those — they're not real user turns.
        if s.startswith("<local-command-") or s.startswith("<command-"):
            return None
        if s.startswith("<system-reminder>"):
            return None
        return s
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "tool_result":
                # Entire record is a tool-result envelope — not a user prompt.
                return None
            if t == "text":
                txt = (block.get("text") or "").strip()
                if txt and not txt.startswith("<system-reminder>"):
                    return txt
    return None


def _extract_assistant_summary(
    records: list[dict],
    from_idx: int,
) -> tuple[list[str], int, str]:
    """Walk forward from ``from_idx`` through assistant records.

    Accumulates (tool_names, text_chars) until the next user turn or end
    of list. Duplicate tool names are preserved in order of first use.
    """
    seen_tools: list[str] = []
    text_chars = 0
    excerpts: list[str] = []
    excerpt_chars = 0
    for i in range(from_idx, len(records)):
        rec = records[i]
        if rec.get("type") == "user":
            break
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                txt = block.get("text") or ""
                text_chars += len(txt)
                if txt and excerpt_chars < _ANSWER_EXCERPT_COLLECT_MAX_CHARS:
                    remaining = _ANSWER_EXCERPT_COLLECT_MAX_CHARS - excerpt_chars
                    excerpts.append(txt[:remaining])
                    excerpt_chars += min(len(txt), remaining)
            elif t == "tool_use":
                name = block.get("name") or ""
                if name and name not in seen_tools:
                    seen_tools.append(name)
    return seen_tools, text_chars, "\n\n".join(e.strip() for e in excerpts if e.strip())


def _find_last_turn(records: list[dict]) -> tuple[str | None, list[str], int, str]:
    """Return (last_user_prompt, tools_used, answer_chars, answer_excerpt).

    Walks the tail backwards to find the most recent genuine user prompt,
    then collects the assistant activity that followed it.
    """
    for idx in range(len(records) - 1, -1, -1):
        rec = records[idx]
        if rec.get("type") != "user":
            continue
        prompt = _extract_user_text(rec)
        if prompt is None:
            continue
        tools, chars, excerpt = _extract_assistant_summary(records, idx + 1)
        return prompt, tools, chars, excerpt
    return None, [], 0, ""


# ── UserPromptSubmit hook — auto-MCP on exploratory prompts ───────────

# Trigger words in Korean + English that signal "please describe / explore
# a subsystem or concept". These beat Claude's tool-choice drift because
# the hook runs BEFORE Claude sees the prompt, so memory is already seeded
# regardless of whether Claude eventually picks MCP or Grep.
_EXPLORATORY_TOKENS_KO = (
    "어떤", "어떻게", "무엇", "무슨", "왜", "어디",
    "설명", "정리", "알려", "보여", "소개",
    "구조", "구성", "흐름", "관계", "아키텍처", "전체",
    "기능", "역할",
)
# Memory-intent tokens bypass the minimum-length gate — even a short
# "지난번에 뭐 물어봤지" deserves the pre-fetch since it's exactly the
# case the memory layer was built for.
_MEMORY_INTENT_TOKENS_KO = ("지난번", "이전에", "아까", "저번", "그때")
_EXPLORATORY_TOKENS_EN_RE = __import__("re").compile(
    r"\b(how|what|why|where|explain|describe|overview|summary|structure|"
    r"architecture|flow|related|tell\s+me|show\s+me|walk\s+me\s+through)\b",
    __import__("re").IGNORECASE,
)
_MEMORY_INTENT_TOKENS_EN_RE = __import__("re").compile(
    r"\b(previously|earlier|last\s+time|the\s+other\s+day|what\s+did\s+(?:i|we|you)\s+(?:ask|say))\b",
    __import__("re").IGNORECASE,
)
# Min prompt length below which we skip. Short single-word prompts
# ("Thanks", "ok", "next") aren't worth pre-fetching.
_EXPLORATORY_MIN_CHARS = 12
# Skip prefixes. Slash commands and bash pass-through don't want
# pre-enrichment; @mentions are file-scoped and Claude handles them.
_SKIP_PREFIXES = ("/", "!", "#")


def _is_exploratory_prompt(prompt: str) -> bool:
    """Heuristic: should we pre-fetch hybrid_search on this prompt?

    Memory-intent tokens (지난번, previously, etc.) bypass the length gate
    because they're the memory layer's canonical use case.
    """
    from hybrid_search.memory import hook_runtime

    return hook_runtime.classify_prompt_for_memory(prompt)


def _format_user_prompt_context(prompt: str, response) -> str:
    """Render the pre-fetched hybrid_search top-K as additionalContext."""
    from hybrid_search.memory import hook_runtime

    return hook_runtime._format_user_prompt_context(response, prompt)


def _run_programmatic_search(prompt: str, cwd: str):
    """Run hybrid_search in-process for pre-fetch. Returns Response or None.

    Failures here must be silent — a failed pre-fetch just means the user
    misses one enrichment pass, never a blocked prompt.
    """
    from hybrid_search.memory import hook_runtime

    response = hook_runtime._run_programmatic_search(prompt, cwd)
    if response is None:
        return None

    # Save the pre-fetch exchange under trigger="user_prompt_submit" so it
    # becomes part of the qa_log corpus like any other turn. Sync write —
    # hook lifecycle is already synchronous, and we want the save committed
    # before the Stop hook later tries to dedup.
    try:
        from hybrid_search.memory import qa_log  # noqa: F811
        qa_log.record(
            query=prompt,
            response=response,
            cwd=cwd,
            async_write=False,
            trigger="user_prompt_submit",
        )
    except Exception:
        pass

    return response


def _handle_user_prompt_submit(event: dict) -> dict | None:
    """UserPromptSubmit entry — pre-fetch + inject on exploratory prompts.

    Silent on non-exploratory prompts (no classification → no context added).
    Silent on search failures. Output respects the
    ``hookSpecificOutput.additionalContext`` envelope.
    """
    prompt = (event.get("prompt") or "").strip()
    if not _is_exploratory_prompt(prompt):
        return None

    root = _resolve_project_root(event)
    if root is None:
        return None

    response = _run_programmatic_search(prompt, str(root))
    if response is None:
        return None

    ctx = _format_user_prompt_context(prompt, response)
    if not ctx:
        return None
    ctx = ctx[:_MAX_CONTEXT_CHARS]
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }
    }


def _handle_stop(event: dict) -> dict | None:
    """Stop hook entry — parse transcript, save qa_log, return None (silent).

    Returns None on purpose: Stop hook output is terminal (can't inject
    context into a subsequent turn), so we never emit additionalContext.
    A successful save is a pure side effect.
    """
    # Avoid infinite continuation loops — Claude Code sets this flag when
    # it's already in a stop-hook-triggered continuation.
    if event.get("stop_hook_active"):
        return None

    root = _resolve_project_root(event)
    if root is None:
        return None

    transcript_raw = event.get("transcript_path")
    if not transcript_raw:
        return None
    try:
        transcript_path = Path(transcript_raw).expanduser()
    except (OSError, ValueError):
        return None
    if not transcript_path.is_file():
        return None

    records = _read_transcript_tail(transcript_path)
    if not records:
        return None

    prompt, tools, answer_chars, answer_excerpt = _find_last_turn(records)
    if prompt is None:
        return None

    from hybrid_search.memory import qa_log

    try:
        qa_log.record_turn(
            query=prompt,
            cwd=str(root),
            tools_used=tools,
            answer_chars=answer_chars,
            answer_excerpt=answer_excerpt,
            trigger="stop_hook",
            async_write=False,
            dedup=True,
        )
    except Exception:
        # Never let save failures bubble up — Stop hook must exit 0 cleanly.
        pass
    return None


def run_hook(stdin_text: str) -> int:
    """Parse a hook payload, emit the JSON envelope, return an exit code."""
    try:
        event = json.loads(stdin_text or "{}")
    except (ValueError, TypeError):
        return 0

    name = event.get("hook_event_name") or event.get("hookEventName") or ""

    if name == "PreToolUse":
        out = _handle_pretooluse(event)
    elif name == "SessionStart":
        out = _handle_session_start(event)
    elif name == "UserPromptSubmit":
        out = _handle_user_prompt_submit(event)
    elif name == "Stop":
        out = _handle_stop(event)
    else:
        out = None

    if out is not None:
        try:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.stdout.flush()
        except Exception:
            # Even output failures must not block the tool call.
            pass
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    """Entry point for ``hybrid-search qa-hook`` — reads stdin, writes stdout."""
    try:
        data = sys.stdin.read()
    except Exception:
        return 0
    return run_hook(data)


# ── Install helper ────────────────────────────────────────────────────

_HOOK_MARKER = "hybrid_search.cli qa-hook"  # so we can detect & skip re-install


def _hook_command() -> str:
    """Build the hook shell command using the *current* interpreter.

    Users often install hybrid-search-mcp in a virtualenv whose ``bin/`` dir
    is not on the system PATH. Hooks run with the user's login shell, so
    ``python -m hybrid_search.cli`` would silently fail when the default
    ``python`` can't see the package. Embedding ``sys.executable`` pins the
    hook to the interpreter that installed the package — the same trick the
    MCP server registration uses.
    """
    return f"{sys.executable} -m hybrid_search.cli qa-hook 2>/dev/null || true"


def _build_default_hook_config() -> dict:
    cmd = _hook_command()
    # UserPromptSubmit may run an actual hybrid_search call, so it gets
    # a slightly larger timeout than the lighter lookup-only hooks.
    return {
        "PreToolUse": [
            {
                "matcher": "Grep|Read",
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd,
                        "timeout": 5,
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd,
                        "timeout": 5,
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd,
                        "timeout": 10,
                    }
                ],
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": cmd,
                        "timeout": 5,
                    }
                ],
            }
        ],
    }


# Kept for backwards-compat; callers should prefer ``_build_default_hook_config``.
DEFAULT_HOOK_CONFIG: dict = _build_default_hook_config()


def _merge_hooks(existing: dict, incoming: dict) -> tuple[dict, int, int]:
    """Merge ``incoming`` hook config into ``existing`` without clobbering.

    Returns ``(merged, added_count, updated_count)``.

    - ``added`` counts newly-inserted hook entries under a given event.
    - ``updated`` counts existing entries whose command line carries our
      marker but points at a stale Python interpreter (different from the
      one that's running right now). Those are rewritten in place so the
      user doesn't have to hand-edit ``settings.local.json`` after moving
      the venv or upgrading the package.
    """
    current_cmd = _hook_command()
    merged_hooks = dict(existing.get("hooks") or {})
    added = 0
    updated = 0
    for event_name, entries in incoming.items():
        bucket = list(merged_hooks.get(event_name) or [])
        for entry in entries:
            already = False
            for existing_entry in bucket:
                for h in existing_entry.get("hooks") or []:
                    cmd = h.get("command") or ""
                    if _HOOK_MARKER in cmd:
                        already = True
                        if cmd != current_cmd:
                            h["command"] = current_cmd
                            updated += 1
                        break
                if already:
                    break
            if not already:
                bucket.append(entry)
                added += 1
        merged_hooks[event_name] = bucket
    out = dict(existing)
    out["hooks"] = merged_hooks
    return out, added, updated


def install_memory_hook(settings_path: Path, *, dry_run: bool = False) -> dict:
    """Merge the memory hook into ``settings_path`` (creating it when absent).

    The hook command embeds the current Python interpreter path so it works
    regardless of whether ``python`` is on the user's login-shell PATH.

    Returns ``{'added': int, 'path': Path, 'status': 'ok' | 'exists' | 'wrote'}``.
    """
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            existing = {}

    merged, added, updated = _merge_hooks(existing, _build_default_hook_config())
    if added == 0 and updated == 0:
        return {"added": 0, "updated": 0, "path": settings_path, "status": "exists"}

    if dry_run:
        return {
            "added": added,
            "updated": updated,
            "path": settings_path,
            "status": "dry-run",
        }

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(settings_path)
    return {
        "added": added,
        "updated": updated,
        "path": settings_path,
        "status": "wrote",
    }
