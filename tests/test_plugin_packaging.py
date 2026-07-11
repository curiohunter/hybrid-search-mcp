"""Plugin packaging — manifests, bootstrap fast path, setup --global-only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]


class TestManifests:
    def test_plugin_json_shape(self) -> None:
        data = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert data["name"] == "memory-layer"
        assert data["hooks"] == "./hooks/hooks.json"
        # The plugin must NOT register the MCP server itself: plugin-scoped
        # servers get namespaced (mcp__plugin_memory-layer_..._hybrid_search)
        # and every routing contract naming mcp__hybrid-search__hybrid_search
        # would silently break. Registration goes through cli setup instead.
        assert "mcpServers" not in data

    def test_plugin_version_matches_pyproject(self) -> None:
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert f'version = "{plugin["version"]}"' in pyproject
        assert marketplace["plugins"][0]["version"] == plugin["version"]

    def test_marketplace_self_source(self) -> None:
        data = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        entry = data["plugins"][0]
        assert entry["name"] == "memory-layer"
        assert entry["source"] == "./"
        assert entry["strict"] is True

    def test_hooks_json_references_bootstrap(self) -> None:
        data = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        starts = data["hooks"]["SessionStart"]
        cmd = starts[0]["hooks"][0]["command"]
        assert "plugin-bootstrap.sh" in cmd
        assert "${CLAUDE_PLUGIN_ROOT}" in cmd
        assert (REPO / "scripts" / "plugin-bootstrap.sh").exists()


class TestBootstrapFastPath:
    def test_silent_exit_zero_when_up_to_date(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        (data / "venv" / "bin").mkdir(parents=True)
        py = data / "venv" / "bin" / "python"
        py.write_text("#!/bin/sh\n", encoding="utf-8")
        py.chmod(0o755)
        (data / "pyproject.lock").write_text(
            (REPO / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        result = subprocess.run(
            ["sh", str(REPO / "scripts" / "plugin-bootstrap.sh")],
            capture_output=True,
            text=True,
            timeout=10,
            env={"CLAUDE_PLUGIN_ROOT": str(REPO), "CLAUDE_PLUGIN_DATA": str(data), "HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit_zero_while_install_in_flight(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        data.mkdir()
        (data / ".installing").touch()
        result = subprocess.run(
            ["sh", str(REPO / "scripts" / "plugin-bootstrap.sh")],
            capture_output=True,
            text=True,
            timeout=10,
            env={"CLAUDE_PLUGIN_ROOT": str(REPO), "CLAUDE_PLUGIN_DATA": str(data), "HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "background" in result.stdout


class TestSetupGlobalOnly:
    def test_registers_global_surface_but_skips_project(self, tmp_path: Path, monkeypatch) -> None:
        from hybrid_search.cli import cmd_setup

        fakehome = tmp_path / "home"
        fakehome.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))

        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))

        claude_json = json.loads((fakehome / ".claude.json").read_text(encoding="utf-8"))
        assert "hybrid-search" in claude_json["mcpServers"]
        settings = json.loads((fakehome / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert settings["hooks"]["PreToolUse"]
        # Project surface untouched — that's the auto_index hook's job later.
        assert not (project / "CLAUDE.md").exists()
        assert not (project / ".claude").exists()
        assert not (project / ".gitignore").exists()
