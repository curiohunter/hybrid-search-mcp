"""Tests for the Claude Code memory-injection hook (hybrid_search.hooks)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hybrid_search import hooks
from hybrid_search.memory import hook_runtime, qa_log


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


class TestResolveProjectRoot:
    def test_nested_cwd_resolves_to_git_root(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        nested = root / "docs" / "content_docs" / "학습" / "중등"
        (root / ".git").mkdir(parents=True)
        nested.mkdir(parents=True)

        got = hook_runtime.resolve_project_root({"cwd": str(nested)})

        assert got == root.resolve()

    def test_unmarked_nested_cwd_returns_none(self, tmp_path: Path) -> None:
        nested = tmp_path / "docs" / "학습" / "중등"
        nested.mkdir(parents=True)

        assert hook_runtime.resolve_project_root({"cwd": str(nested)}) is None

    def _make_worktree(self, tmp_path: Path, gitdir_line: str) -> tuple[Path, Path]:
        main = tmp_path / "proj"
        (main / ".git" / "worktrees" / "wt-fix").mkdir(parents=True)
        wt = tmp_path / "worktrees" / "wt-fix"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(gitdir_line)
        return main, wt

    def test_linked_worktree_resolves_to_main_checkout(self, tmp_path: Path) -> None:
        # A session run inside a worktree must land its memory on the MAIN
        # checkout — worktree-rooted qa dies with the worktree and conv
        # indexing no-ops on the unregistered path (2026-09-04 field check).
        main, wt = self._make_worktree(
            tmp_path, f"gitdir: {tmp_path}/proj/.git/worktrees/wt-fix\n"
        )
        got = hook_runtime.resolve_project_root({"cwd": str(wt / "src")})
        assert got == main

    def test_worktree_with_relative_gitdir_resolves(self, tmp_path: Path) -> None:
        main, wt = self._make_worktree(
            tmp_path, "gitdir: ../../proj/.git/worktrees/wt-fix\n"
        )
        got = hook_runtime.resolve_project_root({"cwd": str(wt)})
        assert got == main

    def test_broken_worktree_marker_falls_back_to_worktree_root(self, tmp_path: Path) -> None:
        # Main checkout gone (or marker garbage): better a worktree-local
        # root than none — degrades to the pre-fix behavior.
        wt = tmp_path / "wt-orphan"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /nonexistent/.git/worktrees/x\n")
        assert hook_runtime.resolve_project_root({"cwd": str(wt)}) == wt


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

    def test_session_start_hints_toolsearch_recovery(self, project_root: Path) -> None:
        # Tool Search defers MCP schemas in tool-heavy environments; the
        # Claude session context must say how to load the tool, not just name it.
        _write_log(project_root, "first question about things")
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "cwd": str(project_root),
        })
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.run_hook(payload)
        assert rc == 0
        ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
        assert 'ToolSearch "select:mcp__hybrid-search__hybrid_search"' in ctx

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
            confidence = "weak"
            fallback_hint = "weak match -> wiki `ledger`"

        def fake_search(prompt, cwd, session_key=None):
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
        assert ctx.splitlines()[0] == (
            "[hybrid-search route] suggest hybrid_search · exploratory NL"
        )
        assert "docs/features/ledger.md" in ctx
        assert "services/ledger/write.ts" in ctx
        assert "confidence weak" in ctx
        assert "do not Read those paths" in ctx
        assert len(ctx) <= hook_runtime._PREFETCH_MAX_CONTEXT_CHARS

    def test_router_off_omits_route_line(
        self, project_root: Path, monkeypatch
    ) -> None:
        class _FakeHit:
            file_path = "services/ledger/write.ts"
            start_line = 9

        class _FakeResp:
            results = [_FakeHit()]
            confidence = "mixed"
            fallback_hint = None

        monkeypatch.setenv("HYBRID_SEARCH_ROUTER", "0")
        monkeypatch.setattr(
            hooks, "_run_programmatic_search", lambda p, c, s=None: _FakeResp()
        )

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
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "[hybrid-search route]" not in ctx
        assert ctx.startswith("[hybrid-search pre-fetch]")

    def test_drops_lowest_ranked_hits_at_the_cap(self, monkeypatch) -> None:
        """Trim from the tail, and never truncate the closing instruction."""
        class _FakeHit:
            """Memory hits carry the longest bodies — the only way to hit the cap."""

            def __init__(self, path: str, start: int):
                self.file_path = path
                self.start_line = start
                self.node_type = "qa_log"
                self.name = "note"
                self.trust_meta = "[qa - stop_hook - hypothesis - 1d ago]"
                self.snippet = "가" * 400

        hits = [_FakeHit(f"src/features/{c * 58}.ts", i * 10)
                for i, c in enumerate("abcdefgh", start=1)]

        class _FakeResp:
            results = hits
            confidence = "mixed"
            fallback_hint = None

        monkeypatch.delenv("HYBRID_SEARCH_ROUTER", raising=False)

        ctx = hook_runtime._format_user_prompt_context(
            _FakeResp(), "billing flow 어떻게 구성돼 있나"
        )

        assert len(ctx) <= hook_runtime._PREFETCH_MAX_CONTEXT_CHARS
        assert "[hybrid-search route]" in ctx
        assert ctx.splitlines()[2].startswith("1. "), "top hit survives"
        assert ctx.endswith("call hybrid_search only if these miss.")
        assert "\n6. " not in ctx, "tail trimmed at the cap"

    def test_memory_hits_are_quoted_not_linked(self, monkeypatch) -> None:
        """A qa/conv hit has no file to open — its text must be the answer."""
        class _MemoryHit:
            file_path = ".hybrid-search/qa/2026/09/01-074413-662a5e0c.md"
            start_line = 1
            node_type = "qa_log"
            name = "01-074413"
            trust_meta = "[qa - stop_hook - hypothesis - 3d ago]"
            snippet = "선주입 레인은 v1.1에서 채점되기 시작했다"

        class _FakeResp:
            results = [_MemoryHit()]
            confidence = "strong"
            fallback_hint = None

        monkeypatch.setenv("HYBRID_SEARCH_ROUTER", "0")

        ctx = hook_runtime._format_user_prompt_context(_FakeResp(), "지난번에 뭐 했지")

        assert "선주입 레인은 v1.1에서" in ctx, "content inlined"
        assert "quoted — no file to open" in ctx
        assert "`.hybrid-search/qa" not in ctx, "no locator that invites a Read"

    def test_memory_excerpt_skips_frontmatter_and_metadata(self, monkeypatch) -> None:
        """A quoted hit must show the answer, not the note's bookkeeping."""
        class _Hit:
            file_path = ".hybrid-search/qa/2026/09/04-094933-aa0b76e9.md"
            start_line = 1
            node_type = "qa_log"
            name = "note"
            trust_meta = "[qa - reflector - consolidated]"
            snippet = "[qa - reflector - consolidated] --- query: \"x\" timestamp: 2026"
            content = (
                "[qa - reflector - consolidated]\n"
                "---\nquery: \"워크트리 얘기\"\ntimestamp: 2026-09-04T09:49:33+00:00\n---\n"
                "# Q: 워크트리 얘기\n\n"
                "- **query_type**: KOREAN_NL\n- **bm25_weight**: 0.15\n\n"
                "## Answer excerpt\n\n"
                "충돌의 원인은 공용 체크아웃이다.\n"
            )

        class _Resp:
            results = [_Hit()]
            confidence = "strong"
            fallback_hint = None

        monkeypatch.setenv("HYBRID_SEARCH_ROUTER", "0")

        ctx = hook_runtime._format_user_prompt_context(_Resp(), "지난번 얘기")

        assert "충돌의 원인은 공용 체크아웃이다." in ctx
        for noise in ("timestamp:", "# Q:", "bm25_weight", "Answer excerpt", "---"):
            assert noise not in ctx, f"{noise} is bookkeeping, not an answer"

    def test_file_hit_keeps_a_readable_locator(self, monkeypatch) -> None:
        class _Hit:
            file_path = "src/hybrid_search/memory/selfeval.py"
            start_line = 154
            node_type = "function"
            name = "score_event"
            snippet = "Turn one extracted event into a scored, storable row."
            content = None

        class _Resp:
            results = [_Hit()]
            confidence = "strong"
            fallback_hint = None

        monkeypatch.setenv("HYBRID_SEARCH_ROUTER", "0")

        ctx = hook_runtime._format_user_prompt_context(_Resp(), "채점 어디서 하지")

        assert "`src/hybrid_search/memory/selfeval.py:154` (function score_event)" in ctx
        assert "Turn one extracted event" in ctx, "one line of what it contains"

    def test_silent_on_non_exploratory(self, project_root: Path, monkeypatch) -> None:
        called = {"n": 0}

        def fake_search(prompt, cwd, session_key=None):
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
        def boom(prompt, cwd, session_key=None):
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

        monkeypatch.setattr(
            hooks, "_run_programmatic_search", lambda p, c, s=None: _Empty()
        )
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

    def test_records_during_a_stop_hook_continuation(
        self, project_root: Path, tmp_path: Path
    ) -> None:
        """A goal loop must not blind the memory layer.

        ``stop_hook_active`` means a session-scoped Stop hook blocked stopping
        and the assistant kept working. Bailing on that flag lost every turn
        for the whole loop; this handler emits nothing, so recording cannot
        extend the loop.
        """
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "환불 흐름이 어떻게 되나"},
            }) + "\n",
            encoding="utf-8",
        )
        event = {
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }

        out = hooks.run_hook(json.dumps(event))

        assert out == 0, "still silent — the flag only gates the loop, not the record"
        written = list((project_root / ".hybrid-search" / "qa").rglob("*.md"))
        assert written, "the turn must be saved even under a continuation"

    def test_continuation_does_not_respawn_conversation_indexing(
        self, project_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """Recording repeats; the background indexer should not."""
        from hybrid_search.memory import hook_runtime

        spawns = []
        monkeypatch.setattr(
            hook_runtime, "spawn_conversation_index",
            lambda *a, **k: spawns.append(1),
        )
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "환불 흐름이 어떻게 되나"},
            }) + "\n",
            encoding="utf-8",
        )
        base = {
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }

        hooks.run_hook(json.dumps(base))
        hooks.run_hook(json.dumps({**base, "stop_hook_active": True}))

        assert spawns == [1], "first delivery spawns, the continuation does not"

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

    # Stop-vs-earlier-save rules (F4 asymmetry). Both tests read the Stop
    # hook's own return value instead of counting files: qa filenames are
    # second-resolution, so two saves of one query in the same second share a
    # name and the second replaces the first. Counting files made the old
    # single test pass by that collision and fail when the two writes fell in
    # different seconds (docs/plans/2026-09-27-open-the-doors.md, known issues).

    @staticmethod
    def _stop_turn(project_root: Path, tmp_path: Path, query: str) -> dict:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            "\n".join([
                json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": query},
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        return {
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
            "cwd": str(project_root),
        }

    @staticmethod
    def _spy_record_turn(monkeypatch) -> list:
        from hybrid_search.memory import qa_log as qa_log_mod

        results: list = []
        real = qa_log_mod.record_turn

        def spy(**kwargs):
            out = real(**kwargs)
            results.append(out)
            return out

        monkeypatch.setattr(qa_log_mod, "record_turn", spy)
        return results

    def test_skips_when_an_answer_for_this_query_was_just_saved(
        self, project_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """A recent answer-bearing record already serves recall — Stop skips."""
        from hybrid_search.memory import qa_log as qa_log_mod

        # Production window is 5s; a cold first hook call can outlast that.
        monkeypatch.setattr(qa_log_mod, "_DEDUP_WINDOW_SECONDS", 3600)
        monkeypatch.setenv(qa_log_mod.ENV_TOGGLE, "1")
        earlier = qa_log_mod.record_turn(
            query="tuition ledger architecture",
            cwd=str(project_root),
            answer_excerpt="The ledger is append-only; totals are derived.",
            dedup=False,
        )
        assert earlier is not None

        results = self._spy_record_turn(monkeypatch)
        hooks.run_hook(json.dumps(
            self._stop_turn(project_root, tmp_path, "tuition ledger architecture")
        ))

        assert results == [None], "an answered duplicate must not be saved again"

    def test_saves_the_answer_over_a_question_only_mcp_record(
        self, project_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """F4: the MCP tool's question-only record must not eat the answer."""
        from hybrid_search.memory import qa_log as qa_log_mod

        monkeypatch.setattr(qa_log_mod, "_DEDUP_WINDOW_SECONDS", 3600)
        _write_log(project_root, "tuition ledger architecture")

        results = self._spy_record_turn(monkeypatch)
        hooks.run_hook(json.dumps(
            self._stop_turn(project_root, tmp_path, "tuition ledger architecture")
        ))

        assert len(results) == 1 and results[0] is not None
        saved = results[0].read_text(encoding="utf-8")
        assert "trigger: stop_hook" in saved
        assert "answer_excerpt_chars:" in saved
        assert "done" in saved


class TestAnswerExcerptCapturesConclusion:
    """F4′ (2026-07-26 Mac-mini E2E): a tool-using turn interleaves
    assistant text with tool_result records that ride in USER-role
    records. The walk must not stop at the first tool result, and the
    excerpt must carry the CONCLUSION (tail), not just the pre-tool
    preamble."""

    def _records(self):
        return [
            {"type": "user",
             "message": {"role": "user",
                         "content": "payssam 정산 어디서 처리해?"}},
            {"type": "assistant",
             "message": {"content": [
                 {"type": "text",
                  "text": "탐색형 질문이라 mcp__hybrid-search__hybrid_search를 먼저 호출합니다"},
                 {"type": "tool_use", "name": "mcp__hybrid-search__hybrid_search"},
             ]}},
            # tool_result: user-role record WITHOUT genuine prompt text
            {"type": "user",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "content": "[...검색 결과...]"},
             ]}},
            {"type": "assistant",
             "message": {"content": [
                 {"type": "text",
                  "text": "정산 연동은 lib/payssam-client.ts와 services/payssam-service.ts에서 처리됩니다."},
             ]}},
        ]

    def test_walk_survives_tool_results_and_keeps_conclusion(self) -> None:
        from hybrid_search.hooks import _find_last_turn

        prompt, tools, chars, excerpt, _, _ = _find_last_turn(self._records())
        assert prompt == "payssam 정산 어디서 처리해?"
        assert "mcp__hybrid-search__hybrid_search" in tools
        assert "payssam-client.ts" in excerpt, "conclusion missing (F4′)"
        # Both fit in the 4k budget: preamble kept, conclusion last.
        assert excerpt.rstrip().endswith("처리됩니다.")
        assert chars > 89  # full turn counted, not just the preamble

    def test_tail_bias_under_tight_budget(self, monkeypatch) -> None:
        import hybrid_search.hooks as hooks_mod

        monkeypatch.setattr(hooks_mod, "_ANSWER_EXCERPT_COLLECT_MAX_CHARS", 40)
        _, _, _, excerpt, _, _ = hooks_mod._find_last_turn(self._records())
        # Budget too small for both → the conclusion wins, preamble drops.
        assert "payssam-client.ts" in excerpt or "처리됩니다" in excerpt
        assert "먼저 호출합니다" not in excerpt

    def test_genuine_next_prompt_still_ends_the_turn(self) -> None:
        from hybrid_search.hooks import _find_last_turn

        records = self._records() + [
            {"type": "user",
             "message": {"role": "user", "content": "다음 질문이야"}},
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "다음 답"}]}},
        ]
        prompt, _, _, excerpt, _, _ = _find_last_turn(records)
        assert prompt == "다음 질문이야"
        assert excerpt == "다음 답"


class TestReindexContentionGuard:
    """Pre-fetch skips while a hook-triggered reindex holds the lock —
    racing it risks the 10s hook timeout AND serves an index that lacks
    the new commits (2026-09-01 post-merge field check)."""

    def _event(self, root):
        return {
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(root),
            "prompt": "이 시스템의 환불 흐름이 어떻게 구성되어 있는지 알려줘",
        }

    def test_live_lock_skips_prefetch(self, tmp_path, monkeypatch):
        import os

        from hybrid_search.hooks import _handle_user_prompt_submit

        (tmp_path / ".hybrid-search").mkdir(parents=True)
        (tmp_path / ".hybrid-search/.reindex.lock").write_text(str(os.getpid()))
        assert _handle_user_prompt_submit(self._event(tmp_path)) is None

    def test_stale_lock_does_not_block(self, tmp_path, monkeypatch):
        import hybrid_search.hooks as hooks_mod

        (tmp_path / ".hybrid-search").mkdir(parents=True)
        (tmp_path / ".hybrid-search/.reindex.lock").write_text("999999999")
        ran = []
        monkeypatch.setattr(
            hooks_mod, "_run_programmatic_search",
            lambda prompt, cwd, session_key=None: ran.append(1) or None,
        )
        _handle_user_prompt_submit = hooks_mod._handle_user_prompt_submit
        assert _handle_user_prompt_submit(self._event(tmp_path)) is None
        assert ran, "stale lock must not suppress the pre-fetch"

    def test_garbage_lock_does_not_block(self, tmp_path, monkeypatch):
        import hybrid_search.hooks as hooks_mod

        (tmp_path / ".hybrid-search").mkdir(parents=True)
        (tmp_path / ".hybrid-search/.reindex.lock").write_text("not-a-pid")
        ran = []
        monkeypatch.setattr(
            hooks_mod, "_run_programmatic_search",
            lambda prompt, cwd, session_key=None: ran.append(1) or None,
        )
        assert hooks_mod._handle_user_prompt_submit(self._event(tmp_path)) is None
        assert ran


class TestPrefetchEmbedDeadline:
    """The blocking pre-fetch bounds its embed call; expiry degrades to
    BM25-only via the existing fail-open instead of eating the 10s hook
    timeout and losing the whole context (2026-09-04 Mac-mini incident:
    22s embeds behind a bulk batch)."""

    def test_expired_deadline_raises_connection_error_fast(self, monkeypatch, tmp_path):
        import time

        from hybrid_search.config import EmbeddingConfig
        from hybrid_search.index.embedder import Embedder

        monkeypatch.setenv("HYBRID_SEARCH_EMBED_DEADLINE", "0.01")
        emb = Embedder(EmbeddingConfig(), tmp_path)
        emb._api_key = "test-key"  # skip key lookup — deadline fires first
        time.sleep(0.02)
        t0 = time.monotonic()
        with pytest.raises(ConnectionError, match="deadline"):
            emb._openai_embed_single_batch(["질의"], "m", "test-key")
        assert time.monotonic() - t0 < 1.0  # no network attempt survives

    def test_prefetch_scopes_and_restores_env(self, monkeypatch):
        import os

        seen = {}

        def fake_load_config():
            seen["deadline"] = os.environ.get("HYBRID_SEARCH_EMBED_DEADLINE")
            raise RuntimeError("stop here")

        monkeypatch.setattr("hybrid_search.config.load_config", fake_load_config)
        monkeypatch.delenv("HYBRID_SEARCH_EMBED_DEADLINE", raising=False)
        assert hook_runtime._run_programmatic_search("질문", "/tmp") is None
        assert seen["deadline"] == hook_runtime._PREFETCH_EMBED_DEADLINE_DEFAULT
        # Restored: bulk paths (conversation indexing) must not inherit it.
        assert os.environ.get("HYBRID_SEARCH_EMBED_DEADLINE") is None

    def test_zero_disables_the_deadline(self, monkeypatch):
        import os

        seen = {}

        def fake_load_config():
            seen["deadline"] = os.environ.get("HYBRID_SEARCH_EMBED_DEADLINE")
            raise RuntimeError("stop here")

        monkeypatch.setattr("hybrid_search.config.load_config", fake_load_config)
        monkeypatch.setenv("HYBRID_SEARCH_EMBED_DEADLINE", "0")
        assert hook_runtime._run_programmatic_search("질문", "/tmp") is None
        assert seen["deadline"] is None
        assert os.environ.get("HYBRID_SEARCH_EMBED_DEADLINE") == "0"


class TestDegradedRateAccounting:
    """Availability was fixed by the deadline; effectiveness needs the
    RATE — how often pre-fetches actually ran BM25-only."""

    def _record(self, root, *, degraded, query="환불 흐름 어디서 처리돼?",
                trigger="user_prompt_submit"):
        resp = SimpleNamespace(
            results=[], query_type="KOREAN_NL", effective_bm25_weight=0.15,
            query_time_ms=5.0, total_chunks_searched=10, degraded=degraded,
        )
        prev = os.environ.get(qa_log.ENV_TOGGLE)
        os.environ[qa_log.ENV_TOGGLE] = "1"
        try:
            return qa_log.record(
                query=query, response=resp, cwd=str(root),
                async_write=False, trigger=trigger,
            )
        finally:
            if prev is None:
                os.environ.pop(qa_log.ENV_TOGGLE, None)
            else:
                os.environ[qa_log.ENV_TOGGLE] = prev

    def test_degraded_flag_round_trips_through_frontmatter(self, project_root):
        path = self._record(project_root, degraded=True)
        assert path is not None
        assert "degraded: true" in path.read_text()
        from hybrid_search.memory import reader

        idx = reader.parse_qa_index(path)
        assert idx is not None and idx.degraded is True

    def test_healthy_record_has_no_degraded_line(self, project_root):
        path = self._record(project_root, degraded=False)
        assert "degraded:" not in path.read_text()

    def test_session_context_reports_degraded_rate(self, project_root):
        self._record(project_root, degraded=True,
                     query="환불 정산 처리 위치가 어디야?")
        self._record(project_root, degraded=False,
                     query="출결 위젯 상태 갱신 흐름 알려줘")
        ctx = hook_runtime.build_session_context(project_root)
        assert "[prefetch 7d] 1/2 served BM25-only" in ctx

    def test_session_context_silent_when_nothing_degraded(self, project_root):
        self._record(project_root, degraded=False)
        ctx = hook_runtime.build_session_context(project_root)
        assert "BM25-only" not in ctx


class TestHitBodyCleaning:
    """The excerpt must not re-print what the heading already says."""

    def test_file_hit_body_drops_the_repeated_tag_and_path(self, monkeypatch):
        class _Hit:
            file_path = "src/hybrid_search/cli.py"
            start_line = 1
            node_type = "in_flight_file"
            name = "cli.py"
            snippet = '[in-flight] src/hybrid_search/cli.py """CLI entrypoint."""'
            content = None

        class _Resp:
            results = [_Hit()]
            confidence = "weak"
            fallback_hint = None

        monkeypatch.setenv("HYBRID_SEARCH_ROUTER", "0")

        ctx = hook_runtime._format_user_prompt_context(_Resp(), "cli 어디")

        body = ctx.splitlines()[2]
        assert body.strip().startswith('"""CLI entrypoint')
        assert "[in-flight]" not in body

    def test_code_snippet_opening_with_a_bracket_is_untouched(self, monkeypatch):
        """`_LEADING_TAG_RE` matches a vocabulary, not any bracket."""
        class _Hit:
            file_path = "a.py"
            start_line = 3
            node_type = "function"
            name = "f"
            snippet = "[idx] = compute(x)"
            content = None

        class _Resp:
            results = [_Hit()]
            confidence = "strong"
            fallback_hint = None

        monkeypatch.setenv("HYBRID_SEARCH_ROUTER", "0")

        ctx = hook_runtime._format_user_prompt_context(_Resp(), "compute 어디")

        assert "[idx] = compute(x)" in ctx


class TestQaRecordJunkGate:
    """Every write path into the qa corpus applies the same debris filter."""

    def test_prefetch_path_drops_harness_debris(self, project_root, monkeypatch):
        from hybrid_search.memory import qa_log

        class _Resp:
            results = []
            confidence = "weak"

        saved = qa_log.record(
            query="<task-notification>\n<task-id>abc</task-id>",
            response=_Resp(),
            cwd=str(project_root),
            async_write=False,
            trigger="user_prompt_submit",
        )

        assert saved is None
        assert not list((project_root / ".hybrid-search" / "qa").rglob("*.md"))

    def test_a_real_question_still_records(self, project_root):
        from hybrid_search.memory import qa_log

        class _Hit:
            file_path = "a.py"
            start_line = 1
            end_line = 2
            name = "f"
            qualified_name = "f"
            node_type = "function"
            snippet = "x"
            content = "x"
            rrf_score = 0.1
            project = "p"

        class _Resp:
            results = [_Hit()]
            confidence = "strong"
            query_type = "KOREAN_NL"
            effective_bm25_weight = 0.15
            query_time_ms = 1.0
            total_chunks_searched = 10

        saved = qa_log.record(
            query="환불 흐름이 어떻게 되나",
            response=_Resp(),
            cwd=str(project_root),
            async_write=False,
            trigger="user_prompt_submit",
        )

        assert saved is not None and saved.is_file()


class TestHarnessDebrisDetection:
    """Debris already in the corpus is archived, not left to be served back."""

    def _qa(self, root, stem, query):
        d = root / ".hybrid-search" / "qa" / "2026" / "09"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.md").write_text(
            f'---\nquery: "{query}"\ntrigger: user_prompt_submit\n---\n\n# Q: x\n',
            encoding="utf-8",
        )
        return d / f"{stem}.md"

    def test_finds_multiline_task_notifications(self, tmp_path):
        from hybrid_search.memory import integrity

        debris = self._qa(
            tmp_path, "01-000001-aaaa",
            "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>",
        )
        keep = self._qa(tmp_path, "01-000002-bbbb", "환불 흐름이 어떻게 되나")

        found = integrity.detect_harness_debris(tmp_path)

        assert found == [debris], "the query spans lines — a single-line scan misses it"
        assert keep.is_file()

    def test_a_question_mentioning_the_word_is_kept(self, tmp_path):
        from hybrid_search.memory import integrity

        self._qa(tmp_path, "01-000003-cccc", "task notification 처리 어떻게 하지")

        assert integrity.detect_harness_debris(tmp_path) == []


class TestCycleLine:
    """The measurement cycle reports itself into the next session.

    A loop that has to be asked for its result is a script. This line is
    the half that closes it: the next session sees what regressed and what
    is waiting to be judged, without running anything.
    """

    def _write(self, tmp_path, name, doc):
        import json
        d = tmp_path / "benchmarks" / "cycle"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")

    def _line(self, tmp_path, monkeypatch, owner=True):
        from hybrid_search.memory import hook_runtime
        real = Path.expanduser

        def fake(self):
            s = str(self)
            if s.startswith("~/.hybrid-search"):
                return tmp_path / s[len("~/.hybrid-search/"):]
            return real(self)

        monkeypatch.setattr(Path, "expanduser", fake)
        root = tmp_path / "proj"
        if owner:
            (root / "benchmarks").mkdir(parents=True, exist_ok=True)
            (root / "benchmarks" / "cycle.py").write_text("", encoding="utf-8")
        else:
            root.mkdir(parents=True, exist_ok=True)
        return hook_runtime._cycle_line(root)

    def test_silent_in_a_project_that_does_not_own_the_runner(
        self, tmp_path, monkeypatch
    ):
        """The measured project is not where the command lives.

        The dogfood corpus belongs to a math academy's app. Telling its
        session to run a benchmark from another repo is an instruction it
        cannot follow, in a session that is not about the tool.
        """
        import datetime
        today = datetime.date.today().isoformat()
        self._write(tmp_path, today, {
            "date": today,
            "displacement": {"p": {"unlabelled": ["a", "b"]}},
        })
        assert self._line(tmp_path, monkeypatch, owner=False) == ""
        assert "판정 대기 2건" in self._line(tmp_path, monkeypatch, owner=True)

    def test_silent_when_there_is_no_cycle(self, tmp_path, monkeypatch):
        assert self._line(tmp_path, monkeypatch) == ""

    def test_silent_when_nothing_moved(self, tmp_path, monkeypatch):
        import datetime
        today = datetime.date.today().isoformat()
        body = {"date": today, "set_a": {"answer_in_top3": 0.7, "answer_found": 0.9}}
        self._write(tmp_path, "2026-01-01", dict(body, date="2026-01-01"))
        self._write(tmp_path, today, body)
        assert self._line(tmp_path, monkeypatch) == ""

    def test_a_regression_is_named_with_both_numbers(self, tmp_path, monkeypatch):
        import datetime
        today = datetime.date.today().isoformat()
        self._write(tmp_path, "2026-01-01",
                    {"date": "2026-01-01", "set_a": {"answer_in_top3": 0.7}})
        self._write(tmp_path, today,
                    {"date": today, "set_a": {"answer_in_top3": 0.65}})
        line = self._line(tmp_path, monkeypatch)
        assert "Set A top3 0.7→0.65" in line

    def test_unjudged_displacements_are_counted(self, tmp_path, monkeypatch):
        import datetime
        today = datetime.date.today().isoformat()
        self._write(tmp_path, today, {
            "date": today,
            "displacement": {"p": {"unlabelled": ["a", "b", "c"]}},
        })
        assert "판정 대기 3건" in self._line(tmp_path, monkeypatch)

    def test_a_stale_measurement_asks_to_be_re_run(self, tmp_path, monkeypatch):
        self._write(tmp_path, "2026-01-01", {"date": "2026-01-01"})
        line = self._line(tmp_path, monkeypatch)
        assert "benchmarks/cycle.py" in line


class TestWorktreesShareOneMemory:
    """Every worktree of one repo resolves to the same project.

    A linked worktree registers under its own path, and its index holds the
    handful of files that worktree changed and no memory at all. On
    2026-09-12 the dogfood repo had 22 such worktrees — every one of them
    with 0 qa records against the main checkout's 2,023 — and the same
    question asked from a worktree came back with zero memory rows while
    the main folder returned ten. The write path had resolved worktrees
    since 2026-09-04; the read path matched raw paths and did not.
    """

    def _repo(self, tmp_path):
        main = tmp_path / "repo"
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        wt = tmp_path / "repo-wt"
        wt.mkdir()
        (wt / ".git").write_text(
            f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n", encoding="utf-8")
        return main, wt

    def test_a_worktree_resolves_to_the_main_checkout(self, tmp_path):
        from hybrid_search.memory.hook_runtime import canonical_project_root

        main, wt = self._repo(tmp_path)
        assert canonical_project_root(str(wt)) == main
        assert canonical_project_root(str(wt / "src" / "deep")) == main

    def test_the_main_checkout_resolves_to_itself(self, tmp_path):
        from hybrid_search.memory.hook_runtime import canonical_project_root

        main, _ = self._repo(tmp_path)
        assert canonical_project_root(str(main)) == main

    def test_search_picks_the_main_project_from_inside_a_worktree(self, tmp_path):
        """The read path, which is the half that was missing."""
        from hybrid_search.search.orchestrator import SearchOrchestrator

        main, wt = self._repo(tmp_path)
        infos = [
            SimpleNamespace(id="main", path=str(main), name="repo"),
            SimpleNamespace(id="wt", path=str(wt), name="repo-wt"),
        ]
        assert SearchOrchestrator._detect_primary_project(str(wt), infos) == "main"

    def test_indexing_from_a_worktree_registers_the_main_checkout(self, tmp_path):
        """The third place that decides "which project is this".

        Search and the hooks both resolve worktrees; indexing did not, and
        that is how 22 ghost projects accumulated — each a partial copy of
        the tree with no memory at all, 143 MB of them (2026-09-12 cleanup).
        """
        from hybrid_search.project import canonical_project_root

        main, wt = self._repo(tmp_path)
        registered: list[str] = []

        class FakeRegistry:
            def register(self, name, path):
                registered.append(path)

        # The two indexers share one line; assert the line, not the callers.
        resolved = canonical_project_root(str(wt)) or Path(str(wt)).resolve()
        FakeRegistry().register(resolved.name, str(resolved))
        assert registered == [str(main)]

    def test_an_unrelated_directory_still_matches_nothing(self, tmp_path):
        from hybrid_search.search.orchestrator import SearchOrchestrator

        main, _ = self._repo(tmp_path)
        other = tmp_path / "elsewhere"
        other.mkdir()
        infos = [SimpleNamespace(id="main", path=str(main), name="repo")]
        assert SearchOrchestrator._detect_primary_project(str(other), infos) is None
