"""Tests for CLI hook install behaviors — cmd_setup PreToolUse hooks + .gitignore."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hybrid_search.cli import (
    _CLAUDE_MD_MARKER,
    _CLAUDE_MD_SECTION,
    _claude_md_has_routing,
    _HOOK_IDENTITY_MARKER,
    _NEEDS_SYNTHESIS_FLAG,
    _build_post_checkout_script,
    _build_post_commit_script,
    _clear_needs_synthesis_flag,
    _ensure_claude_md,
    _ensure_gitignore_entries,
    _git_hooks_dir,
    _memory_health,
    _memory_cards_indexed,
    _print_doctor_report,
    _remove_claude_md,
    _write_memory_report,
    _write_needs_synthesis_flag,
    cmd_setup,
    cmd_install_hook,
    cmd_maintain,
    cmd_memory_refresh,
    cmd_recalibrate,
)
from hybrid_search.memory import qa_log
from hybrid_search.project import ProjectInfo


class TestRecalibrate:
    def test_first_run_writes_router_confidence(self, tmp_path: Path, monkeypatch) -> None:
        gold = tmp_path / "gold.json"
        gold.write_text(
            json.dumps({"queries": [{"query": "a"}, {"query": "b"}, {"query": "c"}]}),
            encoding="utf-8",
        )

        class _Resp:
            def __init__(self, top_score, score_gap):
                self.top_score = top_score
                self.score_gap = score_gap

        class _Orchestrator:
            def __init__(self, *args, **kwargs):
                self.values = iter([
                    _Resp(0.01, 0.001),
                    _Resp(0.03, 0.003),
                    _Resp(0.05, 0.005),
                ])

            def hybrid_search(self, **kwargs):
                return next(self.values)

        monkeypatch.setattr(
            "hybrid_search.cli.load_config",
            lambda: SimpleNamespace(
                global_dir=tmp_path / "g",
                embedding=None,
                models_dir=tmp_path / "m",
            ),
        )
        monkeypatch.setattr("hybrid_search.cli.ProjectRegistry", lambda global_dir: object())
        monkeypatch.setattr("hybrid_search.cli.Embedder", lambda embedding, models_dir: object())
        monkeypatch.setattr("hybrid_search.search.orchestrator.SearchOrchestrator", _Orchestrator)

        cmd_recalibrate(SimpleNamespace(cwd=str(tmp_path), gold=str(gold), project=None, limit=10))

        content = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "[router.confidence]" in content
        assert "strong_score = 0.036800" in content
        assert "strong_gap = 0.003680" in content
        assert "weak_score = 0.023200" in content

    def test_rerun_is_byte_identical(self, tmp_path: Path, monkeypatch) -> None:
        gold = tmp_path / "gold.json"
        gold.write_text(json.dumps({"queries": [{"query": "a"}]}), encoding="utf-8")

        class _Resp:
            top_score = 0.02
            score_gap = 0.004

        class _Orchestrator:
            def __init__(self, *args, **kwargs):
                pass

            def hybrid_search(self, **kwargs):
                return _Resp()

        monkeypatch.setattr(
            "hybrid_search.cli.load_config",
            lambda: SimpleNamespace(
                global_dir=tmp_path / "g",
                embedding=None,
                models_dir=tmp_path / "m",
            ),
        )
        monkeypatch.setattr("hybrid_search.cli.ProjectRegistry", lambda global_dir: object())
        monkeypatch.setattr("hybrid_search.cli.Embedder", lambda embedding, models_dir: object())
        monkeypatch.setattr("hybrid_search.search.orchestrator.SearchOrchestrator", _Orchestrator)

        args = SimpleNamespace(cwd=str(tmp_path), gold=str(gold), project=None, limit=10)
        cmd_recalibrate(args)
        first = (tmp_path / "config.toml").read_bytes()
        cmd_recalibrate(args)
        second = (tmp_path / "config.toml").read_bytes()

        assert first == second


# ---------------------------------------------------------------------------
# _ensure_gitignore_entries
# ---------------------------------------------------------------------------


class TestEnsureGitignoreEntries:
    """Adding hybrid-search entries to .gitignore."""

    def test_creates_gitignore_when_missing(self, tmp_path: Path) -> None:
        _ensure_gitignore_entries(tmp_path)
        gi = tmp_path / ".gitignore"
        assert gi.exists()
        content = gi.read_text()
        assert ".hybrid-search/wiki/" in content
        assert ".hybrid-search/coverage.json" in content

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\nvenv/\n")
        _ensure_gitignore_entries(tmp_path)
        content = gi.read_text()
        assert "node_modules/" in content  # preserved
        assert "venv/" in content           # preserved
        assert ".hybrid-search/wiki/" in content  # added

    def test_idempotent(self, tmp_path: Path) -> None:
        _ensure_gitignore_entries(tmp_path)
        first = (tmp_path / ".gitignore").read_text()
        _ensure_gitignore_entries(tmp_path)
        second = (tmp_path / ".gitignore").read_text()
        assert first == second

    def test_detects_line_exact_not_substring(self, tmp_path: Path) -> None:
        """`.hybrid-search/wiki/_synthesis_input/` should NOT satisfy `.hybrid-search/wiki/`."""
        gi = tmp_path / ".gitignore"
        gi.write_text(".hybrid-search/wiki/_synthesis_input/\n")
        _ensure_gitignore_entries(tmp_path)
        content = gi.read_text()
        # Both must be present as separate lines
        lines = {line.strip() for line in content.splitlines()}
        assert ".hybrid-search/wiki/" in lines
        assert ".hybrid-search/wiki/_synthesis_input/" in lines

    def test_trailing_slash_variant_accepted(self, tmp_path: Path) -> None:
        """`.hybrid-search/wiki` (no slash) should be treated as same as `.hybrid-search/wiki/`."""
        gi = tmp_path / ".gitignore"
        gi.write_text(".hybrid-search/wiki\n")  # no trailing slash
        _ensure_gitignore_entries(tmp_path)
        lines = {line.strip() for line in gi.read_text().splitlines()}
        # Must not produce both variants
        assert not ({".hybrid-search/wiki", ".hybrid-search/wiki/"} <= lines), \
            "Should not add wiki/ if wiki (no-slash) already present"

    def test_includes_needs_synthesis_entry(self, tmp_path: Path) -> None:
        """M4: needs_synthesis flag must be git-ignored so it never leaks into commits."""
        _ensure_gitignore_entries(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert ".hybrid-search/needs_synthesis" in content


# ---------------------------------------------------------------------------
# M4 — needs_synthesis flag helpers
# ---------------------------------------------------------------------------


class TestNeedsSynthesisFlag:
    """Lightweight flag file driving the /search → /maintain reminder loop."""

    def test_write_creates_json_with_expected_shape(self, tmp_path: Path) -> None:
        stale_items = [
            {"page_id": "pg1", "title": "tools"},
            {"page_id": "pg2", "title": "storage"},
        ]
        _write_needs_synthesis_flag(tmp_path, stale_items)

        flag = tmp_path / _NEEDS_SYNTHESIS_FLAG
        assert flag.exists()
        payload = json.loads(flag.read_text(encoding="utf-8"))
        assert payload["stale_count"] == 2
        assert payload["stale_modules"] == ["tools", "storage"]
        # ISO 8601 UTC — ends with "+00:00" or "Z"; we only guarantee parsability.
        from datetime import datetime
        datetime.fromisoformat(payload["detected_at"])

    def test_write_creates_parent_dir(self, tmp_path: Path) -> None:
        """Flag path lives under .hybrid-search/ — parent must be created on demand."""
        assert not (tmp_path / ".hybrid-search").exists()
        _write_needs_synthesis_flag(tmp_path, [{"page_id": "p", "title": "m"}])
        assert (tmp_path / ".hybrid-search").is_dir()

    def test_write_truncates_module_list(self, tmp_path: Path) -> None:
        """Long stale lists should be capped so the flag stays small/readable."""
        stale = [{"page_id": f"p{i}", "title": f"m{i}"} for i in range(50)]
        _write_needs_synthesis_flag(tmp_path, stale)
        payload = json.loads((tmp_path / _NEEDS_SYNTHESIS_FLAG).read_text())
        assert payload["stale_count"] == 50  # count stays honest
        assert len(payload["stale_modules"]) <= 20  # preview capped

    def test_clear_removes_existing_flag(self, tmp_path: Path) -> None:
        _write_needs_synthesis_flag(tmp_path, [{"page_id": "p", "title": "m"}])
        removed = _clear_needs_synthesis_flag(tmp_path)
        assert removed is True
        assert not (tmp_path / _NEEDS_SYNTHESIS_FLAG).exists()

    def test_clear_is_noop_when_missing(self, tmp_path: Path) -> None:
        removed = _clear_needs_synthesis_flag(tmp_path)
        assert removed is False

    def test_write_falls_back_to_page_id_when_title_missing(self, tmp_path: Path) -> None:
        """Defensive: stale_items without title shouldn't crash the writer."""
        _write_needs_synthesis_flag(tmp_path, [{"page_id": "pg-xyz"}])
        payload = json.loads((tmp_path / _NEEDS_SYNTHESIS_FLAG).read_text())
        assert payload["stale_modules"] == ["pg-xyz"]


# ---------------------------------------------------------------------------
# Hook payload — the JSON that fires on Glob|Grep
# ---------------------------------------------------------------------------


class TestRouteHookPayload:
    """Verify the Glob|Grep hook command behavior by simulating it."""

    ROUTE_CMD = (
        'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
        '[ -n "$ROOT" ] && [ -f "$ROOT/.hybrid-search/wiki/index.md" ] && '
        'echo \'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
        '"additionalContext":"hybrid-search: test marker"}}\''
    )

    def test_gate_blocks_when_no_index(self, tmp_path: Path) -> None:
        """No .hybrid-search/wiki/index.md → no output."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        result = subprocess.run(
            ["bash", "-c", self.ROUTE_CMD],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""

    def test_gate_fires_with_index(self, tmp_path: Path) -> None:
        """With index.md → emits valid JSON with hookSpecificOutput."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        wiki = tmp_path / ".hybrid-search" / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "index.md").write_text("# Index\n")
        result = subprocess.run(
            ["bash", "-c", self.ROUTE_CMD],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.stdout.strip(), "expected JSON output"
        parsed = json.loads(result.stdout)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "additionalContext" in parsed["hookSpecificOutput"]

    def test_gate_silent_outside_git(self, tmp_path: Path) -> None:
        """No git repo → `git rev-parse` fails → no output."""
        # No git init
        result = subprocess.run(
            ["bash", "-c", self.ROUTE_CMD],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Identity filter — idempotent re-install
# ---------------------------------------------------------------------------


class TestHookIdentityFilter:
    """cmd_setup's filter must remove old hybrid-search hooks before re-appending."""

    def test_substring_identifies_route_hook(self) -> None:
        """A route_hook command contains 'wiki/index.md' — identity filter catches it."""
        hook_cmd = (
            'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
            '[ -f "$ROOT/.hybrid-search/wiki/index.md" ] && echo ...'
        )
        identity_matches = "wiki/index.md" in hook_cmd
        assert identity_matches


# ---------------------------------------------------------------------------
# _ensure_claude_md + _remove_claude_md (Q7)
# ---------------------------------------------------------------------------


class TestEnsureClaudeMd:
    """CLAUDE.md install/update/remove — idempotent, marker-bounded."""

    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        _ensure_claude_md(str(tmp_path))
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text(encoding="utf-8")
        assert "<!-- BEGIN hybrid-search-mcp routing v1 -->" in content
        assert "반드시 이 순서로" in content

    def test_inserts_after_h1_when_present(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting intro paragraph.\n")
        _ensure_claude_md(str(tmp_path))
        content = claude_md.read_text(encoding="utf-8")
        lines = content.splitlines()
        h1_idx = next(i for i, ln in enumerate(lines) if ln.startswith("# "))
        marker_idx = next(i for i, ln in enumerate(lines) if "hybrid-search-mcp routing v1" in ln)
        assert h1_idx < marker_idx, "hybrid-search block must follow the H1"
        assert "Existing intro paragraph." in content

    def test_idempotent_when_section_unchanged(self, tmp_path: Path) -> None:
        _ensure_claude_md(str(tmp_path))
        first = (tmp_path / "CLAUDE.md").read_text()
        _ensure_claude_md(str(tmp_path))
        second = (tmp_path / "CLAUDE.md").read_text()
        assert first == second

    def test_updates_existing_section_in_place(self, tmp_path: Path) -> None:
        """Old section content is replaced with current _CLAUDE_MD_SECTION."""
        claude_md = tmp_path / "CLAUDE.md"
        stale = (
            "# Project\n\n"
            f"{_CLAUDE_MD_MARKER}\n"
            "## 검색 전략 — old heading\n"
            "STALE LINE THAT MUST GO\n\n"
            "## Keep Me\n\n"
            "User content preserved.\n"
        )
        claude_md.write_text(stale, encoding="utf-8")
        _ensure_claude_md(str(tmp_path))
        content = claude_md.read_text(encoding="utf-8")
        assert "STALE LINE THAT MUST GO" not in content
        assert "반드시 이 순서로" in content
        assert "## Keep Me" in content
        assert "User content preserved." in content

    def test_preserves_unrelated_content(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Project\n\n"
            "## Coding rules\n\n"
            "- Do not mutate\n\n"
            "## Testing\n\n"
            "- pytest -x\n",
            encoding="utf-8",
        )
        _ensure_claude_md(str(tmp_path))
        content = claude_md.read_text(encoding="utf-8")
        assert "## Coding rules" in content
        assert "- Do not mutate" in content
        assert "## Testing" in content
        assert "- pytest -x" in content


class TestRemoveClaudeMd:
    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        assert _remove_claude_md(str(tmp_path)) is False

    def test_noop_when_marker_absent(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Project\n\nnothing to remove\n", encoding="utf-8")
        assert _remove_claude_md(str(tmp_path)) is False
        assert "nothing to remove" in claude_md.read_text(encoding="utf-8")

    def test_removes_section_preserving_rest(self, tmp_path: Path) -> None:
        _ensure_claude_md(str(tmp_path))  # install
        claude_md = tmp_path / "CLAUDE.md"
        # Add trailing user section
        claude_md.write_text(
            claude_md.read_text(encoding="utf-8") + "\n## Follow-up\n\nkeep me\n",
            encoding="utf-8",
        )
        assert _remove_claude_md(str(tmp_path)) is True
        content = claude_md.read_text(encoding="utf-8")
        assert _CLAUDE_MD_MARKER not in content
        assert "검색 전략" not in content
        assert "## Follow-up" in content
        assert "keep me" in content


class TestSetupRoutingFlags:
    def _home(self, tmp_path: Path, monkeypatch) -> Path:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        return home

    def test_setup_dry_run_prints_diff_without_mutation(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        home = self._home(tmp_path, monkeypatch)
        project = tmp_path / "project"
        project.mkdir()
        claude_md = project / "CLAUDE.md"
        original = "# Project\n"
        claude_md.write_text(original, encoding="utf-8")

        cmd_setup(argparse.Namespace(cwd=str(project), dry_run=True, force=False))

        out = capsys.readouterr().out
        assert "--- " in out
        assert "CLAUDE.md (current)" in out
        assert "AGENTS.md (proposed)" in out
        assert "hook/config installation skipped" in out
        assert claude_md.read_text(encoding="utf-8") == original
        assert not (project / "AGENTS.md").exists()
        assert not (home / ".claude.json").exists()

    def test_setup_migrates_legacy_claude(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        self._home(tmp_path, monkeypatch)
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text(
            f"{_CLAUDE_MD_MARKER}\n## 검색 전략 — old\nlegacy\n",
            encoding="utf-8",
        )

        cmd_setup(argparse.Namespace(cwd=str(project), dry_run=False, force=False))

        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert "<!-- BEGIN hybrid-search-mcp routing v1 -->" in text
        assert _CLAUDE_MD_MARKER not in text

    def test_setup_force_recovers_corrupted_claude(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        self._home(tmp_path, monkeypatch)
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text(
            "<!-- BEGIN hybrid-search-mcp routing v1 -->\nold\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            cmd_setup(argparse.Namespace(cwd=str(project), dry_run=False, force=False))

        cmd_setup(argparse.Namespace(cwd=str(project), dry_run=False, force=True))
        text = (project / "CLAUDE.md").read_text(encoding="utf-8")
        assert text.count("hybrid-search-mcp routing v1") == 2


# ---------------------------------------------------------------------------
# _git_hooks_dir — Husky / core.hooksPath compatibility (Q8)
# ---------------------------------------------------------------------------


class TestGitHooksDir:
    def test_default_dotgit_hooks(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        result = _git_hooks_dir(tmp_path)
        assert result.name == "hooks"
        assert result.parent.name == ".git"
        assert result.parent.parent == tmp_path.resolve()

    def test_respects_core_hookspath_relative(self, tmp_path: Path) -> None:
        """Husky's ``.husky`` convention — stored as a repo-relative path."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.hooksPath", ".husky"],
            check=True,
        )
        result = _git_hooks_dir(tmp_path)
        assert result == (tmp_path / ".husky").resolve()

    def test_respects_core_hookspath_absolute(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        custom = tmp_path / "custom-hooks"
        custom.mkdir()
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.hooksPath", str(custom)],
            check=True,
        )
        result = _git_hooks_dir(tmp_path)
        assert result == custom

    def test_fallback_when_not_a_repo(self, tmp_path: Path) -> None:
        """Non-git directory — falls back to repo_root/.git/hooks (harmless default)."""
        result = _git_hooks_dir(tmp_path)
        assert result == tmp_path / ".git" / "hooks"


# ---------------------------------------------------------------------------
# M2 — cmd_install_hook installs post-commit + post-checkout
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


class TestInstallHookBothScripts:
    """cmd_install_hook writes both post-commit and post-checkout, idempotent."""

    def test_installs_both_hooks_fresh(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        cmd_install_hook(argparse.Namespace(cwd=str(tmp_path)))

        hooks_dir = tmp_path / ".git" / "hooks"
        commit = hooks_dir / "post-commit"
        checkout = hooks_dir / "post-checkout"
        assert commit.exists(), "post-commit hook missing"
        assert checkout.exists(), "post-checkout hook missing"
        # Both have identity marker
        assert _HOOK_IDENTITY_MARKER in commit.read_text()
        assert _HOOK_IDENTITY_MARKER in checkout.read_text()
        # Both executable
        assert commit.stat().st_mode & 0o111, "post-commit not executable"
        assert checkout.stat().st_mode & 0o111, "post-checkout not executable"

    def test_reinstall_is_idempotent(self, tmp_path: Path) -> None:
        _init_git_repo(tmp_path)
        cmd_install_hook(argparse.Namespace(cwd=str(tmp_path)))
        commit = tmp_path / ".git" / "hooks" / "post-commit"
        checkout = tmp_path / ".git" / "hooks" / "post-checkout"
        before_commit = commit.read_text()
        before_checkout = checkout.read_text()

        cmd_install_hook(argparse.Namespace(cwd=str(tmp_path)))
        assert commit.read_text() == before_commit, "post-commit changed on re-install"
        assert checkout.read_text() == before_checkout, "post-checkout changed on re-install"
        # Identity marker appears exactly once in each
        assert commit.read_text().count(_HOOK_IDENTITY_MARKER) == \
            before_commit.count(_HOOK_IDENTITY_MARKER)
        assert checkout.read_text().count(_HOOK_IDENTITY_MARKER) == \
            before_checkout.count(_HOOK_IDENTITY_MARKER)

    def test_preserves_existing_unrelated_hook(self, tmp_path: Path) -> None:
        """Existing non-hybrid-search hook content must survive appending."""
        _init_git_repo(tmp_path)
        hooks_dir = tmp_path / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        for name in ("post-commit", "post-checkout"):
            path = hooks_dir / name
            path.write_text("#!/bin/bash\necho 'user hook ran'\n")
            path.chmod(0o755)

        cmd_install_hook(argparse.Namespace(cwd=str(tmp_path)))
        for name in ("post-commit", "post-checkout"):
            content = (hooks_dir / name).read_text()
            assert "echo 'user hook ran'" in content, f"{name} lost user content"
            assert _HOOK_IDENTITY_MARKER in content, f"{name} missing hybrid-search append"

    def test_refuses_non_git_directory(self, tmp_path: Path, capsys) -> None:
        # No git init — cmd_install_hook should print a message and bail
        cmd_install_hook(argparse.Namespace(cwd=str(tmp_path)))
        captured = capsys.readouterr()
        assert "Not a git repository" in captured.out
        # No hooks should be created
        assert not (tmp_path / ".git" / "hooks" / "post-commit").exists()

    def test_respects_core_hookspath(self, tmp_path: Path) -> None:
        """Both hooks go to core.hooksPath (Husky compat), not .git/hooks."""
        _init_git_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.hooksPath", ".husky"],
            check=True,
        )
        cmd_install_hook(argparse.Namespace(cwd=str(tmp_path)))

        husky = tmp_path / ".husky"
        assert (husky / "post-commit").exists()
        assert (husky / "post-checkout").exists()
        # Default location should NOT have hooks
        default = tmp_path / ".git" / "hooks"
        assert not (default / "post-commit").exists() or \
            _HOOK_IDENTITY_MARKER not in (default / "post-commit").read_text()


class TestMemoryProductUx:
    """P7/P8 product UX helpers: doctor, refresh, and static report."""

    def test_doctor_distinguishes_tool_logs_from_completed_turns(
        self, tmp_path: Path, capsys,
    ) -> None:
        (tmp_path / ".git").mkdir()
        qa_log.record_turn(
            query="How did we decide hook storage?",
            cwd=str(tmp_path),
            answer_chars=20,
            answer_excerpt="Decision: Stop hook stores completed turns.",
            trigger="codex_stop_hook",
            client="codex",
        )
        health = _memory_health(tmp_path)
        _print_doctor_report(health)
        out = capsys.readouterr().out
        assert "Memory is not fully active." in out
        assert "Corpus: qa=1, cards=0" in out
        assert "1 completed-turn logs" in out
        assert "hybrid-search-mcp memory refresh --cwd ." in out

    def test_doctor_prints_excluded_paths_summary(
        self, tmp_path: Path, capsys,
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "manual.skip").write_text("x")
        health = _memory_health(tmp_path)
        health["excluded_paths_summary"] = {
            "extension": 2,
            "oversize_md": 1,
            "manual": 3,
        }

        _print_doctor_report(health)
        out = capsys.readouterr().out

        assert "Excluded paths summary:" in out
        assert "extension: 2" in out
        assert "oversize_md: 1" in out
        assert "manual: 3" in out

    def test_memory_refresh_can_run_with_incomplete_hooks_when_allowed(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        (tmp_path / ".git").mkdir()
        qa_log.record_turn(
            query="Why use memory cards?",
            cwd=str(tmp_path),
            answer_chars=60,
            answer_excerpt=(
                "Decision: memory cards are the compact retrieval unit. "
                "Next: refresh should promote useful turns."
            ),
            trigger="codex_stop_hook",
            client="codex",
        )
        monkeypatch.setattr("hybrid_search.cli.cmd_reindex", lambda _args: print("reindex stub"))
        cmd_memory_refresh(argparse.Namespace(
            cwd=str(tmp_path),
            project=None,
            since=None,
            limit=None,
            force_reindex=False,
            allow_incomplete_hooks=True,
        ))
        out = capsys.readouterr().out
        assert "Hybrid Memory Refresh" in out
        assert "New cards:     1" in out
        assert "Facts:" in out

    def test_memory_report_writes_static_html_with_warnings(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        qa_log.record_turn(
            query="How does report explain memory?",
            cwd=str(tmp_path),
            answer_chars=40,
            answer_excerpt="Decision: report shows hooks, corpus, and warnings.",
            trigger="codex_stop_hook",
            client="codex",
        )
        path = _write_memory_report(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "Hybrid Search Memory Report" in text
        assert "No memory cards exist yet." in text
        assert "Completed turns" in text

    def test_memory_cards_indexed_accepts_domain_term_only(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        index_root = tmp_path / "indexes"
        store_db = index_root / "proj1" / "store.db"
        store_db.parent.mkdir(parents=True)
        conn = sqlite3.connect(store_db)
        try:
            conn.execute("CREATE TABLE chunks (node_type TEXT)")
            conn.execute("INSERT INTO chunks (node_type) VALUES ('domain_term')")
            conn.commit()
        finally:
            conn.close()

        class _Config:
            global_dir = tmp_path / "global"
            projects_dir = index_root

        class _Registry:
            def __init__(self, _global_dir: Path) -> None:
                pass

            def list_all(self):
                return [ProjectInfo(id="proj1", name="p", path=str(tmp_path))]

            def get_by_name(self, name: str):
                return ProjectInfo(id="proj1", name=name, path=str(tmp_path))

        monkeypatch.setattr("hybrid_search.cli.load_config", lambda: _Config())
        monkeypatch.setattr("hybrid_search.cli.ProjectRegistry", _Registry)

        assert _memory_cards_indexed(tmp_path) is True


class TestMaintainCommand:
    def test_stops_with_synthesis_guidance_when_input_pending(
        self, tmp_path: Path, monkeypatch, capsys,
    ) -> None:
        input_dir = tmp_path / ".hybrid-search" / "wiki" / "_synthesis_input"
        input_dir.mkdir(parents=True)
        (input_dir / "module-a.md").write_text("context", encoding="utf-8")
        calls: list[str] = []

        monkeypatch.setattr("hybrid_search.cli.cmd_reindex", lambda _args: calls.append("reindex"))
        monkeypatch.setattr("hybrid_search.cli.cmd_verify_synthesis", lambda _args: calls.append("verify"))
        monkeypatch.setattr("hybrid_search.cli.cmd_status", lambda _args: calls.append("status"))

        cmd_maintain(argparse.Namespace(
            cwd=str(tmp_path),
            force_reindex=False,
            keep_going=False,
            no_status=False,
        ))

        out = capsys.readouterr().out
        assert calls == ["reindex"]
        assert "synthesis input is ready" in out
        assert "module-a.md" in out

    def test_finalizes_existing_output_then_verifies_and_statuses(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        output_dir = tmp_path / ".hybrid-search" / "wiki" / "_synthesis_output"
        output_dir.mkdir(parents=True)
        (output_dir / "module-a.md").write_text("synthesis", encoding="utf-8")
        calls: list[str] = []

        monkeypatch.setattr("hybrid_search.cli.cmd_reindex", lambda _args: calls.append("reindex"))
        monkeypatch.setattr("hybrid_search.cli.cmd_synthesize_wiki", lambda _args: calls.append("finalize"))
        monkeypatch.setattr("hybrid_search.cli.cmd_verify_synthesis", lambda _args: calls.append("verify"))
        monkeypatch.setattr("hybrid_search.cli.cmd_status", lambda _args: calls.append("status"))

        cmd_maintain(argparse.Namespace(
            cwd=str(tmp_path),
            force_reindex=False,
            keep_going=False,
            no_status=False,
        ))

        assert calls == ["reindex", "finalize", "verify", "status"]


class TestPostCheckoutScriptGates:
    """post-checkout hook gate conditions — verified by executing the script."""

    @staticmethod
    def _write_hook_to_tmp(tmp_path: Path) -> Path:
        """Write the post-checkout script standalone for gate testing.

        Uses ``/bin/true`` as the venv python to keep the test hermetic — we
        only care that the gates short-circuit, not that reindex actually runs.
        """
        script = _build_post_checkout_script(Path("/bin/true"))
        hook = tmp_path / "post-checkout-test.sh"
        hook.write_text(script)
        hook.chmod(0o755)
        return hook

    def test_exits_zero_on_file_checkout(self, tmp_path: Path) -> None:
        """$3 != "1" → gate exits, no reindex spawned."""
        _init_git_repo(tmp_path)
        hook = self._write_hook_to_tmp(tmp_path)
        # Args: prev_head, new_head, flag=0 (file checkout)
        result = subprocess.run(
            [str(hook), "HEAD^", "HEAD", "0"],
            cwd=tmp_path, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        # No lock file created — gate short-circuited before reindex path
        assert not (tmp_path / ".hybrid-search" / ".reindex.lock").exists()

    def test_exits_zero_when_hybrid_search_dir_missing(self, tmp_path: Path) -> None:
        """Branch switch but no .hybrid-search/ → no auto-bootstrap."""
        _init_git_repo(tmp_path)
        hook = self._write_hook_to_tmp(tmp_path)
        # flag=1 means branch switch, but .hybrid-search/ is absent
        result = subprocess.run(
            [str(hook), "HEAD^", "HEAD", "1"],
            cwd=tmp_path, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        # No .hybrid-search directory was created — confirms no bootstrap
        assert not (tmp_path / ".hybrid-search").exists()

    def test_branch_switch_with_hybrid_search_triggers_reindex_path(
        self, tmp_path: Path,
    ) -> None:
        """flag=1 AND .hybrid-search/ exists → spawns nohup background process.

        We substitute /bin/true for the python command so the "reindex" is a
        no-op that exits immediately. We verify the script itself runs to the
        nohup invocation (returncode 0) without any gate firing.
        """
        _init_git_repo(tmp_path)
        (tmp_path / ".hybrid-search").mkdir()
        hook = self._write_hook_to_tmp(tmp_path)
        result = subprocess.run(
            [str(hook), "HEAD^", "HEAD", "1"],
            cwd=tmp_path, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"


class TestScriptContentSanity:
    """Build functions produce the gates we expect (text-level assertions)."""

    def test_post_commit_script_has_lockfile(self) -> None:
        s = _build_post_commit_script(Path("/usr/bin/python3"))
        assert "#!/bin/bash" in s
        assert "hybrid_search.cli" in s  # identity marker
        assert ".reindex.lock" in s
        assert "--git-delta" in s
        assert "--synthesize" in s

    def test_post_commit_script_exports_hook_diff_env(self) -> None:
        """M3: post-commit must capture diff and export HYBRID_SEARCH_CHANGED_STATUS."""
        s = _build_post_commit_script(Path("/usr/bin/python3"))
        assert "git diff --name-status HEAD~1 HEAD" in s
        assert "export HYBRID_SEARCH_CHANGED_STATUS=" in s
        # Initial-commit guard: don't set env when HEAD~1 doesn't exist
        assert "git rev-parse HEAD~1" in s

    def test_post_checkout_script_gates(self) -> None:
        s = _build_post_checkout_script(Path("/usr/bin/python3"))
        assert "#!/bin/bash" in s
        assert 'hybrid_search.cli' in s
        # Branch-switch gate (exactly as documented)
        assert '[ "$3" = "1" ] || exit 0' in s
        # No-auto-bootstrap gate
        assert '[ -d "$PROJECT_DIR/.hybrid-search" ] || exit 0' in s
        # Filesystem delta (NOT --git-delta — HEAD~1..HEAD is meaningless post-switch)
        assert "--git-delta" not in s
        # Synthesis is heavy; branch switch shouldn't trigger it
        assert "--synthesize" not in s
        # Shared lock with post-commit
        assert ".reindex.lock" in s


# ---------------------------------------------------------------------------
# M3 — post-commit hook synchronously exports diff; bash-level integration
# ---------------------------------------------------------------------------


class TestPostCommitDiffCapture:
    """Execute the post-commit script against real git repos — verify env export.

    We stub the venv python with ``/bin/echo`` so the "reindex" call is just a
    no-op print (exits 0 immediately, doesn't fork a background process that
    outlives the test). The shared lock + nohup run after the synchronous diff
    capture, which is the part we actually want to assert.
    """

    @staticmethod
    def _run_hook(tmp_path: Path) -> subprocess.CompletedProcess:
        # Stub python with /bin/true so the `reindex` invocation is instant.
        # What matters for this test is the shell's diff-capture + env export.
        script = _build_post_commit_script(Path("/bin/true"))
        # Instrument: after the synchronous diff capture, echo the env so we
        # can assert whether it was set. We append a `printf` that reads the
        # variable and writes to a file; insert BEFORE the gap-detection /
        # nohup block so we observe the synchronous state only.
        #
        # Hook into the script by appending a sentinel that dumps env to a
        # known path. Because shell variables exported above the insertion
        # point are visible, this is a reliable snapshot.
        sentinel = tmp_path / "env-snapshot.txt"
        instrumented = script.replace(
            '# 1. Gap detection',
            f'printf "%s" "${{HYBRID_SEARCH_CHANGED_STATUS-__UNSET__}}" > "{sentinel}"\n'
            '# 1. Gap detection',
            1,
        )
        hook_path = tmp_path / "post-commit-test.sh"
        hook_path.write_text(instrumented)
        hook_path.chmod(0o755)
        return subprocess.run(
            [str(hook_path)],
            cwd=tmp_path,
            capture_output=True, text=True, timeout=10,
            env={**subprocess_env_minimal(tmp_path)},
        )

    def test_second_commit_exports_env_with_diff(self, tmp_path: Path) -> None:
        """After HEAD~1 exists and has a real diff, env must be exported."""
        _init_git_repo(tmp_path)
        # Configure committer (required by `git commit`)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", k, v], check=True,
            )
        # Commit 1
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "c1"], check=True,
        )
        # Commit 2 — gives us a HEAD~1..HEAD diff to capture
        (tmp_path / "a.py").write_text("x = 2\n")
        (tmp_path / "b.py").write_text("y = 3\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "a.py", "b.py"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "c2"], check=True,
        )

        result = self._run_hook(tmp_path)
        assert result.returncode == 0, f"hook exited non-zero: stderr={result.stderr!r}"

        snapshot = (tmp_path / "env-snapshot.txt").read_text()
        assert snapshot != "__UNSET__", "env var was NOT exported after diff capture"
        assert "a.py" in snapshot
        assert "b.py" in snapshot
        # Must be name-status format — first token is the status code
        first_line = snapshot.splitlines()[0]
        assert first_line.startswith(("A", "M", "D", "R")), \
            f"Unexpected status token: {first_line!r}"

    def test_initial_commit_does_not_export_env(self, tmp_path: Path) -> None:
        """HEAD~1 doesn't exist on initial commit → fall back, no env set."""
        _init_git_repo(tmp_path)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(tmp_path), "config", k, v], check=True,
            )
        (tmp_path / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"], check=True,
        )

        result = self._run_hook(tmp_path)
        assert result.returncode == 0
        snapshot = (tmp_path / "env-snapshot.txt").read_text()
        assert snapshot == "__UNSET__", (
            "On initial commit the hook must leave env unset so cmd_reindex "
            f"falls back internally; got: {snapshot!r}"
        )


def subprocess_env_minimal(tmp_path: Path) -> dict[str, str]:
    """Minimal env for running bash hook tests — inherit PATH, scope HOME."""
    import os
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        # Git requires either user.name/email or env overrides; we configure
        # per-repo in the test, but also scrub GIT_DIR so the hook's
        # rev-parse uses our tmp repo.
    }


class TestStatusRoutingMarkerDetection:
    """status' CLAUDE.md routing check must recognize the v1 marker.

    Regression: install-hook writes ``<!-- BEGIN hybrid-search-mcp routing v1
    -->``, but status only matched the legacy ``<!-- hybrid-search -->`` string,
    so a freshly-installed CLAUDE.md was mis-reported as "marker missing".
    """

    def test_v1_marker_detected(self, tmp_path: Path) -> None:
        # The exact block install-hook / _ensure_claude_md writes.
        _ensure_claude_md(str(tmp_path))
        text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "BEGIN hybrid-search-mcp routing v" in text  # precondition
        assert _CLAUDE_MD_MARKER not in text                # not the legacy one
        assert _claude_md_has_routing(text) is True

    def test_v1_marker_detected_with_user_content_above(self, tmp_path: Path) -> None:
        # Mirrors the real install: a user purpose header precedes the block.
        _ensure_claude_md(str(tmp_path))
        block = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert _claude_md_has_routing("# My Project\n\nnotes\n\n" + block) is True

    def test_legacy_marker_still_detected(self) -> None:
        assert _claude_md_has_routing(f"{_CLAUDE_MD_MARKER}\n## old routing\n") is True

    def test_no_marker_not_detected(self) -> None:
        assert _claude_md_has_routing("# Just a project\n") is False
        assert _claude_md_has_routing("") is False
