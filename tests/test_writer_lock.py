"""One IndexWriter per project index — every writing command must queue.

Tantivy allows a single IndexWriter per directory. Conversation indexing
took a PID lock for that reason; `index` and `reindex` never did, and the
`.reindex.lock` the git post-commit hook writes is read only by the hook's
own shell wrapper. A manual `index --force` therefore started straight on
top of a hook-triggered reindex and died partway with "Failed to acquire
Lockfile", leaving the run aborted. That happened twice in one session.
"""

from __future__ import annotations

import os

import pytest

from hybrid_search import cli


class TestWriterLock:
    def test_lock_lives_beside_the_project_index(self, tmp_path):
        assert cli._writer_lock_path(tmp_path).parent == tmp_path

    def test_a_live_holder_is_reported(self, tmp_path):
        lock = tmp_path / ".writer.lock"
        lock.write_text("1")  # pid 1 is always alive
        assert cli._lock_holder(lock) == 1

    def test_our_own_pid_does_not_block_us(self, tmp_path):
        """Re-entering the same process must not deadlock the run."""
        lock = tmp_path / ".writer.lock"
        lock.write_text(str(os.getpid()))
        assert cli._lock_holder(lock) is None

    def test_a_dead_holder_is_ignored(self, tmp_path):
        """A crashed indexer must not lock the project out forever."""
        lock = tmp_path / ".writer.lock"
        lock.write_text("999999")
        assert cli._lock_holder(lock) is None

    def test_garbage_contents_are_ignored(self, tmp_path):
        lock = tmp_path / ".writer.lock"
        lock.write_text("not-a-pid")
        assert cli._lock_holder(lock) is None

    def test_acquire_refuses_while_another_process_holds_it(self, tmp_path):
        lock = tmp_path / ".writer.lock"
        lock.write_text("1")
        assert cli._acquire_conv_lock(lock) is False

    def test_acquire_succeeds_when_free(self, tmp_path):
        lock = tmp_path / ".writer.lock"
        assert cli._acquire_conv_lock(lock) is True
        assert lock.read_text().strip() == str(os.getpid())


class TestReindexRefusesToDoubleWrite:
    def test_reindex_exits_without_touching_the_index(self, tmp_path, monkeypatch, capsys):
        """The point is that it stops *before* the pipeline runs — a partial
        write is worse than a refused one."""
        import argparse

        project = tmp_path / "repo"
        project.mkdir()
        index_dir = tmp_path / "idx"
        index_dir.mkdir()
        (index_dir / ".writer.lock").write_text("1")

        monkeypatch.setattr(cli, "get_project_dir", lambda *a, **k: index_dir, raising=False)
        monkeypatch.setattr(
            "hybrid_search.storage.indexes.get_project_dir", lambda *a, **k: index_dir
        )

        def _boom(*a, **k):
            raise AssertionError("pipeline must not run while locked")

        monkeypatch.setattr(cli, "_reindex_locked", _boom)

        args = argparse.Namespace(cwd=str(project), force=False, include_content=False)
        with pytest.raises(SystemExit) as exc:
            cli.cmd_reindex(args)
        assert exc.value.code == 1
        assert "PID 1" in capsys.readouterr().out


class TestPidLiveness:
    def test_permission_denied_means_alive_not_dead(self):
        """os.kill raises PermissionError for a process we may not signal.
        Reading that as "dead" hands the lock to a second writer while the
        first is still holding the Tantivy directory open."""
        assert cli._pid_alive(1) is True

    def test_missing_process_is_dead(self):
        assert cli._pid_alive(999999) is False
