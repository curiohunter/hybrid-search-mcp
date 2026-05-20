"""Codex lifecycle hooks for hybrid-search memory."""

from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hybrid_search.memory import hook_runtime
from hybrid_search.memory.routing_template import (
    LEGACY_AGENTS_MARKER,
    agents_block,
    apply_update,
)

_PENDING_REL = Path(".hybrid-search") / "runtime" / "codex-last-prompt.json"
_HOOK_MARKER = "hybrid_search.cli codex-hook"
_AGENTS_MARKER = LEGACY_AGENTS_MARKER


def codex_context_response(event: str, text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }


def codex_noop_response() -> dict[str, Any]:
    return {}


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def _pending_path(project_root: Path) -> Path:
    return project_root / _PENDING_REL


def _write_pending_prompt(project_root: Path, event: dict[str, Any]) -> None:
    prompt = (event.get("prompt") or "").strip()
    if not prompt:
        return
    payload = {
        "prompt": prompt,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cwd": str(project_root),
        "hash": _prompt_hash(prompt),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
    }
    path = _pending_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_pending_prompt(project_root: Path, event: dict[str, Any]) -> str | None:
    path = _pending_path(project_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    event_turn = event.get("turn_id")
    pending_turn = payload.get("turn_id")
    if event_turn and pending_turn and event_turn != pending_turn:
        return None
    prompt = payload.get("prompt")
    return prompt.strip() if isinstance(prompt, str) and prompt.strip() else None


def _handle_session_start(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("source") == "clear":
        return codex_noop_response()
    root = hook_runtime.resolve_project_root(event)
    if root is None:
        return codex_noop_response()
    ctx = hook_runtime.build_session_context(root)
    if not ctx:
        return codex_noop_response()
    return codex_context_response("SessionStart", ctx)


def _handle_user_prompt_submit(event: dict[str, Any]) -> dict[str, Any]:
    prompt = (event.get("prompt") or "").strip()
    root = hook_runtime.resolve_project_root(event)
    if root is None:
        return codex_noop_response()

    try:
        _write_pending_prompt(root, event)
    except Exception:
        pass

    if not hook_runtime.classify_prompt_for_memory(prompt):
        return codex_noop_response()

    ctx = hook_runtime.build_user_prompt_context(root, prompt, record_prefetch=False)
    if not ctx:
        return codex_noop_response()
    return codex_context_response("UserPromptSubmit", ctx)


def _handle_stop(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("stop_hook_active"):
        return codex_noop_response()
    root = hook_runtime.resolve_project_root(event)
    if root is None:
        return codex_noop_response()

    prompt = (event.get("prompt") or event.get("last_user_message") or "").strip()
    if not prompt:
        prompt = _read_pending_prompt(root, event) or ""
    answer = event.get("last_assistant_message")
    if not prompt or not isinstance(answer, str) or not answer.strip():
        return codex_noop_response()

    hook_runtime.record_completed_turn(
        root,
        prompt,
        answer,
        trigger="codex_stop_hook",
        tools_used=(),
        client="codex",
    )
    return codex_noop_response()


def run_hook(stdin_text: str) -> int:
    try:
        event = json.loads(stdin_text or "{}")
    except (ValueError, TypeError):
        event = {}
    if not isinstance(event, dict):
        event = {}

    name = event.get("hook_event_name") or event.get("hookEventName") or ""
    try:
        if name == "SessionStart":
            out = _handle_session_start(event)
        elif name == "UserPromptSubmit":
            out = _handle_user_prompt_submit(event)
        elif name == "Stop":
            out = _handle_stop(event)
        else:
            out = codex_noop_response()
    except Exception as exc:
        print(f"hybrid-search codex hook failed: {exc}", file=sys.stderr)
        out = codex_noop_response()

    try:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        pass
    return 0


def cli_main(argv: list[str] | None = None) -> int:
    try:
        data = sys.stdin.read()
    except Exception:
        data = "{}"
    return run_hook(data)


def _hook_command() -> str:
    return f"{sys.executable} -m hybrid_search.cli codex-hook"


def _build_hook_config() -> dict[str, Any]:
    cmd = _hook_command()
    return {
        "SessionStart": [
            {
                "matcher": "startup|resume",
                "hooks": [{"type": "command", "command": cmd, "timeout": 5}],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [{"type": "command", "command": cmd, "timeout": 10}],
            }
        ],
        "Stop": [
            {
                "hooks": [{"type": "command", "command": cmd, "timeout": 5}],
            }
        ],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_codex_hooks(existing: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    incoming = _build_hook_config()
    current_cmd = _hook_command()
    hooks = dict(existing.get("hooks") or {})
    added = 0
    updated = 0
    for event_name, entries in incoming.items():
        bucket = list(hooks.get(event_name) or [])
        for entry in entries:
            found = False
            for existing_entry in bucket:
                for handler in existing_entry.get("hooks") or []:
                    cmd = handler.get("command") or ""
                    if _HOOK_MARKER in cmd:
                        found = True
                        if cmd != current_cmd:
                            handler["command"] = current_cmd
                            updated += 1
                        if handler.get("type") != "command":
                            handler["type"] = "command"
                            updated += 1
                        break
                if found:
                    break
            if not found:
                bucket.append(entry)
                added += 1
        hooks[event_name] = bucket
    out = dict(existing)
    out["hooks"] = hooks
    return out, added, updated


def _set_feature_flag(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    out: list[str] = []
    in_features = False
    saw_features = False
    saw_flag = False
    changed = False
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_features and not saw_flag:
                out.append("hooks = true")
                changed = True
                inserted = True
            in_features = stripped == "[features]"
            saw_features = saw_features or in_features
        if in_features and stripped.startswith("hooks"):
            saw_flag = True
            if stripped != "hooks = true":
                out.append("hooks = true")
                changed = True
            else:
                out.append(line)
            continue
        if in_features and stripped.startswith("codex_hooks"):
            if not saw_flag:
                out.append("hooks = true")
                saw_flag = True
            changed = True
            continue
        out.append(line)
    if in_features and not saw_flag and not inserted:
        out.append("hooks = true")
        changed = True
    if not saw_features:
        if out and out[-1].strip():
            out.append("")
        out.extend(["[features]", "hooks = true"])
        changed = True
    return "\n".join(out).rstrip() + "\n", changed


def _replace_toml_table(text: str, table: str, body_lines: list[str]) -> tuple[str, bool]:
    header = f"[{table}]"
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    found = False
    old_block: list[str] = []
    new_block = [header, *body_lines]
    while i < len(lines):
        if lines[i].strip() == header:
            found = True
            old_block = [lines[i]]
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    break
                old_block.append(lines[i])
                i += 1
            out.extend(new_block)
            continue
        out.append(lines[i])
        i += 1
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.extend(new_block)
        return "\n".join(out).rstrip() + "\n", True
    changed = [line.rstrip() for line in old_block] != new_block
    return "\n".join(out).rstrip() + "\n", changed


def _update_config_toml(path: Path) -> tuple[bool, bool]:
    text = ""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
    text, feature_changed = _set_feature_flag(text)
    text, mcp_changed = _replace_toml_table(
        text,
        "mcp_servers.hybrid-search",
        [
            f'command = "{sys.executable}"',
            'args = ["-m", "hybrid_search.cli", "serve"]',
        ],
    )
    if feature_changed or mcp_changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return feature_changed, mcp_changed


def _append_unique_line(path: Path, line: str) -> bool:
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    if line in existing.splitlines():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(existing + suffix + line + "\n", encoding="utf-8")
    return True


def _update_agents_md(path: Path) -> bool:
    result = apply_update(path, agents_block())
    return result.written


def _has_toml_codex_config(path: Path) -> tuple[bool, bool]:
    if not path.exists():
        return False, False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False, False
    features = data.get("features") if isinstance(data, dict) else {}
    mcp = data.get("mcp_servers") if isinstance(data, dict) else {}
    feature_ok = isinstance(features, dict) and features.get("hooks") is True
    server = mcp.get("hybrid-search") if isinstance(mcp, dict) else None
    mcp_ok = isinstance(server, dict) and server.get("command") == sys.executable
    return feature_ok, mcp_ok


def install_codex_hook(
    project_root: Path,
    *,
    user: bool = False,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Install Codex hooks.json plus config.toml MCP registration."""
    if user:
        codex_dir = Path.home() / ".codex"
        agents_path = None
    else:
        codex_dir = project_root / ".codex"
        agents_path = project_root / "AGENTS.md"

    hooks_path = codex_dir / "hooks.json"
    config_path = codex_dir / "config.toml"

    existing = _load_json(hooks_path)
    merged, added, updated = _merge_codex_hooks(existing)

    if dry_run:
        return {
            "status": "dry-run",
            "hooks_path": hooks_path,
            "config_path": config_path,
            "added": added,
            "updated": updated,
        }

    if added or updated or not hooks_path.exists():
        _atomic_write_json(hooks_path, merged)

    feature_changed, mcp_changed = _update_config_toml(config_path)
    gitignore_changed = False
    agents_changed = False
    if not user:
        gitignore_changed = _append_unique_line(project_root / ".gitignore", ".hybrid-search/runtime/")
        if agents_path is not None:
            result = apply_update(agents_path, agents_block(), force=force)
            agents_changed = result.written

    changed = any((added, updated, feature_changed, mcp_changed, gitignore_changed, agents_changed))
    return {
        "status": "wrote" if changed else "exists",
        "hooks_path": hooks_path,
        "config_path": config_path,
        "added": added,
        "updated": updated,
        "feature_changed": feature_changed,
        "mcp_changed": mcp_changed,
        "gitignore_changed": gitignore_changed,
        "agents_changed": agents_changed,
        "user": user,
    }


def codex_status(project_root: Path) -> dict[str, Any]:
    project_hooks = project_root / ".codex" / "hooks.json"
    project_config = project_root / ".codex" / "config.toml"
    user_hooks = Path.home() / ".codex" / "hooks.json"
    user_config = Path.home() / ".codex" / "config.toml"

    def hooks_ok(path: Path) -> bool:
        payload = _load_json(path)
        hooks = payload.get("hooks") if isinstance(payload, dict) else None
        if not isinstance(hooks, dict):
            return False
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            if _HOOK_MARKER not in json.dumps(hooks.get(event, [])):
                return False
        return True

    project_feature, project_mcp = _has_toml_codex_config(project_config)
    user_feature, user_mcp = _has_toml_codex_config(user_config)
    override = project_root / "AGENTS.override.md"
    agents = project_root / "AGENTS.md"
    agents_size = agents.stat().st_size if agents.exists() else 0
    return {
        "project_hooks": hooks_ok(project_hooks),
        "project_hooks_path": project_hooks,
        "project_feature": project_feature,
        "project_mcp": project_mcp,
        "user_hooks": hooks_ok(user_hooks),
        "user_hooks_path": user_hooks,
        "user_feature": user_feature,
        "user_mcp": user_mcp,
        "agents_override": override.exists(),
        "agents_size": agents_size,
        "agents_near_limit": agents_size >= 28 * 1024,
    }
