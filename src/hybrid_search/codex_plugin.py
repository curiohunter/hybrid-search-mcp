"""Codex first-class plugin packaging + smoke test (P0-3).

The Codex hook/MCP plumbing already exists (`codex_hooks.py`); what made
the Codex side a second-class citizen was the install *experience*:
a separate command, no bundled manifest, and no way to prove after
install that both agents actually share one memory root. This module
adds the three missing pieces:

- ``build_plugin_manifest`` — a single ``.codex-plugin/plugin.json``
  bundling hooks + MCP registration, so plugin-aware Codex versions get
  everything from one file. The legacy ``hooks.json``/``config.toml``
  writes are kept alongside for older Codex releases.
- ``install_codex_plugin`` — one idempotent call that writes both.
- ``smoke_test`` — post-install proof: hooks registered, a fake Stop
  event round-trips into a qa file (then cleaned up), MCP registered on
  both agents, and the shared `.hybrid-search/` root is wired from both
  sides. This is what `setup --codex` prints and `doctor --codex` re-runs.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hybrid_search import codex_hooks

PLUGIN_DIRNAME = ".codex-plugin"
PLUGIN_NAME = "hybrid-search-memory-layer"


@dataclass
class SmokeCheck:
    name: str
    ok: bool
    detail: str
    # True when the check could not run (e.g. qa logging disabled) —
    # displayed as SKIP, never as PASS (round-1 review).
    skipped: bool = False


def build_plugin_manifest() -> dict[str, Any]:
    """Plugin manifest bundling hooks + MCP for plugin-aware Codex."""
    from hybrid_search import __version__

    return {
        "name": PLUGIN_NAME,
        "version": __version__,
        "description": (
            "Evidence-grounded shared memory for Claude Code + Codex: "
            "hybrid search over conversations, plans, commits, code, docs."
        ),
        "hooks": codex_hooks._build_hook_config(),
        "mcpServers": {
            "hybrid-search": {
                "command": sys.executable,
                "args": ["-m", "hybrid_search.server"],
            }
        },
    }


def manifest_path(project_root: Path, *, user: bool = False) -> Path:
    if user:
        return Path.home() / ".codex" / "plugins" / PLUGIN_NAME / "plugin.json"
    return project_root / PLUGIN_DIRNAME / "plugin.json"


def install_codex_plugin(
    project_root: Path,
    *,
    user: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Write the plugin manifest AND the legacy hook/config files.

    Idempotent: unchanged files are left untouched so repeat runs report
    "exists" and never churn mtimes (CX-T1).
    """
    path = manifest_path(project_root, user=user)
    manifest = build_plugin_manifest()
    rendered = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    manifest_changed = False
    if not path.exists() or path.read_text(encoding="utf-8") != rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(rendered, encoding="utf-8")
        tmp.replace(path)
        manifest_changed = True

    legacy = codex_hooks.install_codex_hook(project_root, user=user, force=force)
    return {
        "manifest_path": path,
        "manifest_changed": manifest_changed,
        "legacy": legacy,
        "status": "wrote" if manifest_changed or legacy.get("status") == "wrote" else "exists",
    }


# --- smoke test -----------------------------------------------------------


def smoke_test(project_root: Path, *, user: bool = False) -> list[SmokeCheck]:
    """CONFIG/HANDLER smoke — not an install E2E (round-1 review).

    What this proves: hook config is registered, the Stop handler writes
    a qa record when invoked in-process (cleaned up afterwards — a smoke
    artifact must never pollute the corpus), MCP config keys exist on
    both agents, and both agent surfaces point at the same
    `.hybrid-search/` root.

    What this does NOT prove (release gate, pending — CX-T2/CX-T3):
    Codex actually invoking the installed hook command as a subprocess,
    a live MCP handshake + search round-trip, and a real
    Claude-write → Codex-read recall on a clean machine.
    """
    checks: list[SmokeCheck] = []
    status = codex_hooks.codex_status(project_root)

    scope = "user" if user else "project"
    hooks_ok = bool(status.get(f"{scope}_hooks"))
    checks.append(SmokeCheck(
        "hooks-registered", hooks_ok,
        str(status.get(f"{scope}_hooks_path")),
    ))

    checks.append(_stop_roundtrip_check(project_root))

    mcp_codex = bool(status.get(f"{scope}_mcp"))
    mcp_claude = _claude_mcp_registered()
    checks.append(SmokeCheck(
        "mcp-both-agents", mcp_codex and mcp_claude,
        f"codex={'ok' if mcp_codex else 'missing'} "
        f"claude={'ok' if mcp_claude else 'missing'}",
    ))

    checks.append(_shared_root_check(project_root))
    return checks


def _stop_roundtrip_check(project_root: Path) -> SmokeCheck:
    """Inject a fake Stop event and verify a qa record lands on disk."""
    from hybrid_search.memory import qa_log

    if not qa_log.is_enabled():
        return SmokeCheck(
            "stop-event-roundtrip", True,
            "qa logging disabled by env — roundtrip not exercised",
            skipped=True,
        )
    marker = f"codex-smoke-{uuid.uuid4().hex[:8]}"
    event = {
        "hook_event_name": "Stop",
        "cwd": str(project_root),
        "prompt": f"smoke test probe {marker}",
        "last_assistant_message": f"smoke test answer {marker}",
        "session_id": f"smoke-{marker}",
    }
    try:
        codex_hooks._handle_stop(event)
    except Exception as exc:
        return SmokeCheck("stop-event-roundtrip", False, f"hook raised: {exc}")

    qa_dir = project_root / ".hybrid-search" / "qa"
    deadline = time.monotonic() + 3.0
    found: Path | None = None
    while time.monotonic() < deadline and found is None:
        if qa_dir.is_dir():
            for p in qa_dir.rglob("*.md"):
                try:
                    if marker in p.read_text(encoding="utf-8"):
                        found = p
                        break
                except OSError:
                    continue
        if found is None:
            time.sleep(0.1)
    if found is None:
        return SmokeCheck(
            "stop-event-roundtrip", False,
            f"no qa record containing {marker} under {qa_dir}",
        )
    try:
        found.unlink()  # never leave smoke artifacts in the corpus
    except OSError:
        pass
    return SmokeCheck("stop-event-roundtrip", True, f"qa written + cleaned ({found.name})")


def _claude_mcp_registered() -> bool:
    claude_json = Path.home() / ".claude.json"
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return "hybrid-search" in (data.get("mcpServers") or {})


def _shared_root_check(project_root: Path) -> SmokeCheck:
    """Both agents must be wired to the SAME `.hybrid-search/` root:
    CLAUDE.md carries the Claude-side search section, AGENTS.md carries
    the Codex-side routing block, and the root itself exists."""
    root = project_root / ".hybrid-search"
    claude_wired = _contains(project_root / "CLAUDE.md", "hybrid-search")
    agents_md = project_root / "AGENTS.md"
    codex_wired = _contains(agents_md, "hybrid-search-mcp routing") or _contains(
        agents_md, "hybrid-search-mcp:codex-routing"  # legacy marker
    )
    ok = root.is_dir() and claude_wired and codex_wired
    return SmokeCheck(
        "shared-memory-root", ok,
        f"root={'ok' if root.is_dir() else 'missing'} "
        f"claude_md={'ok' if claude_wired else 'missing'} "
        f"agents_md={'ok' if codex_wired else 'missing'}",
    )


def _contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8")
    except OSError:
        return False
