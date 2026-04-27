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
        # v0.3.0: all four hook types ship together.
        for name in ("PreToolUse", "SessionStart", "UserPromptSubmit", "Stop"):
            assert name in written["hooks"], f"missing hook entry: {name}"

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
        # v0.3.0 adds UserPromptSubmit + Stop on top of the refreshed pair.
        assert result["updated"] == 2  # PreToolUse + SessionStart stale paths refreshed
        assert result["added"] == 2    # UserPromptSubmit + Stop freshly installed

        import sys
        rewritten = json.loads(settings.read_text())
        pre_cmd = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        sess_cmd = rewritten["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert sys.executable in pre_cmd
        assert sys.executable in sess_cmd
        # Second run is a no-op once the paths are already current.
        result2 = hooks.install_memory_hook(settings)
        assert result2["status"] == "exists"


class TestExploratoryClassifier:
    """Unit coverage for the UserPromptSubmit exploratory heuristic."""

    @pytest.mark.parametrize(
        "prompt",
        [
            "학원비 정산 어떻게 되나",
            "입학테스트 관련해서 어떤 기능들이 있는지 설명해줘",
            "explain the billing architecture",
            "how does portal-v3 work",
            "지난번에 뭐 물어봤지",
            "ledger writepath 전체 구조",
        ],
    )
    def test_classifies_exploratory_prompts(self, prompt: str) -> None:
        assert hooks._is_exploratory_prompt(prompt)

    @pytest.mark.parametrize(
        "prompt",
        [
            "",
            "   ",
            "ok",
            "thanks",
            "next",
            "/help",
            "/clear",
            "!ls -la",
            "@file.py",
            "fix this typo",
        ],
    )
    def test_classifies_non_exploratory_prompts(self, prompt: str) -> None:
        assert not hooks._is_exploratory_prompt(prompt)


class TestUserPromptSubmitHook:
    """G2 coverage — pre-fetch hybrid_search on exploratory prompts."""

    def test_injects_context_on_exploratory_prompt(
        self, project_root: Path, monkeypatch
    ) -> None:
        """When the prompt looks exploratory, hook calls search and injects top-K."""
        # Stub the programmatic search so the test stays offline.
        class _FakeHit:
            def __init__(self, path, name, start=1, end=5):
                self.file_path = path
                self.name = name
                self.start_line = start
                self.end_line = end
                self.qualified_name = name

        class _FakeResp:
            results = [
                _FakeHit("docs/features/ledger.md", "ledger"),
                _FakeHit("services/ledger/write.ts", "writeLedger"),
            ]

        def fake_search(prompt, cwd):
            return _FakeResp()

        monkeypatch.setattr(hooks, "_run_programmatic_search", fake_search)

        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "ledger 전체 구조 설명해줘",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        assert parsed["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "ledger" in ctx
        assert "docs/features/ledger.md" in ctx

    def test_silent_on_non_exploratory(self, project_root: Path, monkeypatch) -> None:
        called = {"n": 0}

        def fake_search(prompt, cwd):
            called["n"] += 1
            return None

        monkeypatch.setattr(hooks, "_run_programmatic_search", fake_search)
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "/clear",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.run_hook(payload)
        assert buf.getvalue() == ""
        assert called["n"] == 0, "classifier should short-circuit before search"

    def test_silent_on_search_failure(self, project_root: Path, monkeypatch) -> None:
        def boom(prompt, cwd):
            return None

        monkeypatch.setattr(hooks, "_run_programmatic_search", boom)
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "tuition 어떻게 구성돼 있나",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        assert buf.getvalue() == ""

    def test_empty_results_is_silent(self, project_root: Path, monkeypatch) -> None:
        class _Empty:
            results = []

        monkeypatch.setattr(hooks, "_run_programmatic_search", lambda p, c: _Empty())
        payload = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "prompt": "어떻게 구성 되어 있나",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.run_hook(payload)
        assert buf.getvalue() == ""


class TestStopHook:
    """G1 coverage — Stop hook guarantees qa_log save on every turn."""

    def test_saves_turn_from_transcript(self, project_root: Path, tmp_path: Path) -> None:
        """Stop hook parses JSONL transcript and writes a qa_log entry.

        G1 check: a turn that used only Grep/Read (no MCP) still ends up
        in qa_log after Stop fires.
        """
        transcript = tmp_path / "session.jsonl"
        # Minimal realistic transcript — system setup, user turn, assistant
        # with a couple tool_use blocks and final text.
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": "tuition_fees update 어디서 하나"},
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Grep", "input": {}},
                            {"type": "tool_use", "name": "Read", "input": {}},
                            {"type": "text", "text": "9 places under services/..."},
                        ]
                    },
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        event = {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }
        hooks.run_hook(json.dumps(event))

        # Assert a qa_log was written under the project root.
        written = list((project_root / ".hybrid-search" / "qa").rglob("*.md"))
        assert written, "Stop hook should persist a qa_log entry"
        body = written[0].read_text(encoding="utf-8")
        assert "tuition_fees update 어디서 하나" in body
        assert "trigger: stop_hook" in body
        assert "tools_used:" in body
        assert "Grep" in body
        assert "Read" in body
        assert "answer_excerpt_chars:" in body
        assert "## Answer excerpt" in body
        assert "9 places under services/..." in body

    def test_respects_stop_hook_active(self, project_root: Path, tmp_path: Path) -> None:
        """Guard against infinite continuation loops — exit silently when flag set."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "test prompt"},
            }) + "\n",
            encoding="utf-8",
        )
        event = {
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }
        hooks.run_hook(json.dumps(event))
        written = list((project_root / ".hybrid-search" / "qa").rglob("*.md"))
        assert not written

    def test_skips_local_command_stdout(self, project_root: Path, tmp_path: Path) -> None:
        """User messages that are local-command echoes aren't real prompts."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": "<local-command-stdout>ok</local-command-stdout>"},
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Bye"}]},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        event = {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }
        hooks.run_hook(json.dumps(event))
        written = list((project_root / ".hybrid-search" / "qa").rglob("*.md"))
        assert not written

    def test_missing_transcript_is_silent(self, project_root: Path) -> None:
        event = {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": "/nonexistent/path.jsonl",
            "cwd": str(project_root),
        }
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(json.dumps(event))
        assert rc == 0
        assert buf.getvalue() == ""

    def test_dedups_against_recent_mcp_save(self, project_root: Path, tmp_path: Path) -> None:
        """When MCP tool already saved this query < 5s ago, Stop skips."""
        # Simulate the MCP tool having already written a qa file for this query.
        _write_log(project_root, "tuition ledger architecture")

        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": "tuition ledger architecture"},
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        event = {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }
        before = list((project_root / ".hybrid-search" / "qa").rglob("*.md"))
        hooks.run_hook(json.dumps(event))
        after = list((project_root / ".hybrid-search" / "qa").rglob("*.md"))
        assert len(after) == len(before), "dedup should prevent double-save"
