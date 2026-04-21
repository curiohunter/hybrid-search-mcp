"""Wiki orphan cleanup (Phase 2 drift fix).

Verifies that a full wiki regeneration purges .md files left over from
previous runs (e.g. ``test_wiki-1..11.md`` after the fragmentation fix
merges them into a single ``test_wiki.md``).
"""

from __future__ import annotations

from pathlib import Path

from hybrid_search.cli import _cleanup_orphan_wiki_pages


def test_missing_dir_is_noop(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert _cleanup_orphan_wiki_pages(missing, {"index.md"}) == 0


def test_keeps_expected_files(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Index")
    (tmp_path / "auth.md").write_text("# Auth")
    expected = {"index.md", "auth.md"}
    removed = _cleanup_orphan_wiki_pages(tmp_path, expected)
    assert removed == 0
    assert {p.name for p in tmp_path.iterdir()} == expected


def test_removes_orphans(tmp_path: Path) -> None:
    # Simulate state after the test_wiki-1..11 fragmentation fix
    (tmp_path / "index.md").write_text("# Index")
    (tmp_path / "test_wiki.md").write_text("# test_wiki")
    for i in range(1, 12):
        (tmp_path / f"test_wiki-{i}.md").write_text(f"# orphan {i}")

    expected = {"index.md", "test_wiki.md"}
    removed = _cleanup_orphan_wiki_pages(tmp_path, expected)
    assert removed == 11
    remaining = {p.name for p in tmp_path.iterdir()}
    assert remaining == expected


def test_preserves_stale_md(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Index")
    (tmp_path / "STALE.md").write_text("# STALE")
    (tmp_path / "orphan.md").write_text("# orphan")

    removed = _cleanup_orphan_wiki_pages(tmp_path, {"index.md"})
    assert removed == 1
    assert (tmp_path / "STALE.md").exists()
    assert not (tmp_path / "orphan.md").exists()


def test_preserves_subdirectory_files(tmp_path: Path) -> None:
    """Synthesis staging dirs must not be touched."""
    (tmp_path / "index.md").write_text("# Index")
    syn = tmp_path / "_synthesis_input"
    syn.mkdir()
    (syn / "auth.md").write_text("# staging")
    (tmp_path / "orphan.md").write_text("# orphan")

    removed = _cleanup_orphan_wiki_pages(tmp_path, {"index.md"})
    assert removed == 1
    assert (syn / "auth.md").exists()
    assert not (tmp_path / "orphan.md").exists()


def test_ignores_non_md_files(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("# Index")
    (tmp_path / "coverage.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("notes")

    removed = _cleanup_orphan_wiki_pages(tmp_path, {"index.md"})
    assert removed == 0
    assert (tmp_path / "coverage.json").exists()
    assert (tmp_path / "notes.txt").exists()


def test_coverage_matches_wiki_files_after_cleanup(tmp_path: Path) -> None:
    """coverage.json total_pages should equal on-disk .md count after cleanup.

    The pre-fix symptom was total_pages=75 vs 98 on-disk files (orphan drift).
    """
    # Seed a post-regeneration state: 4 pages written + 3 orphans
    written = {"index.md", "auth.md", "user.md", "billing.md"}
    for name in written:
        (tmp_path / name).write_text(f"# {name}")
    for orphan in ("auth-1.md", "auth-2.md", "user-1.md"):
        (tmp_path / orphan).write_text("# orphan")

    _cleanup_orphan_wiki_pages(tmp_path, written)

    on_disk = {p.name for p in tmp_path.iterdir() if p.suffix == ".md"}
    # total_pages from coverage.json would be len(written) = 4
    total_pages = len(written)
    assert len(on_disk) == total_pages
    assert on_disk == written
