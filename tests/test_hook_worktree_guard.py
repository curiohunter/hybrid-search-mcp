"""Git hooks must belong to the main checkout, and merges must be seen.

Two holes cost real money before they were found.

A linked worktree shares `.git/hooks` with its parent, and the hooks
resolved their project with `--show-toplevel` — the worktree's own path.
Since a project id is a hash of that path, every worktree registered as a
*separate project* and was indexed from scratch: a full second copy of the
tree, embedded again. Eight ghost projects accumulated here, and two of
them (12,692 and 13,099 chunks) were re-embedded in a single day.

Separately, git does not run `post-commit` for a merge commit — it runs
`post-merge`, which was never installed. Merging a branch and pulling a
collaborator's work, the two events that actually move a branch forward,
left the index untouched. A later `--git-delta` covers only its own
commit, so those files were never picked up at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_search import cli

BUILDERS = {
    "post-commit": cli._build_post_commit_script,
    "post-checkout": cli._build_post_checkout_script,
    "post-merge": cli._build_post_merge_script,
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
class TestEveryHook:
    def test_refuses_to_run_inside_a_linked_worktree(self, name):
        body = BUILDERS[name](Path("/venv/python"))
        assert "--git-common-dir" in body
        assert '"$GIT_DIR" != "$COMMON_DIR"' in body

    def test_resolves_the_project_after_the_guard(self, name):
        """Order matters: bail out first, then decide what to index."""
        body = BUILDERS[name](Path("/venv/python"))
        assert body.index("COMMON_DIR") < body.index("PROJECT_DIR=")

    def test_carries_a_version_marker(self, name):
        assert cli._HOOK_VERSION_MARKER in BUILDERS[name](Path("/venv/python"))


class TestPostMergeExists:
    def test_it_reindexes(self):
        body = cli._build_post_merge_script(Path("/venv/python"))
        assert "hybrid_search.cli reindex" in body

    def test_it_shares_the_reindex_lock(self):
        """post-commit may already be running; they must not collide."""
        body = cli._build_post_merge_script(Path("/venv/python"))
        assert ".reindex.lock" in body

    def test_it_does_not_use_git_delta(self):
        """A merge can move any number of files and has no single diff to
        follow; a full scan skips unchanged files by hash anyway."""
        assert "--git-delta" not in cli._build_post_merge_script(Path("/venv/python"))


class TestStaleHooksGetRefreshed:
    def _install(self, tmp_path, body):
        h = tmp_path / "post-commit"
        return h, cli._install_hook_file(h, body, section_header="X")

    def test_a_fresh_install_is_written(self, tmp_path):
        h, status = self._install(tmp_path, cli._build_post_commit_script(Path("/v")))
        assert status == "installed" and h.exists()

    def test_the_same_version_is_left_alone(self, tmp_path):
        body = cli._build_post_commit_script(Path("/v"))
        h, _ = self._install(tmp_path, body)
        assert cli._install_hook_file(h, body, section_header="X") == "already-installed"

    def test_an_older_generation_is_replaced(self, tmp_path):
        """The bug this guards: the installer saw its own marker, said
        'already-installed', and left the stale script forever — so a fix
        to the hook body reached only fresh checkouts."""
        h = tmp_path / "post-commit"
        h.write_text("#!/bin/bash\n# hybrid-search-mcp:post-commit — old\nhybrid_search.cli x\n")
        status = cli._install_hook_file(
            h, cli._build_post_commit_script(Path("/v")), section_header="X"
        )
        assert status == "updated"
        assert cli._HOOK_VERSION_MARKER in h.read_text()
        assert "--git-common-dir" in h.read_text()

    def test_a_foreign_hook_keeps_its_own_content(self, tmp_path):
        """Husky and friends own the file; we only own our appended tail."""
        h = tmp_path / "post-commit"
        h.write_text("#!/bin/sh\nnpx husky run\n")
        status = cli._install_hook_file(
            h, cli._build_post_commit_script(Path("/v")), section_header="X"
        )
        assert status == "appended"
        assert "npx husky run" in h.read_text()

    # The pre-v2 banner. It predates the version marker, so a test that
    # only ever wrote "# hybrid-search-mcp:" never exercised this shape.
    _V1 = (
        "#!/bin/bash\n"
        "# Hybrid Search — auto delta-reindex on commit (background, non-blocking)\n"
        '"/gone/uv/tools/hybrid-search-mcp/bin/python" -m hybrid_search.cli reindex\n'
    )

    def test_the_pre_v2_banner_is_replaced_not_appended_to(self, tmp_path):
        """The v1 body was misread as a stranger's hook and kept. Both
        bodies then wrote the same lock file — the dead one could take the
        lock and make the live one skip its reindex — and the dead one
        still called a uv tool path that had since been renamed."""
        h = tmp_path / "post-commit"
        h.write_text(self._V1)
        status = cli._install_hook_file(
            h, cli._build_post_commit_script(Path("/v")), section_header="X"
        )
        assert status == "updated"
        body = h.read_text()
        assert "/gone/uv/tools" not in body
        assert body.count("LOCK_FILE=") == 1

    def test_an_already_duplicated_hook_heals(self, tmp_path):
        """Repairing installs that the bug already wrote: the file carries
        our section header, yet every line in it is still ours."""
        h = tmp_path / "post-commit"
        h.write_text(
            self._V1
            + "\n# --- X ---\n"
            + cli._build_post_commit_script(Path("/old")).split("\n", 1)[1]
        )
        status = cli._install_hook_file(
            h, cli._build_post_commit_script(Path("/v")), section_header="X"
        )
        assert status == "updated"
        body = h.read_text()
        assert "/gone/uv/tools" not in body
        assert "/old" not in body
        assert body.count("LOCK_FILE=") == 1

    def test_a_foreign_hook_with_our_old_section_is_refreshed_in_place(self, tmp_path):
        """The healing must not reach past our own section header: the
        stranger's lines survive, our stale ones do not."""
        h = tmp_path / "post-commit"
        h.write_text(
            "#!/bin/sh\nnpx husky run\n\n# --- X ---\n"
            '"/gone/uv/tools/hybrid-search-mcp/bin/python" -m hybrid_search.cli reindex\n'
        )
        status = cli._install_hook_file(
            h, cli._build_post_commit_script(Path("/v")), section_header="X"
        )
        assert status == "updated"
        body = h.read_text()
        assert "npx husky run" in body
        assert "/gone/uv/tools" not in body
        assert body.count("LOCK_FILE=") == 1

    def test_a_foreign_hook_holding_the_current_section_is_left_alone(self, tmp_path):
        """Version marker present and the head is not ours — nothing to do.
        This is the cheap exit the duplicate check must not swallow."""
        h = tmp_path / "post-commit"
        h.write_text(
            "#!/bin/sh\nnpx husky run\n\n# --- X ---\n"
            + cli._build_post_commit_script(Path("/v")).split("\n", 1)[1]
        )
        status = cli._install_hook_file(
            h, cli._build_post_commit_script(Path("/v")), section_header="X"
        )
        assert status == "already-installed"
        assert "npx husky run" in h.read_text()


class TestOurOwnBodyBehindAStrangersBanner:
    """The guard shipped, the install said "already-installed", and the
    un-guarded body kept running.

    Ownership was judged on one line: the comment right after the shebang.
    In a hook whose first block belongs to someone else — a project's own
    tooling, Husky, anything that got there first — our pre-v2 section sat
    *behind* that line, so the whole head read as a stranger's and was
    copied forward on every upgrade. The result was a repo where every
    commit in a linked worktree still fired a reindex of the main project:
    two of them were found running at once, one for an hour.
    """

    HEADER = "Hybrid Search auto-reindex (post-commit)"

    _STRANGER = (
        "#!/bin/sh\n"
        "\n"
        "# Somebody else's hook\n"
        "other_tool_update() {\n"
        "  other-tool update --quiet &\n"
        "}\n"
        "other_tool_update\n"
    )
    # Our v1 section: appended under its own header, no worktree guard.
    _OURS_V1 = (
        "\n# --- Hybrid Search auto-reindex ---\n"
        'PROJECT_DIR="$(git rev-parse --show-toplevel)"\n'
        'LOCK_FILE="$PROJECT_DIR/.hybrid-search/.reindex.lock"\n'
        '"/old/python" -m hybrid_search.cli reindex --git-delta '
        '--cwd "$PROJECT_DIR" || true\n'
    )

    def _current(self):
        return cli._build_post_commit_script(Path("/v"))

    def _install(self, tmp_path, text):
        h = tmp_path / "post-commit"
        h.write_text(text)
        status = cli._install_hook_file(h, self._current(), section_header=self.HEADER)
        return h, status

    def test_the_stale_body_is_removed_from_under_the_current_one(self, tmp_path):
        h, status = self._install(
            tmp_path,
            self._STRANGER
            + self._OURS_V1
            + f"\n# --- {self.HEADER} ---\n"
            + self._current().split("\n", 1)[1],
        )
        body = h.read_text()
        assert status == "updated"
        assert body.count("hybrid_search.cli reindex") == 1
        assert "/old/python" not in body
        assert body.count("--git-common-dir") == 1, "the guarded body must be the only one"

    def test_the_strangers_lines_survive_the_repair(self, tmp_path):
        h, _ = self._install(
            tmp_path,
            self._STRANGER
            + self._OURS_V1
            + f"\n# --- {self.HEADER} ---\n"
            + self._current().split("\n", 1)[1],
        )
        body = h.read_text()
        assert "other_tool_update" in body
        assert body.startswith("#!/bin/sh")

    def test_the_stale_body_alone_is_replaced_not_doubled(self, tmp_path):
        """Same file without the current section: still one body afterwards."""
        h, status = self._install(tmp_path, self._STRANGER + self._OURS_V1)
        body = h.read_text()
        assert status == "updated"
        assert body.count("hybrid_search.cli reindex") == 1
        assert "other_tool_update" in body
        assert "--git-common-dir" in body

    def test_a_strangers_own_section_is_not_mistaken_for_ours(self, tmp_path):
        """Other tools use `# --- ... ---` headers too. Only sections that
        name us, or call our CLI, may be cut."""
        h, _ = self._install(
            tmp_path,
            "#!/bin/sh\n\n# --- Some Other Tool ---\nrun-other-tool\n" + self._OURS_V1,
        )
        body = h.read_text()
        assert "# --- Some Other Tool ---" in body
        assert "run-other-tool" in body
        assert body.count("hybrid_search.cli reindex") == 1

    def test_reinstalling_the_repaired_hook_changes_nothing(self, tmp_path):
        h, _ = self._install(tmp_path, self._STRANGER + self._OURS_V1)
        before = h.read_text()
        status = cli._install_hook_file(h, self._current(), section_header=self.HEADER)
        assert status == "already-installed"
        assert h.read_text() == before


class TestTheLockAndTheWriteNameTheSameProject:
    """Standing in a worktree must not change which project is locked.

    The pipeline canonicalises a linked worktree to its main checkout;
    `cmd_reindex` resolved the path itself and did not. So a reindex fired
    from a worktree took the writer lock on hash(worktree) and then wrote
    hash(main) — the lock guarded an index nobody was writing. A second
    indexer, started from the main checkout, saw that index unlocked and
    walked straight in: two processes writing one index, found on
    2026-09-17. The stray hash also got a real directory on disk,
    registered to no project — a ghost index left behind by every
    worktree reindex.
    """

    def _worktree(self, tmp_path):
        main = tmp_path / "repo"
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        tree = tmp_path / "repo-wt"
        tree.mkdir()
        (tree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
        return main, tree

    def test_a_worktree_locks_the_main_checkouts_project(self, tmp_path, monkeypatch):
        import argparse

        main, tree = self._worktree(tmp_path)
        seen: dict[str, str] = {}

        def _get_project_dir(projects_dir, pid):
            seen.setdefault("lock_id", pid)
            d = tmp_path / "idx" / pid
            d.mkdir(parents=True, exist_ok=True)
            return d

        monkeypatch.setattr(
            "hybrid_search.storage.indexes.get_project_dir", _get_project_dir
        )
        monkeypatch.setattr(cli, "load_config", lambda: _StubConfig(tmp_path))
        monkeypatch.setattr(cli, "ProjectRegistry", lambda *a, **k: _StubRegistry())

        def _body(args, config, registry, project_path, project_name, cwd):
            seen["written_path"] = project_path

        monkeypatch.setattr(cli, "_reindex_locked", _body)

        cli.cmd_reindex(
            argparse.Namespace(cwd=str(tree), force=False, include_content=False)
        )

        from hybrid_search.project import project_hash

        assert seen["written_path"] == str(main.resolve()), "wrote the worktree, not main"
        assert seen["lock_id"] == project_hash(str(main.resolve())), (
            "locked a different project than the one being written"
        )


class _StubConfig:
    def __init__(self, tmp_path):
        self.global_dir = tmp_path / "g"
        self.projects_dir = tmp_path / "idx"
        self.indexing = None
        self.embedding = None
        self.models_dir = tmp_path / "m"


class _StubRegistry:
    def list_all(self):
        return []
