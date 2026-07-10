"""Local memory viewer — self-contained HTML, escaped content."""

from __future__ import annotations

from pathlib import Path

from hybrid_search.viewer import build_viewer_html, collect_viewer_data, write_viewer


def _seed(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    qa = root / ".hybrid-search" / "qa" / "2026" / "07"
    qa.mkdir(parents=True)
    (qa / "10-090000-abcd1234.md").write_text(
        '---\nquery: "환불 로직 <script>alert(1)</script> 어떻게 바뀌었어"\n'
        'query_type: "TURN"\ntimestamp: "2026-07-10T09:00:00+00:00"\n'
        'trigger: "stop_hook"\nclient: "claude"\n---\n\nbody\n',
        encoding="utf-8",
    )
    cards = root / ".hybrid-search" / "memory" / "cards" / "2026" / "07"
    cards.mkdir(parents=True)
    (cards / "10-090100-feed0001.md").write_text(
        '---\ntype: memory_card\nsummary: "형제할인은 둘째부터 10%"\n---\n\n본문\n',
        encoding="utf-8",
    )
    return root


class TestCollect:
    def test_collects_qa_and_cards(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        data = collect_viewer_data(root, "proj", {"코드 청크": 42})
        assert len(data.qa_entries) == 1
        assert "환불 로직" in data.qa_entries[0]["query"]
        assert data.qa_entries[0]["client"] == "claude"
        assert len(data.cards) == 1
        assert "형제할인" in data.cards[0]["summary"]
        assert data.stats == {"코드 청크": 42}

    def test_empty_project_is_fine(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        data = collect_viewer_data(root, "empty")
        assert data.qa_entries == [] and data.cards == []


class TestHtml:
    def test_self_contained_and_escaped(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        out = write_viewer(root, "proj", {"Q&A": 1})
        html_text = out.read_text(encoding="utf-8")
        # No external requests — self-contained.
        assert "http://" not in html_text.replace("http://www.w3.org", "")
        assert "<script src" not in html_text
        # User content is JSON-embedded, script tags never appear verbatim.
        assert "<script>alert(1)</script>" not in html_text
        assert "환불 로직" in html_text
        # Stats key with & is escaped in markup.
        assert "Q&amp;A" in html_text

    def test_written_into_hybrid_search_dir(self, tmp_path: Path) -> None:
        root = _seed(tmp_path)
        out = write_viewer(root, "proj")
        assert out == root / ".hybrid-search" / "viewer.html"
        assert out.exists()
