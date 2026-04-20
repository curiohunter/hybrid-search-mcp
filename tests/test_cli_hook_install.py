"""Tests for CLI hook install behaviors — cmd_setup PreToolUse hooks + .gitignore."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hybrid_search.cli import (
    _CLAUDE_MD_MARKER,
    _CLAUDE_MD_SECTION,
    _ensure_claude_md,
    _ensure_gitignore_entries,
    _git_hooks_dir,
    _remove_claude_md,
)


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
        assert _CLAUDE_MD_MARKER in content
        assert "의도 기반 라우팅" in content

    def test_inserts_after_h1_when_present(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting intro paragraph.\n")
        _ensure_claude_md(str(tmp_path))
        content = claude_md.read_text(encoding="utf-8")
        lines = content.splitlines()
        h1_idx = next(i for i, ln in enumerate(lines) if ln.startswith("# "))
        marker_idx = next(i for i, ln in enumerate(lines) if _CLAUDE_MD_MARKER in ln)
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
        assert "의도 기반 라우팅" in content
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
        assert "의도 기반 라우팅" not in content
        assert "## Follow-up" in content
        assert "keep me" in content


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
