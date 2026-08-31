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
