"""Tests for the Memory Layer reader — Sprint 2 (qa list/show/grep/stats)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hybrid_search.memory import qa_log, reader


def _write_log(
    project_root: Path,
    query: str,
    *,
    query_type: str = "NL_EN",
    bm25: float = 0.4,
    time_ms: float = 10.0,
    chunks: int = 100,
) -> Path:
    """Drive the real writer (sync) so tests exercise the exact on-disk format."""
    resp = SimpleNamespace(
        results=[
            SimpleNamespace(
                chunk_id="c1",
                file_path="a.py",
                project="p",
                name="f",
                qualified_name="a.f",
                node_type="function",
                start_line=1,
                end_line=3,
                snippet="hello world",
            )
        ],
        query_type=query_type,
        effective_bm25_weight=bm25,
        query_time_ms=time_ms,
        total_chunks_searched=chunks,
    )
    prev = os.environ.get(qa_log.ENV_TOGGLE)
    os.environ[qa_log.ENV_TOGGLE] = "1"
    try:
        path = qa_log.record(
            query=query,
            response=resp,
            cwd=str(project_root),
            async_write=False,
        )
    finally:
        if prev is None:
            os.environ.pop(qa_log.ENV_TOGGLE, None)
        else:
            os.environ[qa_log.ENV_TOGGLE] = prev
    assert path is not None, "writer should return a path in sync mode"
    return path


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".hybrid-search").mkdir()
    return tmp_path


class TestParseQAIndex:
    def test_parses_writer_output(self, project_root: Path) -> None:
        p = _write_log(project_root, "hello world")
        idx = reader.parse_qa_index(p)
        assert idx is not None
        assert idx.query == "hello world"
        assert idx.query_type == "NL_EN"
        assert idx.effective_bm25_weight == pytest.approx(0.4)
        assert idx.result_count == 1
        assert idx.timestamp is not None and idx.timestamp.tzinfo is not None

    def test_unescapes_quotes_and_backslashes(self, project_root: Path) -> None:
        p = _write_log(project_root, 'weird "quoted" \\ path')
        idx = reader.parse_qa_index(p)
        assert idx is not None
        assert idx.query == 'weird "quoted" \\ path'

    def test_missing_frontmatter_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "junk.md"
        p.write_text("no frontmatter here", encoding="utf-8")
        assert reader.parse_qa_index(p) is None

    def test_malformed_frontmatter_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.md"
        p.write_text("---\nno_query_field: x\n---\n# body\n", encoding="utf-8")
        assert reader.parse_qa_index(p) is None

    def test_id_encodes_year_month(self, project_root: Path) -> None:
        p = _write_log(project_root, "q1")
        idx = reader.parse_qa_index(p)
        assert idx is not None
        # Friendly id: YYYY-MM-<stem>
        parts = idx.id.split("-")
        assert len(parts) >= 5
        assert parts[0].isdigit() and len(parts[0]) == 4
        assert parts[1].isdigit() and len(parts[1]) == 2


class TestIterAndList:
    def test_empty_dir_yields_nothing(self, project_root: Path) -> None:
        assert list(reader.iter_qa_files(project_root)) == []
        assert list(reader.iter_qa_indexes(project_root)) == []

    def test_newest_first_order(self, project_root: Path) -> None:
        p1 = _write_log(project_root, "older")
        # Force a distinguishable mtime; the writer uses now() so same-second
        # writes could collide on some filesystems.
        os.utime(p1, (1_700_000_000, 1_700_000_000))
        p2 = _write_log(project_root, "newer")
        os.utime(p2, (1_800_000_000, 1_800_000_000))
        ordered = list(reader.iter_qa_files(project_root))
        assert ordered == [p2, p1]

    def test_skips_unparseable_files(self, project_root: Path) -> None:
        good = _write_log(project_root, "good")
        bad_dir = good.parent
        (bad_dir / "garbage.md").write_text("not a log", encoding="utf-8")
        ids = [i.path.name for i in reader.iter_qa_indexes(project_root)]
        assert good.name in ids
        assert "garbage.md" not in ids


class TestFindById:
    def test_find_by_full_friendly_id(self, project_root: Path) -> None:
        p = _write_log(project_root, "find me")
        idx = reader.parse_qa_index(p)
        assert idx is not None
        found = reader.find_qa_by_id(project_root, idx.id)
        assert found is not None and found.path == p

    def test_find_by_hash_prefix(self, project_root: Path) -> None:
        p = _write_log(project_root, "prefix match")
        idx = reader.parse_qa_index(p)
        assert idx is not None and idx.hash
        found = reader.find_qa_by_id(project_root, idx.hash[:4])
        assert found is not None and found.path == p

    def test_find_by_stem(self, project_root: Path) -> None:
        p = _write_log(project_root, "by stem")
        found = reader.find_qa_by_id(project_root, p.stem)
        assert found is not None and found.path == p

    def test_missing_returns_none(self, project_root: Path) -> None:
        _write_log(project_root, "q")
        assert reader.find_qa_by_id(project_root, "zzzzzzzz") is None

    def test_empty_token_returns_none(self, project_root: Path) -> None:
        _write_log(project_root, "q")
        assert reader.find_qa_by_id(project_root, "") is None


class TestGrep:
    def test_hits_frontmatter_and_body(self, project_root: Path) -> None:
        _write_log(project_root, "authority_alpha config")
        hits = list(reader.grep_qa(project_root, "authority_alpha"))
        assert any("query:" in h.line for h in hits)
        assert any(h.line.startswith("# Q:") for h in hits)

    def test_case_insensitive_by_default(self, project_root: Path) -> None:
        _write_log(project_root, "Authority Alpha")
        hits = list(reader.grep_qa(project_root, "authority"))
        assert hits, "should match regardless of case"

    def test_case_sensitive_flag(self, project_root: Path) -> None:
        _write_log(project_root, "Authority Alpha")
        strict = list(
            reader.grep_qa(project_root, "authority", case_insensitive=False)
        )
        assert strict == []

    def test_empty_term_yields_nothing(self, project_root: Path) -> None:
        _write_log(project_root, "q")
        assert list(reader.grep_qa(project_root, "")) == []


class TestReadBody:
    def test_body_excludes_frontmatter(self, project_root: Path) -> None:
        p = _write_log(project_root, "body test")
        body = reader.read_qa_body(p)
        assert body.startswith("# Q: body test")
        assert "query_type:" not in body.split("\n")[0]

    def test_read_missing_path_returns_empty(self, tmp_path: Path) -> None:
        assert reader.read_qa_body(tmp_path / "nope.md") == ""
