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
    cwd = event.get("cwd")
    if not cwd:
        return None
    try:
        return Path(cwd).resolve()
    except (OSError, ValueError):
        return None


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

    from hybrid_search.memory import reader

    try:
        indexes = list(reader.iter_qa_indexes(root))
    except Exception:
        return None
    if not indexes:
        return None

    ctx = _format_session_start_context(indexes[:20])
    if not ctx:
        return None
    ctx = ctx[:_MAX_CONTEXT_CHARS]
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }


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

DEFAULT_HOOK_CONFIG: dict = {
    "PreToolUse": [
        {
            "matcher": "Grep|Read",
            "hooks": [
                {
                    "type": "command",
                    "command": (
                        "python -m hybrid_search.cli qa-hook 2>/dev/null || true"
                    ),
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
                    "command": (
                        "python -m hybrid_search.cli qa-hook 2>/dev/null || true"
                    ),
                    "timeout": 5,
                }
            ],
        }
    ],
}


def _merge_hooks(existing: dict, incoming: dict) -> tuple[dict, int]:
    """Merge ``incoming`` hook config into ``existing`` without clobbering.

    Returns ``(merged, added_count)``. Hooks whose command line already
    mentions ``_HOOK_MARKER`` are treated as already-installed and skipped.
    """
    merged_hooks = dict(existing.get("hooks") or {})
    added = 0
    for event_name, entries in incoming.items():
        bucket = list(merged_hooks.get(event_name) or [])
        for entry in entries:
            # Does any existing entry under this event already contain our marker?
            already = False
            for existing_entry in bucket:
                for h in existing_entry.get("hooks") or []:
                    cmd = (h.get("command") or "")
                    if _HOOK_MARKER in cmd:
                        already = True
                        break
                if already:
                    break
            if not already:
                bucket.append(entry)
                added += 1
        merged_hooks[event_name] = bucket
    out = dict(existing)
    out["hooks"] = merged_hooks
    return out, added


def install_memory_hook(settings_path: Path, *, dry_run: bool = False) -> dict:
    """Merge the memory hook into ``settings_path`` (creating it when absent).

    Returns ``{'added': int, 'path': Path, 'status': 'ok' | 'exists' | 'wrote'}``.
    """
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            existing = {}

    merged, added = _merge_hooks(existing, DEFAULT_HOOK_CONFIG)
    if added == 0:
        return {"added": 0, "path": settings_path, "status": "exists"}

    if dry_run:
        return {"added": added, "path": settings_path, "status": "dry-run"}

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(settings_path)
    return {"added": added, "path": settings_path, "status": "wrote"}
