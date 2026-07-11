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
    @staticmethod
    def _fake_venv(data: Path) -> None:
        (data / "venv" / "bin").mkdir(parents=True)
        py = data / "venv" / "bin" / "python"
        py.write_text("#!/bin/sh\n", encoding="utf-8")
        py.chmod(0o755)

    @staticmethod
    def _run(tmp_path: Path, data: Path, *, root: Path = REPO, broken_python: bool = False):
        env = {
            "CLAUDE_PLUGIN_ROOT": str(root),
            "CLAUDE_PLUGIN_DATA": str(data),
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        }
        if broken_python:
            # Fail venv creation instantly so the installer subshell dies on
            # set -e without ever touching pip or the network — what we're
            # testing is the lock/trap lifecycle, not pip.
            stubs = tmp_path / "stubs"
            stubs.mkdir(exist_ok=True)
            for name in ("python3", "python3.11", "python3.12"):
                stub = stubs / name
                stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                stub.chmod(0o755)
            env["PATH"] = f"{stubs}:{env['PATH']}"
        return subprocess.run(
            ["sh", str(REPO / "scripts" / "plugin-bootstrap.sh")],
            capture_output=True, text=True, timeout=15, env=env,
        )

    @staticmethod
    def _repo_revision() -> str:
        return subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    def test_silent_exit_zero_when_at_current_revision(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        self._fake_venv(data)
        (data / "revision.lock").write_text(self._repo_revision(), encoding="utf-8")
        result = self._run(tmp_path, data)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_source_change_invalidates_lock(self, tmp_path: Path) -> None:
        # An upgrade that changes source but not pyproject must reinstall —
        # the lock is the commit SHA, so any stale value goes slow-path.
        data = tmp_path / "data"
        self._fake_venv(data)
        (data / "revision.lock").write_text("deadbeef-stale-revision", encoding="utf-8")
        # broken_python keeps the spawned installer from doing real pip work;
        # the assertion is that the fast path was NOT taken.
        result = self._run(tmp_path, data, broken_python=True)
        assert result.returncode == 0
        assert "updating to new revision" in result.stdout
        import time
        for _ in range(50):
            if not (data / ".installing.lock").exists():
                break
            time.sleep(0.2)

    def test_exit_zero_while_install_in_flight(self, tmp_path: Path) -> None:
        import time
        data = tmp_path / "data"
        (data / ".installing.lock").mkdir(parents=True)
        (data / ".installing.lock" / "owner").write_text(f"0 {int(time.time())}", encoding="utf-8")
        result = self._run(tmp_path, data)
        assert result.returncode == 0
        assert "still running" in result.stdout
        assert (data / ".installing.lock").exists()  # fresh lock not reclaimed

    def test_fresh_unstamped_lockdir_is_treated_as_in_flight(self, tmp_path: Path) -> None:
        # The exact window the CI race lived in: lockdir exists, owner file
        # not written yet. Age must come from the lockdir's own mtime (GNU
        # stat -c first — BSD-style `stat -f %m` on Linux prints the
        # FILESYSTEM report with exit 0, poisoning the arithmetic), so a
        # brand-new unstamped lock reads as in-flight, never as stale.
        data = tmp_path / "data"
        (data / ".installing.lock").mkdir(parents=True)  # no owner file
        result = self._run(tmp_path, data)
        assert result.returncode == 0
        assert "still running" in result.stdout
        assert (data / ".installing.lock").exists()

    def test_stale_lock_is_reclaimed_and_failure_clears_lock(self, tmp_path: Path) -> None:
        import time
        data = tmp_path / "data"
        (data / ".installing.lock").mkdir(parents=True)
        # Dead PID + ancient timestamp = stale; must be reclaimed instead of
        # wedging every future session.
        (data / ".installing.lock" / "owner").write_text("999999 1", encoding="utf-8")
        result = self._run(tmp_path, data, broken_python=True)
        assert result.returncode == 0
        assert "install started" in result.stdout or "updating" in result.stdout
        # The spawned installer fails fast (venv creation stub) — the trap
        # must clear the lockdir and NOT write revision.lock, so the next
        # SessionStart retries.
        for _ in range(50):
            if not (data / ".installing.lock").exists():
                break
            time.sleep(0.2)
        assert not (data / ".installing.lock").exists()
        assert not (data / "revision.lock").exists()

    def test_failed_install_retries_on_next_run(self, tmp_path: Path) -> None:
        import time
        data = tmp_path / "data"
        data.mkdir(parents=True)
        first = self._run(tmp_path, data, broken_python=True)
        assert first.returncode == 0
        for _ in range(50):
            if not (data / ".installing.lock").exists():
                break
            time.sleep(0.2)
        second = self._run(tmp_path, data, broken_python=True)
        assert second.returncode == 0
        assert "install started" in second.stdout  # retried, not "still running"

    def test_concurrent_bootstraps_spawn_exactly_one_installer(self, tmp_path: Path) -> None:
        # mkdir-based acquisition: of N simultaneous SessionStarts exactly
        # one wins the lock; check-then-write on a plain file lets several
        # through. The python3 stub sleeps so the winner holds the lock for
        # the whole race window.
        import concurrent.futures
        import time

        data = tmp_path / "data"
        data.mkdir(parents=True)
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        for name in ("python3", "python3.11", "python3.12"):
            stub = stubs / name
            stub.write_text("#!/bin/sh\nsleep 4\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)
        env = {
            "CLAUDE_PLUGIN_ROOT": str(REPO),
            "CLAUDE_PLUGIN_DATA": str(data),
            "HOME": str(tmp_path),
            "PATH": f"{stubs}:/usr/bin:/bin",
        }

        def run_one(_):
            return subprocess.run(
                ["sh", str(REPO / "scripts" / "plugin-bootstrap.sh")],
                capture_output=True, text=True, timeout=20, env=env,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run_one, range(8)))

        assert all(r.returncode == 0 for r in results)
        started = sum(1 for r in results if "install started" in r.stdout or "updating" in r.stdout)
        waiting = sum(1 for r in results if "still running" in r.stdout)
        assert started == 1, [r.stdout for r in results]
        assert waiting == 7
        for _ in range(60):
            if not (data / ".installing.lock").exists():
                break
            time.sleep(0.2)


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


class TestTeardown:
    def test_setup_then_teardown_leaves_no_global_surface(self, tmp_path: Path, monkeypatch) -> None:
        # Plugin uninstall runs no cleanup hooks, so `teardown` is the
        # documented removal path — the full roundtrip must be clean.
        from hybrid_search.cli import cmd_setup, cmd_teardown

        fakehome = tmp_path / "home"
        fakehome.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))

        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))
        # A hook the user added themselves must survive teardown.
        settings_path = fakehome / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["hooks"].setdefault("PreToolUse", []).append(
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user-hook"}]}
        )
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        cmd_teardown(SimpleNamespace())

        claude_json = json.loads((fakehome / ".claude.json").read_text(encoding="utf-8"))
        assert "hybrid-search" not in claude_json.get("mcpServers", {})
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        remaining = [
            str(h.get("hooks", [{}])[0].get("command", ""))
            for entries in settings.get("hooks", {}).values()
            if isinstance(entries, list)
            for h in entries
            if isinstance(h, dict)
        ]
        assert all("hybrid" not in c and "wiki" not in c and "qa-hook" not in c for c in remaining)
        assert any("user-hook" in c for c in remaining)
        skills = fakehome / ".claude" / "skills"
        assert not any(
            (skills / n / "skill.md").exists()
            for n in ("search", "maintain", "rebuild-index", "save-wiki", "bootstrap-wiki", "setup-hybrid-search")
        )

    def test_teardown_on_clean_machine_is_noop(self, tmp_path: Path, monkeypatch, capsys) -> None:
        from hybrid_search.cli import cmd_teardown

        monkeypatch.setenv("HOME", str(tmp_path))
        cmd_teardown(SimpleNamespace())
        assert "Nothing to remove" in capsys.readouterr().out

    def test_preexisting_user_skill_is_backed_up_and_restored(self, tmp_path: Path, monkeypatch) -> None:
        # "search" is a generic name. If the user already had their own
        # ~/.claude/skills/search/skill.md, setup must back it up and
        # teardown must restore it — not delete it.
        from hybrid_search.cli import cmd_setup, cmd_teardown

        fakehome = tmp_path / "home"
        skill_dir = fakehome / ".claude" / "skills" / "search"
        skill_dir.mkdir(parents=True)
        user_content = "---\nname: search\n---\nMY OWN SEARCH SKILL\n"
        (skill_dir / "skill.md").write_text(user_content, encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))

        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))
        installed = (skill_dir / "skill.md").read_text(encoding="utf-8")
        assert installed != user_content  # ours is active
        assert (skill_dir / "skill.md.pre-memory-layer").exists()

        cmd_teardown(SimpleNamespace())
        assert (skill_dir / "skill.md").read_text(encoding="utf-8") == user_content
        assert not (skill_dir / "skill.md.pre-memory-layer").exists()

    def test_user_modified_skill_is_kept_on_teardown(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # The user edited our installed skill afterwards — content no longer
        # matches the manifest SHA, so teardown must not delete their work.
        from hybrid_search.cli import cmd_setup, cmd_teardown

        fakehome = tmp_path / "home"
        fakehome.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))
        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))

        skill_md = fakehome / ".claude" / "skills" / "search" / "skill.md"
        skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nUSER EDIT\n", encoding="utf-8")

        cmd_teardown(SimpleNamespace())
        assert skill_md.exists()
        assert "USER EDIT" in skill_md.read_text(encoding="utf-8")
        assert "Kept skill 'search'" in capsys.readouterr().out

    def test_preexisting_user_hook_mentioning_stale_md_survives_setup_and_teardown(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The user hook exists BEFORE setup: the reinstall filter in
        # cmd_setup must not sweep it away on the broad "STALE.md" needle
        # (the teardown-only test never exercised that path), and teardown
        # must keep it too — both go through _is_memory_layer_hook.
        from hybrid_search.cli import cmd_setup, cmd_teardown

        fakehome = tmp_path / "home"
        settings_path = fakehome / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        user_hook = {
            "matcher": "Edit",
            "hooks": [{"type": "command", "command": 'cat "$PROJECT/docs/STALE.md"'}],
        }
        settings_path.write_text(
            json.dumps({"hooks": {"PreToolUse": [user_hook]}}), encoding="utf-8"
        )
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))

        def _user_hook_present() -> bool:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            return any(
                'docs/STALE.md' in str(h.get("hooks", [{}])[0].get("command", ""))
                for entries in settings.get("hooks", {}).values()
                if isinstance(entries, list)
                for h in entries
                if isinstance(h, dict)
            )

        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))
        assert _user_hook_present(), "setup deleted the user's pre-existing hook"

        cmd_teardown(SimpleNamespace())
        assert _user_hook_present(), "teardown deleted the user's pre-existing hook"

    def test_user_hook_mentioning_stale_md_survives_teardown(self, tmp_path: Path, monkeypatch) -> None:
        # Ownership must key on OUR distinctive command substrings
        # (.hybrid-search/... paths, module invocations) — a user's own hook
        # that happens to reference a file named STALE.md is not ours.
        from hybrid_search.cli import cmd_setup, cmd_teardown

        fakehome = tmp_path / "home"
        fakehome.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))
        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))

        settings_path = fakehome / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        user_hook = {
            "matcher": "Edit",
            "hooks": [{"type": "command", "command": 'cat "$PROJECT/docs/STALE.md"'}],
        }
        settings["hooks"].setdefault("PreToolUse", []).append(user_hook)
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        cmd_teardown(SimpleNamespace())

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        commands = [
            str(h.get("hooks", [{}])[0].get("command", ""))
            for entries in settings.get("hooks", {}).values()
            if isinstance(entries, list)
            for h in entries
            if isinstance(h, dict)
        ]
        assert any('docs/STALE.md' in c for c in commands)
        assert all(".hybrid-search/wiki" not in c and "qa-hook" not in c for c in commands)

    def test_user_files_inside_skill_dir_survive_teardown(self, tmp_path: Path, monkeypatch) -> None:
        # We own skill.md, not the directory: notes the user parked next to
        # it must survive, and only the skill.md we installed goes away.
        from hybrid_search.cli import cmd_setup, cmd_teardown

        fakehome = tmp_path / "home"
        fakehome.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))
        cmd_setup(SimpleNamespace(cwd=str(project), dry_run=False, force=False, global_only=True))

        skill_dir = fakehome / ".claude" / "skills" / "search"
        (skill_dir / "my-notes.md").write_text("keep me", encoding="utf-8")

        cmd_teardown(SimpleNamespace())

        assert not (skill_dir / "skill.md").exists()
        assert (skill_dir / "my-notes.md").read_text(encoding="utf-8") == "keep me"

    def test_foreign_mcp_registration_is_kept(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json
        from hybrid_search.cli import cmd_teardown

        fakehome = tmp_path / "home"
        fakehome.mkdir()
        monkeypatch.setenv("HOME", str(fakehome))
        (fakehome / ".claude.json").write_text(_json.dumps({
            "mcpServers": {"hybrid-search": {"command": "someone-elses-binary", "args": []}}
        }), encoding="utf-8")

        cmd_teardown(SimpleNamespace())
        data = _json.loads((fakehome / ".claude.json").read_text(encoding="utf-8"))
        assert "hybrid-search" in data["mcpServers"]
