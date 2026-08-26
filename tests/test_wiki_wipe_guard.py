"""Wiki cleanup must not wipe an entire wiki on a bad scan.

Reproduces a real loss: `index --force` ran against a project whose source
files had been deleted, so every one of its 526 synthesised wiki pages
looked orphaned and all 526 were unlinked. The pages were the last
surviving record of that project. Deleting everything is a symptom of a
broken scan, not an instruction to delete.
"""

from __future__ import annotations

from hybrid_search import wiki_cleanup


def _wiki(tmp_path, n: int):
    d = tmp_path / "wiki"
    d.mkdir()
    for i in range(n):
        (d / f"page{i}.md").write_text(
            f"# Page {i}\n\n## Files\n\n- `src/mod{i}.py`\n"
        )
    return d


class TestWipeGuard:
    def test_total_wipe_is_refused(self, tmp_path):
        d = _wiki(tmp_path, 20)
        result = wiki_cleanup.cleanup_orphans(d, indexed_paths=set())
        assert result.deleted == []
        assert result.refused
        assert len(list(d.glob("*.md"))) == 20

    def test_refusal_explains_the_likely_cause(self, tmp_path):
        d = _wiki(tmp_path, 20)
        result = wiki_cleanup.cleanup_orphans(d, indexed_paths=set())
        assert "sources are missing" in result.refused

    def test_allow_wipe_overrides(self, tmp_path):
        d = _wiki(tmp_path, 20)
        result = wiki_cleanup.cleanup_orphans(d, set(), allow_wipe=True)
        assert len(result.deleted) == 20
        assert result.refused is None

    def test_a_tiny_wiki_may_still_go_to_zero(self, tmp_path):
        """Under the floor the sweep is unremarkable and stays automatic."""
        d = _wiki(tmp_path, 3)
        result = wiki_cleanup.cleanup_orphans(d, set())
        assert len(result.deleted) == 3
        assert result.refused is None

    def test_partial_orphans_are_still_deleted(self, tmp_path):
        """A big refactor legitimately orphans most of a wiki — only the
        all-or-nothing case is suspicious."""
        d = _wiki(tmp_path, 20)
        keep = {"src/mod0.py", "src/mod1.py"}
        result = wiki_cleanup.cleanup_orphans(d, keep)
        assert result.refused is None
        assert len(result.deleted) == 18
        assert len(list(d.glob("*.md"))) == 2

    def test_structural_pages_do_not_mask_a_total_wipe(self, tmp_path):
        """The real wiki held index.md and STALE.md alongside 526 module
        pages. Those two have no `## Files` section and are always kept, so
        a ratio taken over every scanned page reads 526/528 and lets the
        wipe through. The guard counts only judgeable pages."""
        d = _wiki(tmp_path, 20)
        (d / "index.md").write_text("# Index\n\nno file bullets here\n")
        (d / "STALE.md").write_text("# Stale\n\nalso structural\n")
        result = wiki_cleanup.cleanup_orphans(d, indexed_paths=set())
        assert result.refused
        assert result.deleted == []
        assert len(list(d.glob("*.md"))) == 22
