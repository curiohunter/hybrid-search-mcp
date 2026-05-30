"""Automation — Stop hooks fire-and-forget background conversation indexing."""

from __future__ import annotations

from pathlib import Path

from hybrid_search import codex_hooks
from hybrid_search.memory import hook_runtime


class _FakePopen:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return object()


def test_build_conv_index_command() -> None:
    cmd = hook_runtime._build_conv_index_command("/t.jsonl", "/proj", "codex")
    assert "index-conversations" in cmd
    assert "--transcript" in cmd and "/t.jsonl" in cmd
    assert "--cwd" in cmd and "/proj" in cmd
    assert "--source" in cmd and "codex" in cmd


def test_spawn_is_hermetic_under_pytest(monkeypatch) -> None:
    """The suite must never launch a real indexing subprocess."""
    fake = _FakePopen()
    monkeypatch.setattr(hook_runtime.subprocess, "Popen", fake)
    monkeypatch.delenv("HYBRID_SEARCH_CONV_INDEX", raising=False)
    # PYTEST_CURRENT_TEST is set by pytest during the test → guard short-circuits.
    hook_runtime.spawn_conversation_index("/t.jsonl", "/proj", "claude")
    assert fake.calls == []


def test_spawn_respects_toggle_off(monkeypatch) -> None:
    fake = _FakePopen()
    monkeypatch.setattr(hook_runtime.subprocess, "Popen", fake)
    monkeypatch.setenv("HYBRID_SEARCH_CONV_INDEX", "0")
    hook_runtime.spawn_conversation_index("/t.jsonl", "/proj", "claude")
    assert fake.calls == []


def test_spawn_never_raises(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("spawn failed")

    monkeypatch.setattr(hook_runtime.subprocess, "Popen", boom)
    monkeypatch.delenv("HYBRID_SEARCH_CONV_INDEX", raising=False)
    hook_runtime.spawn_conversation_index("/t.jsonl", "/proj", "claude")  # no raise


def test_find_codex_session_file(tmp_path, monkeypatch) -> None:
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "05" / "04"
    sessions.mkdir(parents=True)
    sid = "019df2ab-cf3d-7050-83b8-e50d2c47054a"
    target = sessions / f"rollout-2026-05-04T20-07-19-{sid}.jsonl"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert codex_hooks._find_codex_session_file(sid) == target
    assert codex_hooks._find_codex_session_file("nonexistent") is None
    assert codex_hooks._find_codex_session_file(None) is None
