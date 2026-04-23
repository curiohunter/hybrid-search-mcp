"""Tests for ``hybrid_search.wiki_cleanup`` — DB-driven orphan detection.

Distinct from ``test_wiki_cleanup.py`` which covers the
``_cleanup_orphan_wiki_pages`` helper that purges renamed/merged module
pages during a full regeneration pass. This module verifies the v0.3.0
orphan detector that compares wiki ``## Files`` bullets against the store
DB's indexed-paths snapshot — catching gitignore drift and deletions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_search import wiki_cleanup


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    d = tmp_path / "wiki"
    d.mkdir()
    return d


def _write_page(wiki_dir: Path, name: str, file_refs: list[str]) -> Path:
    body = ["# " + name, "", "**Files**: " + str(len(file_refs)), "", "## Files", ""]
    body.extend(f"- `{ref}`" for ref in file_refs)
    body += ["", "## Symbols", "", "(body)"]
    path = wiki_dir / f"{name}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    return path


class TestExtractFileRefs:
    def test_extracts_bullet_paths(self, wiki_dir: Path) -> None:
        p = _write_page(wiki_dir, "foo", ["a.py", "b.ts"])
        refs = wiki_cleanup.extract_file_refs(p.read_text())
        assert refs == ["a.py", "b.ts"]

    def test_empty_when_no_files_section(self, wiki_dir: Path) -> None:
        (wiki_dir / "index.md").write_text("# Index\n\nno files here\n", encoding="utf-8")
        refs = wiki_cleanup.extract_file_refs((wiki_dir / "index.md").read_text())
        assert refs == []

    def test_stops_at_next_heading(self, wiki_dir: Path) -> None:
        body = "# x\n## Files\n\n- `a.py`\n\n## Symbols\n\n- `ignored.py`\n"
        assert wiki_cleanup.extract_file_refs(body) == ["a.py"]


class TestFindOrphans:
    def test_healthy_pages_kept(self, wiki_dir: Path) -> None:
        _write_page(wiki_dir, "alive", ["src/a.py", "src/b.ts"])
        orphans, scanned = wiki_cleanup.find_orphans(
            wiki_dir, {"src/a.py", "src/b.ts"}
        )
        assert orphans == []
        assert scanned == 1

    def test_zombie_detected(self, wiki_dir: Path) -> None:
        page = _write_page(wiki_dir, "dead", ["src/gone.py"])
        orphans, _ = wiki_cleanup.find_orphans(wiki_dir, {"src/other.py"})
        assert orphans == [page]

    def test_partial_stale_kept(self, wiki_dir: Path) -> None:
        """If even one referenced file survives, the page isn't an orphan."""
        page = _write_page(wiki_dir, "partial", ["src/alive.py", "src/dead.py"])
        orphans, _ = wiki_cleanup.find_orphans(wiki_dir, {"src/alive.py"})
        assert orphans == []
        assert page.exists()

    def test_empty_pages_preserved(self, wiki_dir: Path) -> None:
        (wiki_dir / "index.md").write_text("# Index\n", encoding="utf-8")
        orphans, _ = wiki_cleanup.find_orphans(wiki_dir, set())
        assert orphans == []

    def test_line_range_suffix_normalised(self, wiki_dir: Path) -> None:
        """Refs like ``src/foo.py:12-30`` strip the suffix before lookup."""
        page = _write_page(wiki_dir, "ranged", ["src/foo.py:12-30"])
        orphans, _ = wiki_cleanup.find_orphans(wiki_dir, {"src/foo.py"})
        assert orphans == []
        assert page.exists()


class TestCleanupOrphans:
    def test_dry_run_does_not_delete(self, wiki_dir: Path) -> None:
        page = _write_page(wiki_dir, "ghost", ["src/gone.py"])
        result = wiki_cleanup.cleanup_orphans(wiki_dir, set(), dry_run=True)
        assert result.orphans == [page]
        assert result.deleted == []
        assert page.exists()

    def test_actual_delete(self, wiki_dir: Path) -> None:
        page = _write_page(wiki_dir, "ghost", ["src/gone.py"])
        result = wiki_cleanup.cleanup_orphans(wiki_dir, set())
        assert result.deleted == [page]
        assert not page.exists()

    def test_survivor_untouched(self, wiki_dir: Path) -> None:
        alive = _write_page(wiki_dir, "alive", ["src/a.py"])
        dead = _write_page(wiki_dir, "dead", ["src/gone.py"])
        result = wiki_cleanup.cleanup_orphans(wiki_dir, {"src/a.py"})
        assert alive.exists()
        assert not dead.exists()
        assert result.scanned == 2
        assert result.deleted == [dead]

    def test_missing_wiki_dir_safe(self, tmp_path: Path) -> None:
        # Nonexistent wiki dir → scanned=0, orphans=[]. Shouldn't raise.
        result = wiki_cleanup.cleanup_orphans(tmp_path / "nowhere", set())
        assert result.scanned == 0
        assert result.orphans == []
        assert result.deleted == []
