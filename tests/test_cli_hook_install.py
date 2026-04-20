"""Tests for CLI hook install behaviors — cmd_setup PreToolUse hooks + .gitignore."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hybrid_search.cli import _ensure_gitignore_entries


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
