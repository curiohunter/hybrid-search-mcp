"""`status` must notice an index that holds almost none of its project.

Three projects here had silently collapsed — 571 chunks for 1,767 files in
one case — and nothing reported it. From the outside that is indistinguishable
from bad search quality, so it went unnoticed until a forced rebuild grew the
index 22x.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import count_indexable_files


class TestCountIndexableFiles:
    def test_counts_supported_extensions_only(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        (tmp_path / "notes.bin").write_bytes(b"\x00\x01")
        cfg = IndexingConfig(supported_extensions=[".py"])
        assert count_indexable_files(tmp_path, cfg) == 2

    def test_honours_exclude_patterns(self, tmp_path):
        (tmp_path / "keep.py").write_text("x = 1\n")
        vendor = tmp_path / "node_modules"
        vendor.mkdir()
        (vendor / "dep.py").write_text("y = 2\n")
        cfg = IndexingConfig(
            supported_extensions=[".py"], exclude_patterns=["node_modules"]
        )
        assert count_indexable_files(tmp_path, cfg) == 1


def _store(tmp_path, paths: list[str]):
    db = tmp_path / "store.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE files (relative_path TEXT)")
    conn.executemany("INSERT INTO files VALUES (?)", [(p,) for p in paths])
    conn.commit()
    conn.close()
    return tmp_path


def _run(capsys, tmp_path, indexed: list[str], on_disk: int):
    from hybrid_search import cli

    _store(tmp_path, indexed)
    pinfo = MagicMock(id="pid", file_count=len(indexed))
    config = MagicMock()
    with patch(
        "hybrid_search.index.scanner.count_indexable_files", return_value=on_disk
    ), patch("hybrid_search.storage.indexes.get_project_dir", return_value=tmp_path):
        cli._print_index_coverage(config, pinfo, tmp_path)
    return capsys.readouterr().out


class TestCoverageReport:
    def test_collapsed_index_warns_and_names_the_fix(self, capsys, tmp_path):
        out = _run(capsys, tmp_path, [f"src/f{i}.py" for i in range(20)], on_disk=1767)
        assert "⚠" in out and "--force" in out

    def test_warning_says_it_is_not_a_small_project(self, capsys, tmp_path):
        out = _run(capsys, tmp_path, [f"src/f{i}.py" for i in range(20)], on_disk=1767)
        assert "not a small project" in out

    def test_healthy_index_is_reported_without_alarm(self, capsys, tmp_path):
        out = _run(capsys, tmp_path, [f"src/f{i}.py" for i in range(90)], on_disk=100)
        assert "✓" in out and "⚠" not in out

    def test_partial_coverage_from_exclusions_is_not_an_alarm(self, capsys, tmp_path):
        """The scanner walks files the pipeline later drops for language,
        size, or content rules — measured healthy coverage runs as low as
        31%, so the bar has to sit under that."""
        out = _run(capsys, tmp_path, [f"src/f{i}.py" for i in range(31)], on_disk=100)
        assert "⚠" not in out

    def test_tiny_projects_are_skipped(self, capsys, tmp_path):
        """A ratio over a handful of files says nothing."""
        out = _run(capsys, tmp_path, ["src/a.py"], on_disk=10)
        assert out.strip() == ""

    def test_a_broken_scan_does_not_break_status(self, capsys, tmp_path):
        from hybrid_search import cli

        with patch(
            "hybrid_search.index.scanner.count_indexable_files",
            side_effect=OSError("boom"),
        ):
            cli._print_index_coverage(MagicMock(), MagicMock(id="p"), tmp_path)
        assert capsys.readouterr().out.strip() == ""
