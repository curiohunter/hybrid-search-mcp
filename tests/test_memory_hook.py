"""Tests for the Claude Code memory-injection hook (hybrid_search.hooks)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hybrid_search import hooks
from hybrid_search.memory import qa_log


def _write_log(project_root: Path, query: str) -> Path:
    """Populate one qa_log entry by driving the real writer synchronously."""
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
    prev = os.environ.get(qa_log.ENV_TOGGLE)
    os.environ[qa_log.ENV_TOGGLE] = "1"
    try:
        path = qa_log.record(
            query=query, response=resp, cwd=str(project_root), async_write=False
        )
    finally:
        if prev is None:
            os.environ.pop(qa_log.ENV_TOGGLE, None)
        else:
            os.environ[qa_log.ENV_TOGGLE] = prev
    assert path is not None
    return path


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".hybrid-search").mkdir()
    return tmp_path


class TestExtractTerm:
    def test_grep_pattern(self) -> None:
        term = hooks._extract_term({
            "tool_name": "Grep",
            "tool_input": {"pattern": "parseConfig"},
        })
        assert term == "parseConfig"

    def test_grep_empty_pattern_is_none(self) -> None:
        assert hooks._extract_term({
            "tool_name": "Grep",
            "tool_input": {"pattern": ""},
        }) is None

    def test_read_filename(self) -> None:
        term = hooks._extract_term({
            "tool_name": "Read",
            "tool_input": {"file_path": "/a/b/c/config.py"},
        })
        assert term == "config.py"

    def test_unknown_tool_returns_none(self) -> None:
        assert hooks._extract_term({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/x"},
        }) is None


class TestLooksLikeNoise:
    @pytest.mark.parametrize("term", ["", "  ", "*", ".", "=", "ab", "foo", "test"])
    def test_noise_patterns(self, term: str) -> None:
        assert hooks._looks_like_noise(term)

    @pytest.mark.parametrize("term", ["parseConfig", "admission_results", "수강료"])
    def test_real_patterns_pass(self, term: str) -> None:
        assert not hooks._looks_like_noise(term)


class TestRunHook:
    def test_pretooluse_grep_injects_context(self, project_root: Path) -> None:
        _write_log(project_root, "how does parseConfig work")
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "parseConfig"},
            "cwd": str(project_root),
        })
        import io
        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        out = buf.getvalue()
        assert out, "expected JSON on stdout"
        parsed = json.loads(out)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "parseConfig" in parsed["hookSpecificOutput"]["additionalContext"]

    def test_pretooluse_no_matches_is_silent(self, project_root: Path) -> None:
        _write_log(project_root, "something totally unrelated")
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "nonexistentterm"},
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        assert buf.getvalue() == ""

    def test_pretooluse_noise_pattern_is_silent(self, project_root: Path) -> None:
        _write_log(project_root, "useful content here")
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "."},
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        assert buf.getvalue() == ""

    def test_session_start_injects_summary(self, project_root: Path) -> None:
        _write_log(project_root, "first question about things")
        _write_log(project_root, "second question about stuff")
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        out = buf.getvalue()
        parsed = json.loads(out)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "first question" in ctx or "second question" in ctx

    def test_session_start_empty_project_is_silent(self, project_root: Path) -> None:
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        assert buf.getvalue() == ""

    def test_malformed_json_is_silent(self) -> None:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook("not json")
        assert rc == 0
        assert buf.getvalue() == ""

    def test_missing_cwd_is_silent(self) -> None:
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "parseConfig"},
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        assert buf.getvalue() == ""

    def test_context_cap_respected(self, project_root: Path) -> None:
        # Plant many qa_log entries with the same term to potentially exceed cap
        for i in range(20):
            _write_log(project_root, f"parseConfig investigation #{i} with long query text " * 3)
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "parseConfig"},
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.run_hook(payload)
        parsed = json.loads(buf.getvalue())
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert len(ctx) <= hooks._MAX_CONTEXT_CHARS


class TestInstallMemoryHook:
    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        result = hooks.install_memory_hook(settings)
        assert result["status"] == "wrote"
        assert result["added"] > 0
        assert settings.exists()
        written = json.loads(settings.read_text())
        assert "hooks" in written
        assert "PreToolUse" in written["hooks"]
        assert "SessionStart" in written["hooks"]

    def test_merges_without_clobbering_existing_hooks(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        pre_existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": "echo 'existing'"}],
                    }
                ]
            }
        }
        settings.write_text(json.dumps(pre_existing))
        result = hooks.install_memory_hook(settings)
        assert result["status"] == "wrote"
        merged = json.loads(settings.read_text())
        pre_hooks = merged["hooks"]["PreToolUse"]
        # Both existing Edit|Write entry and the new Grep|Read entry should coexist
        assert len(pre_hooks) == 2
        matchers = {e.get("matcher") for e in pre_hooks}
        assert "Edit|Write" in matchers
        assert "Grep|Read" in matchers

    def test_idempotent_second_install_noop(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        hooks.install_memory_hook(settings)
        result = hooks.install_memory_hook(settings)
        assert result["status"] == "exists"
        assert result["added"] == 0

    def test_dry_run_does_not_touch_file(self, tmp_path: Path) -> None:
        settings = tmp_path / ".claude" / "settings.json"
        result = hooks.install_memory_hook(settings, dry_run=True)
        assert result["status"] == "dry-run"
        assert not settings.exists()

    def test_embeds_current_python_path(self, tmp_path: Path) -> None:
        import sys
        settings = tmp_path / ".claude" / "settings.json"
        hooks.install_memory_hook(settings)
        written = json.loads(settings.read_text())
        cmd = written["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert sys.executable in cmd, (
            "hook command must embed the current interpreter so it works even "
            "when the user's login-shell PATH doesn't include the venv"
        )

    def test_refreshes_stale_python_path(self, tmp_path: Path) -> None:
        """Previously-installed hook with a different python path is rewritten in place."""
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir()
        stale = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Grep|Read",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python -m hybrid_search.cli qa-hook 2>/dev/null || true",
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
                                "command": "python -m hybrid_search.cli qa-hook 2>/dev/null || true",
                                "timeout": 5,
                            }
                        ],
                    }
                ],
            }
        }
        settings.write_text(json.dumps(stale))
        result = hooks.install_memory_hook(settings)
        assert result["status"] == "wrote"
        assert result["added"] == 0
        assert result["updated"] == 2

        import sys
        rewritten = json.loads(settings.read_text())
        pre_cmd = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        sess_cmd = rewritten["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert sys.executable in pre_cmd
        assert sys.executable in sess_cmd
        # Second run is a no-op once the paths are already current.
        result2 = hooks.install_memory_hook(settings)
        assert result2["status"] == "exists"
