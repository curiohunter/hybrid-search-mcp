"""Tests for the Memory Layer reader — Sprint 2/4 (list/show/grep/stats/prune)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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


class TestParseDuration:
    @pytest.mark.parametrize(
        ("spec", "td"),
        [
            ("30d", timedelta(days=30)),
            ("12h", timedelta(hours=12)),
            ("2w", timedelta(weeks=2)),
            ("3m", timedelta(days=90)),
            ("  1d  ", timedelta(days=1)),
            ("5H", timedelta(hours=5)),  # case-insensitive
        ],
    )
    def test_units(self, spec: str, td: timedelta) -> None:
        assert reader.parse_duration(spec) == td

    @pytest.mark.parametrize("bad", ["", "30", "d30", "1.5d", "forever", "30 days"])
    def test_rejects_garbage(self, bad: str) -> None:
        with pytest.raises(ValueError):
            reader.parse_duration(bad)


class TestResolveCutoff:
    def test_older_than_subtracts_from_now(self) -> None:
        ref = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
        cutoff = reader.resolve_cutoff(older_than="10d", now=ref)
        assert cutoff == ref - timedelta(days=10)

    def test_before_parses_iso(self) -> None:
        cutoff = reader.resolve_cutoff(before="2026-01-15")
        assert cutoff == datetime(2026, 1, 15, tzinfo=timezone.utc)

    def test_before_naive_is_treated_as_utc(self) -> None:
        cutoff = reader.resolve_cutoff(before="2026-01-15T09:00:00")
        assert cutoff.tzinfo is not None
        assert cutoff.utcoffset() == timedelta(0)

    def test_requires_exactly_one_input(self) -> None:
        with pytest.raises(ValueError):
            reader.resolve_cutoff()
        with pytest.raises(ValueError):
            reader.resolve_cutoff(older_than="1d", before="2026-01-01")

    def test_bad_date_raises(self) -> None:
        with pytest.raises(ValueError):
            reader.resolve_cutoff(before="not-a-date")


class TestPruneOlderThan:
    def _age(self, path: Path, seconds_ago: float) -> None:
        target = datetime.now(timezone.utc).timestamp() - seconds_ago
        os.utime(path, (target, target))

    def test_dry_run_lists_without_deleting(self, project_root: Path) -> None:
        p = _write_log(project_root, "old")
        self._age(p, 3600 * 24 * 30)  # 30d old
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        result = reader.prune_older_than(project_root, cutoff, dry_run=True)
        assert [x.name for x in result.deleted] == [p.name]
        assert p.exists()  # untouched
        assert result.dirs_removed == []

    def test_deletes_expired_and_cleans_empty_dirs(self, project_root: Path) -> None:
        p = _write_log(project_root, "stale")
        self._age(p, 3600 * 24 * 30)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        result = reader.prune_older_than(project_root, cutoff)
        assert result.deleted and not p.exists()
        # YYYY/MM dirs should be rmdir'd since we removed the only entry.
        qa_root = reader.qa_dir(project_root)
        remaining = [x for x in qa_root.rglob("*") if x.is_file()]
        assert remaining == []
        assert any("04" in d.name for d in result.dirs_removed)

    def test_keeps_recent_entries(self, project_root: Path) -> None:
        fresh = _write_log(project_root, "fresh")
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        result = reader.prune_older_than(project_root, cutoff)
        assert result.deleted == []
        assert fresh.exists()

    def test_mixed_retention(self, project_root: Path) -> None:
        old = _write_log(project_root, "old")
        self._age(old, 3600 * 24 * 30)
        recent = _write_log(project_root, "recent")
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        result = reader.prune_older_than(project_root, cutoff)
        assert old not in list(project_root.rglob("*.md"))
        assert recent.exists()
        assert [x.name for x in result.deleted] == [old.name]

    def test_empty_dir_is_noop(self, project_root: Path) -> None:
        cutoff = datetime.now(timezone.utc)
        result = reader.prune_older_than(project_root, cutoff)
        assert result.deleted == []
        assert result.dirs_removed == []


class TestPruneKeepLatest:
    def _age(self, path: Path, seconds_ago: float) -> None:
        target = datetime.now(timezone.utc).timestamp() - seconds_ago
        os.utime(path, (target, target))

    def test_keep_all_when_under_ceiling(self, project_root: Path) -> None:
        for i in range(3):
            _write_log(project_root, f"q{i}")
        result = reader.prune_keep_latest(project_root, keep_n=5)
        assert result.deleted == []

    def test_drops_oldest_beyond_ceiling(self, project_root: Path) -> None:
        paths = []
        for i in range(4):
            p = _write_log(project_root, f"q{i}-unique-{i}")
            # Age each progressively older so ordering is deterministic
            self._age(p, 3600 * (4 - i))
            paths.append(p)
        # Newest is index 3 (age=3600s), oldest is index 0 (age=14400s)
        result = reader.prune_keep_latest(project_root, keep_n=2)
        surviving = {p.name for p in project_root.rglob("*.md")}
        assert len(surviving) == 2
        # The two newest (indices 2 and 3) should survive
        assert paths[2].name in surviving
        assert paths[3].name in surviving
        # Exactly 2 deletions; dry_run=False
        assert len(result.deleted) == 2

    def test_dry_run(self, project_root: Path) -> None:
        for i in range(3):
            p = _write_log(project_root, f"q{i}-x{i}")
            self._age(p, 3600 * (3 - i))
        result = reader.prune_keep_latest(project_root, keep_n=1, dry_run=True)
        assert len(result.deleted) == 2
        # Files still on disk
        assert len(list(project_root.rglob("*.md"))) == 3


class TestAutoPrune:
    def _age(self, path: Path, seconds_ago: float) -> None:
        target = datetime.now(timezone.utc).timestamp() - seconds_ago
        os.utime(path, (target, target))

    def test_age_ceiling_alone(self, project_root: Path) -> None:
        old = _write_log(project_root, "old-one")
        self._age(old, 3600 * 24 * 100)  # 100d
        recent = _write_log(project_root, "recent-one")
        result = reader.auto_prune(project_root, retention_days=90, max_files=None)
        assert old not in list(project_root.rglob("*.md"))
        assert recent.exists()
        assert len(result.deleted) == 1

    def test_count_ceiling_alone(self, project_root: Path) -> None:
        paths = []
        for i in range(5):
            p = _write_log(project_root, f"q{i}-tag{i}")
            self._age(p, 100 * (5 - i))
            paths.append(p)
        result = reader.auto_prune(project_root, retention_days=None, max_files=2)
        assert len(result.deleted) == 3
        remaining = {p.name for p in project_root.rglob("*.md")}
        assert len(remaining) == 2

    def test_both_ceilings_no_double_count(self, project_root: Path) -> None:
        # One very old, one recent. Age-prune removes the old one; count ceiling
        # would also select it — but dedupe keeps result.deleted at length 1.
        old = _write_log(project_root, "old-a")
        self._age(old, 3600 * 24 * 100)
        recent = _write_log(project_root, "recent-a")
        result = reader.auto_prune(project_root, retention_days=90, max_files=5)
        assert len(result.deleted) == 1
        assert old not in list(project_root.rglob("*.md"))
        assert recent.exists()

    def test_both_ceilings_apply_independently(self, project_root: Path) -> None:
        # 1 old beyond retention, 4 recent above count ceiling of 2.
        old = _write_log(project_root, "very-old")
        self._age(old, 3600 * 24 * 100)
        recent_paths = []
        for i in range(4):
            p = _write_log(project_root, f"fresh-{i}")
            self._age(p, 100 * (4 - i))
            recent_paths.append(p)
        result = reader.auto_prune(project_root, retention_days=90, max_files=2)
        # old deleted by age; then count drops 2 more of the recents.
        assert len(result.deleted) == 3
        remaining = {p.name for p in project_root.rglob("*.md")}
        assert len(remaining) == 2
        # The 2 freshest survive (indices 2, 3 of recent_paths)
        assert recent_paths[3].name in remaining
        assert recent_paths[2].name in remaining

    def test_dry_run_keeps_all_files(self, project_root: Path) -> None:
        for i in range(3):
            p = _write_log(project_root, f"q{i}-dry-{i}")
            self._age(p, 3600 * 24 * 200)  # all old
        result = reader.auto_prune(project_root, retention_days=90, max_files=1, dry_run=True)
        assert len(result.deleted) == 3
        assert len(list(project_root.rglob("*.md"))) == 3
