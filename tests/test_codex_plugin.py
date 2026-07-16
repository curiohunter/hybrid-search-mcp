"""P0-3 tests — Codex first-class plugin packaging + smoke test.

The install experience is the product here: one idempotent command that
writes the plugin manifest AND the legacy hook files, plus a smoke test
that proves the two agents share one memory root. HOME is isolated per
test so nothing touches the developer's real ~/.codex or ~/.claude.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hybrid_search import codex_plugin
from hybrid_search.codex_plugin import (
    SmokeCheck,
    _shared_root_check,
    _stop_roundtrip_check,
    build_plugin_manifest,
    install_codex_plugin,
    manifest_path,
    smoke_test,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".hybrid-search").mkdir(parents=True)
    return root


# --- manifest ----------------------------------------------------------------

class TestManifest:
    def test_bundles_hooks_and_mcp(self) -> None:
        m = build_plugin_manifest()
        assert m["name"] == codex_plugin.PLUGIN_NAME
        assert set(m["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}
        assert "hybrid-search" in m["mcpServers"]
        assert m["mcpServers"]["hybrid-search"]["args"] == ["-m", "hybrid_search.server"]

    def test_scoped_paths(self, project: Path) -> None:
        assert manifest_path(project) == project / ".codex-plugin" / "plugin.json"
        assert manifest_path(project, user=True) == (
            Path.home() / ".codex" / "plugins" / codex_plugin.PLUGIN_NAME / "plugin.json"
        )


class TestInstall:
    def test_writes_manifest_and_legacy_files(self, project: Path) -> None:
        result = install_codex_plugin(project)
        assert result["status"] == "wrote"
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        assert manifest["name"] == codex_plugin.PLUGIN_NAME
        assert (project / ".codex" / "hooks.json").exists()
        assert (project / ".codex" / "config.toml").exists()

    def test_idempotent_second_run(self, project: Path) -> None:
        """CX-T1 — repeat runs change nothing and report 'exists'."""
        install_codex_plugin(project)
        second = install_codex_plugin(project)
        assert second["manifest_changed"] is False
        assert second["status"] == "exists"

    def test_user_scope(self, project: Path) -> None:
        result = install_codex_plugin(project, user=True)
        assert Path(result["manifest_path"]).is_relative_to(Path.home())
        assert (Path.home() / ".codex" / "hooks.json").exists()


# --- smoke test ----------------------------------------------------------------

class TestSmoke:
    def test_stop_roundtrip_writes_and_cleans_qa(
        self, project: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CX-T2 core — fake Stop event lands a qa record, then the smoke
        artifact is deleted (never pollute the corpus)."""
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "1")
        check = _stop_roundtrip_check(project)
        assert check.ok, check.detail
        qa_dir = project / ".hybrid-search" / "qa"
        leftovers = [
            p for p in qa_dir.rglob("*.md")
            if "codex-smoke-" in p.read_text(encoding="utf-8")
        ] if qa_dir.is_dir() else []
        assert leftovers == []

    def test_stop_roundtrip_skips_when_qa_disabled(
        self, project: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-1 review: a check that could not run is SKIP, not PASS."""
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "0")
        check = _stop_roundtrip_check(project)
        assert check.skipped is True
        assert check.ok is True  # skip never fails the gate either

    def test_shared_root_requires_both_agents_wired(self, project: Path) -> None:
        """CX-T3 shape — the check fails until BOTH agent surfaces point
        at the same root."""
        assert _shared_root_check(project).ok is False
        (project / "CLAUDE.md").write_text("uses hybrid-search MCP", encoding="utf-8")
        assert _shared_root_check(project).ok is False
        (project / "AGENTS.md").write_text(
            "<!-- hybrid-search-mcp:codex-routing -->", encoding="utf-8"
        )
        assert _shared_root_check(project).ok is True

    def test_full_smoke_green_after_install(
        self, project: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "1")
        install_codex_plugin(project)
        (project / "CLAUDE.md").write_text("uses hybrid-search MCP", encoding="utf-8")
        claude_json = Path.home() / ".claude.json"
        claude_json.write_text(json.dumps(
            {"mcpServers": {"hybrid-search": {"command": "python"}}}
        ), encoding="utf-8")

        checks = smoke_test(project)
        failures = [c for c in checks if not c.ok]
        assert failures == [], failures
        assert len(checks) == 4

    def test_smoke_reports_missing_pieces(self, project: Path) -> None:
        checks = smoke_test(project)
        by_name = {c.name: c for c in checks}
        assert by_name["hooks-registered"].ok is False
        assert by_name["mcp-both-agents"].ok is False


# --- teardown symmetry ------------------------------------------------------------

class TestTeardown:
    def test_teardown_removes_user_manifest(self, project: Path) -> None:
        """CX-T4 — the user-scoped manifest is global surface and must go;
        the project-scoped one follows per-project policy and stays."""
        from hybrid_search.cli import cmd_teardown

        install_codex_plugin(project, user=True)
        install_codex_plugin(project)
        user_path = manifest_path(project, user=True)
        assert user_path.exists()

        cmd_teardown(argparse.Namespace())

        assert not user_path.exists()
        assert manifest_path(project).exists()
