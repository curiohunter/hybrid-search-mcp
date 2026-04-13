"""Tests for file scanner — index/scanner.py (crash recovery, delta detection)."""

from pathlib import Path

from hybrid_search.index.scanner import _is_changed, compute_file_hash
from hybrid_search.storage.db import FileRecord


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
