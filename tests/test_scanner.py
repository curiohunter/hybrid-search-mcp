"""Tests for file scanner — index/scanner.py (crash recovery, delta detection)."""

from unittest.mock import patch
from pathlib import Path

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import (
    _is_changed,
    compute_file_hash,
    get_changed_files_from_git,
    scan_project_subset,
)
from hybrid_search.storage.db import FileRecord, StoreDB


class TestIsChanged:
    """_is_changed() delta detection tests."""

    def test_empty_hash_triggers_reindex(self, tmp_path: Path) -> None:
        """file_hash="" (partial write from crash) should always return True."""
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="test.py",
            file_hash="",  # <-- crash marker
            file_size=int(f.stat().st_size),
            file_mtime=str(f.stat().st_mtime),
        )
        assert _is_changed(f, rec) is True

    def test_matching_hash_not_changed(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        real_hash = compute_file_hash(f)
        stat = f.stat()
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="test.py",
            file_hash=real_hash,
            file_size=stat.st_size,
            file_mtime=str(stat.st_mtime),
        )
        assert _is_changed(f, rec) is False

    def test_different_size_triggers_hash_check(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("print('hello')")
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="test.py",
            file_hash="fakehash",
            file_size=999,  # different size
            file_mtime=str(f.stat().st_mtime),
        )
        assert _is_changed(f, rec) is True

    def test_missing_file_returns_changed(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.py"
        rec = FileRecord(
            id="f1", project_id="p1", relative_path="gone.py",
            file_hash="abc",
        )
        assert _is_changed(missing, rec) is True


class TestGitDiff:
    def test_parses_name_status_output(self, tmp_path: Path) -> None:
        completed = type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": "A\tnew.py\nM\tsrc/app.py\nD\told.py\nR100\tbefore.py\tafter.py\n",
                "stderr": "",
            },
        )()
        with patch("hybrid_search.index.scanner.subprocess.run", return_value=completed):
            result = get_changed_files_from_git(tmp_path)

        assert result is not None
        assert result.added == ["new.py", "after.py"]
        assert result.modified == ["src/app.py"]
        assert result.deleted == ["old.py", "before.py"]
        assert result.renamed == [("before.py", "after.py")]


class TestSubsetScan:
    def test_detects_added_changed_deleted_from_subset(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        project_root = tmp_path / "project"
        project_root.mkdir()

        existing = project_root / "existing.py"
        existing.write_text("print('new')\n")
        added = project_root / "added.py"
        added.write_text("print('added')\n")

        with db.transaction() as conn:
            db.upsert_file(
                conn,
                FileRecord(
                    id="f-existing",
                    project_id="p1",
                    relative_path="existing.py",
                    file_hash="old-hash",
                    file_size=1,
                    file_mtime="0",
                    language="python",
                ),
            )
            db.upsert_file(
                conn,
                FileRecord(
                    id="f-deleted",
                    project_id="p1",
                    relative_path="deleted.py",
                    file_hash="old-hash",
                    file_size=1,
                    file_mtime="0",
                    language="python",
                ),
            )

        result = scan_project_subset(
            project_root,
            "p1",
            db,
            IndexingConfig(),
            changed_paths=["existing.py", "added.py", "ignored.txt"],
            deleted_paths=["deleted.py"],
        )

        assert [p.name for p in result.added] == ["added.py"]
        assert [p.name for p in result.changed] == ["existing.py"]
        assert result.deleted == ["deleted.py"]
