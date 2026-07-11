from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from hybrid_search import codex_hooks
from hybrid_search.memory import hook_runtime, qa_log


def _mark_project(root: Path) -> None:
    (root / ".git").mkdir(exist_ok=True)


def _run(payload: dict) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = codex_hooks.run_hook(json.dumps(payload))
    assert rc == 0
    out = buf.getvalue()
    assert out
    return json.loads(out)


def _write_log(project_root: Path, query: str) -> None:
    (project_root / ".hybrid-search").mkdir(exist_ok=True)
    resp = SimpleNamespace(
        results=[
            SimpleNamespace(
                chunk_id="c1",
                file_path="a.py",
                project="p",
                name="f",
                qualified_name="a.f",
                node_type="function",
                start_line=1,
                end_line=3,
                snippet="hello world",
            )
        ],
        query_type="NL_EN",
        effective_bm25_weight=0.4,
        query_time_ms=10.0,
        total_chunks_searched=100,
    )
    assert qa_log.record(query=query, response=resp, cwd=str(project_root), async_write=False)


def test_codex_hook_session_start_injects_recent_memory(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    _write_log(tmp_path, "how does parseConfig work")
    out = _run({
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(tmp_path),
    })
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "past turns available" in ctx
    assert "parseConfig" in ctx
    assert len(ctx) <= 360
    # ToolSearch is a Claude Code facility; the codex context must not mention it.
    assert "ToolSearch" not in ctx


def test_codex_hook_session_start_skips_clear_source(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    _write_log(tmp_path, "how does parseConfig work")
    assert _run({"hook_event_name": "SessionStart", "source": "clear", "cwd": str(tmp_path)}) == {}


def test_codex_hook_user_prompt_submit_injects_context(tmp_path: Path, monkeypatch) -> None:
    _mark_project(tmp_path)
    def fake_context(project_root: Path, prompt: str, *, record_prefetch: bool = False) -> str:
        assert project_root == tmp_path.resolve()
        assert "architecture" in prompt
        assert record_prefetch is False
        return "memory ctx"

    monkeypatch.setattr(hook_runtime, "build_user_prompt_context", fake_context)
    out = _run({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "explain the architecture",
        "cwd": str(tmp_path),
        "turn_id": "t1",
    })
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert out["hookSpecificOutput"]["additionalContext"] == "memory ctx"
    pending = tmp_path / ".hybrid-search" / "runtime" / "codex-last-prompt.json"
    payload = json.loads(pending.read_text(encoding="utf-8"))
    assert payload["prompt"] == "explain the architecture"


def test_codex_hook_user_prompt_submit_skips_precision_prompt(tmp_path: Path, monkeypatch) -> None:
    _mark_project(tmp_path)
    called = False

    def fake_context(*args, **kwargs) -> str:
        nonlocal called
        called = True
        return "nope"

    monkeypatch.setattr(hook_runtime, "build_user_prompt_context", fake_context)
    assert _run({"hook_event_name": "UserPromptSubmit", "prompt": "/status", "cwd": str(tmp_path)}) == {}
    assert called is False


def test_codex_hook_stop_records_turn_from_pending_prompt(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    _run({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "explain payment flow",
        "cwd": str(tmp_path),
        "turn_id": "turn-1",
    })
    out = _run({
        "hook_event_name": "Stop",
        "cwd": str(tmp_path),
        "turn_id": "turn-1",
        "stop_hook_active": False,
        "last_assistant_message": "Here is the answer.",
    })
    assert out == {}
    written = list((tmp_path / ".hybrid-search" / "qa").rglob("*.md"))
    assert written
    body = written[0].read_text(encoding="utf-8")
    assert "explain payment flow" in body
    assert "trigger: codex_stop_hook" in body
    assert "client: codex" in body
    assert "answer_excerpt_chars:" in body
    assert "## Answer excerpt" in body
    assert "Here is the answer." in body


def test_codex_hook_stop_noops_without_answer(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    _run({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "explain payment flow",
        "cwd": str(tmp_path),
        "turn_id": "turn-1",
    })
    assert _run({
        "hook_event_name": "Stop",
        "cwd": str(tmp_path),
        "turn_id": "turn-1",
        "stop_hook_active": False,
        "last_assistant_message": None,
    }) == {}
    assert not list((tmp_path / ".hybrid-search" / "qa").rglob("*.md"))


def test_codex_hook_stop_noop_prints_valid_json(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    assert _run({"hook_event_name": "Stop", "cwd": str(tmp_path)}) == {}


def test_codex_hook_stop_is_only_qa_log_writer(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    _run({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "explain payment flow",
        "cwd": str(tmp_path),
        "turn_id": "turn-1",
    })
    assert not list((tmp_path / ".hybrid-search" / "qa").rglob("*.md"))


def test_codex_pending_prompt_write_is_atomic(tmp_path: Path) -> None:
    _mark_project(tmp_path)
    _run({
        "hook_event_name": "UserPromptSubmit",
        "prompt": "explain payment flow",
        "cwd": str(tmp_path),
        "turn_id": "turn-1",
    })
    runtime_dir = tmp_path / ".hybrid-search" / "runtime"
    assert (runtime_dir / "codex-last-prompt.json").exists()
    assert not (runtime_dir / "codex-last-prompt.json.tmp").exists()


def test_codex_hook_never_blocks_on_bad_json() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert codex_hooks.run_hook("{") == 0
    assert json.loads(buf.getvalue()) == {}


def test_install_codex_hook_writes_hooks_json(tmp_path: Path) -> None:
    result = codex_hooks.install_codex_hook(tmp_path)
    assert result["status"] == "wrote"
    hooks_path = tmp_path / ".codex" / "hooks.json"
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "SessionStart" in payload["hooks"]
    assert "UserPromptSubmit" in payload["hooks"]
    assert "Stop" in payload["hooks"]


def test_install_codex_hook_enables_feature_flag(tmp_path: Path) -> None:
    codex_hooks.install_codex_hook(tmp_path)
    text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[features]" in text
    assert "hooks = true" in text
    assert "codex_hooks" not in text


def test_install_codex_hook_migrates_deprecated_feature_flag(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("[features]\ncodex_hooks = true\n", encoding="utf-8")
    codex_hooks.install_codex_hook(tmp_path)
    text = config.read_text(encoding="utf-8")
    assert "hooks = true" in text
    assert "codex_hooks" not in text


def test_install_codex_hook_writes_toml_mcp_server(tmp_path: Path) -> None:
    codex_hooks.install_codex_hook(tmp_path)
    text = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.hybrid-search]" in text
    assert 'args = ["-m", "hybrid_search.cli", "serve"]' in text


def test_install_codex_hook_preserves_existing_config(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5.3-codex"\n', encoding="utf-8")
    codex_hooks.install_codex_hook(tmp_path)
    text = config.read_text(encoding="utf-8")
    assert 'model = "gpt-5.3-codex"' in text


def test_install_codex_hook_is_idempotent(tmp_path: Path) -> None:
    first = codex_hooks.install_codex_hook(tmp_path)
    second = codex_hooks.install_codex_hook(tmp_path)
    assert first["status"] == "wrote"
    assert second["status"] == "exists"


def test_install_codex_hook_does_not_duplicate_existing_hybrid_search_hooks(tmp_path: Path) -> None:
    codex_hooks.install_codex_hook(tmp_path)
    codex_hooks.install_codex_hook(tmp_path)
    payload = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        matches = [
            h
            for group in payload["hooks"][event]
            for h in group.get("hooks", [])
            if "hybrid_search.cli codex-hook" in h.get("command", "")
        ]
        assert len(matches) == 1
