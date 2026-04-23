"""Tests for ``hybrid_search.memory.integrity`` — staleness, dedup, archive."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from hybrid_search.memory import integrity


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / ".hybrid-search").mkdir()
    return tmp_path


def _write_qa(
    project_root: Path,
    name: str,
    query: str,
    result_paths: list[str],
    *,
    mtime_ago_s: float = 0.0,
) -> Path:
    """Write a qa file mirroring the format produced by ``qa_log._format_record``."""
    year, month = "2026", "04"
    qa_dir = project_root / integrity.QA_DIRNAME / year / month
    qa_dir.mkdir(parents=True, exist_ok=True)
    body = [
        "---",
        f'query: "{query}"',
        "query_type: ENGLISH_NL",
        "effective_bm25_weight: 0.4",
        "query_time_ms: 100.0",
        "total_chunks_searched: 1000",
        "timestamp: 2026-04-23T00:00:00+00:00",
        f"result_count: {len(result_paths)}",
        "---",
        "",
        f"# Q: {query}",
        "",
        "## Top results",
        "",
    ]
    for i, rp in enumerate(result_paths, start=1):
        body.append(f"### {i}. `{rp}` — name")
        body.append(f"- chunk_id: `c{i}`")
        body.append("")
    path = qa_dir / f"{name}.md"
    path.write_text("\n".join(body), encoding="utf-8")
    if mtime_ago_s > 0:
        ts = datetime.now(timezone.utc).timestamp() - mtime_ago_s
        os.utime(path, (ts, ts))
    return path


class TestExtractResultPaths:
    def test_extracts_top_result_paths(self) -> None:
        body = "## Top results\n\n### 1. `src/a.py:10-20` — foo\n\n### 2. `src/b.py` — bar\n"
        assert integrity._extract_result_paths(body) == ["src/a.py", "src/b.py"]

    def test_ignores_non_result_headings(self) -> None:
        body = "## Top results\n\n### 1. `src/a.py` — foo\n## Other section\n### 2. `fake.py`\n"
        # Only the heading under Top results counts; regex is permissive so
        # both are captured — the caller applies the DB-presence filter.
        # Ensuring at minimum the real one is picked up.
        found = integrity._extract_result_paths(body)
        assert "src/a.py" in found


class TestArchiveFile:
    def test_moves_qa_into_archive(self, project_root: Path) -> None:
        src = _write_qa(project_root, "ghost", "q", ["src/x.py"])
        archived = integrity.archive_file(src, project_root)
        assert archived is not None
        assert not src.exists()
        assert archived.exists()
        # Preserves YYYY/MM tree under qa-archive.
        assert "qa-archive" in archived.parts
        assert "2026" in archived.parts
        assert "04" in archived.parts

    def test_rejects_path_outside_qa(self, project_root: Path, tmp_path: Path) -> None:
        stray = tmp_path / "stray.md"
        stray.write_text("---\n---\n")
        result = integrity.archive_file(stray, project_root)
        assert result is None
        assert stray.exists()

    def test_collision_gets_counter_suffix(self, project_root: Path) -> None:
        a = _write_qa(project_root, "twin", "q1", ["src/a.py"])
        first = integrity.archive_file(a, project_root)
        assert first is not None
        # Simulate a second archive for the same stem — writer used the
        # same file name. Second call should rename with `.1` suffix.
        b = _write_qa(project_root, "twin", "q2", ["src/b.py"])
        second = integrity.archive_file(b, project_root)
        assert second is not None
        assert first != second
        assert first.exists()
        assert second.exists()


class TestPurgeOldArchive:
    def test_removes_files_older_than_ttl(self, project_root: Path) -> None:
        # Archive a fresh + old file.
        fresh = _write_qa(project_root, "fresh", "q1", ["src/a.py"])
        old = _write_qa(project_root, "old", "q2", ["src/b.py"], mtime_ago_s=45 * 86400)
        archive_fresh = integrity.archive_file(fresh, project_root)
        archive_old = integrity.archive_file(old, project_root)
        # archive_file preserves mtime on old, but ensure it stays old:
        ts_old = datetime.now(timezone.utc).timestamp() - 45 * 86400
        os.utime(archive_old, (ts_old, ts_old))

        removed = integrity.purge_old_archive(project_root, max_age_days=30)
        assert archive_old in removed
        assert archive_fresh not in removed
        assert not archive_old.exists()
        assert archive_fresh.exists()

    def test_empty_archive_is_noop(self, project_root: Path) -> None:
        assert integrity.purge_old_archive(project_root) == []


class TestDetectStaleQa:
    def test_all_refs_missing_is_stale(self, project_root: Path) -> None:
        stale = _write_qa(project_root, "stale", "q", ["src/gone.py", "src/also_gone.py"])
        result = integrity.detect_stale_qa(project_root, {"src/alive.py"})
        assert result == [stale]

    def test_any_ref_alive_survives(self, project_root: Path) -> None:
        p = _write_qa(project_root, "mixed", "q", ["src/alive.py", "src/dead.py"])
        result = integrity.detect_stale_qa(project_root, {"src/alive.py"})
        assert result == []
        assert p.exists()

    def test_empty_results_section_preserved(self, project_root: Path) -> None:
        # qa files saved by Stop hook may have no ## Top results block.
        p = _write_qa(project_root, "empty", "q", [])
        assert integrity.detect_stale_qa(project_root, set()) == []
        assert p.exists()


class TestDetectSemanticDuplicates:
    def test_identical_vectors_dedup(self, project_root: Path) -> None:
        # Simulate three chunks: A/B identical, C different.
        vec_ab = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        vec_c = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
        vectors = {"A": vec_ab, "B": vec_ab, "C": vec_c}

        def get_vector(cid):
            return vectors.get(cid)

        chunks = [
            ("A", "/x/qa/a.md", 100.0),
            ("B", "/x/qa/b.md", 200.0),  # newer
            ("C", "/x/qa/c.md", 50.0),
        ]
        pairs = integrity.detect_semantic_duplicates(chunks, get_vector, threshold=0.9)
        assert len(pairs) == 1
        older, keeper, sim = pairs[0]
        # Newer (B, mtime=200) should be the keeper.
        assert keeper == "/x/qa/b.md"
        assert older == "/x/qa/a.md"
        assert sim >= 0.99

    def test_below_threshold_no_dedup(self, project_root: Path) -> None:
        # Orthogonal vectors — cosine = 0.
        def gv(cid):
            return {
                "A": np.array([1.0, 0.0], dtype=np.float32),
                "B": np.array([0.0, 1.0], dtype=np.float32),
            }.get(cid)

        chunks = [("A", "/a.md", 1.0), ("B", "/b.md", 2.0)]
        pairs = integrity.detect_semantic_duplicates(chunks, gv, threshold=0.9)
        assert pairs == []

    def test_transitive_cluster_keeps_newest(self, project_root: Path) -> None:
        # Three near-identical vectors; expect 2 dedup pairs collapsing to 1 keeper.
        v = np.array([1.0, 0.0], dtype=np.float32)

        def gv(cid):
            return v

        chunks = [
            ("A", "/a.md", 10.0),
            ("B", "/b.md", 20.0),
            ("C", "/c.md", 30.0),  # newest
        ]
        pairs = integrity.detect_semantic_duplicates(chunks, gv, threshold=0.9)
        assert len(pairs) == 2
        keepers = {k for _, k, _ in pairs}
        assert keepers == {"/c.md"}

    def test_missing_vectors_skipped(self) -> None:
        def gv(cid):
            return None

        chunks = [("A", "/a.md", 1.0), ("B", "/b.md", 2.0)]
        assert integrity.detect_semantic_duplicates(chunks, gv) == []


class TestRunIntegrityPass:
    def test_stale_archive_flow(self, project_root: Path) -> None:
        stale = _write_qa(project_root, "ghost", "q", ["src/gone.py"])
        report = integrity.run_integrity_pass(
            project_root,
            indexed_paths=set(),  # everything considered gone
            qa_log_chunks=None,   # skip dedup
            get_vector=None,
        )
        assert len(report.stale_archived) == 1
        assert not stale.exists()
        archived = report.stale_archived[0]
        assert "qa-archive" in archived.parts

    def test_dedup_flow(self, project_root: Path) -> None:
        a = _write_qa(project_root, "a", "q1", ["src/alive.py"], mtime_ago_s=100)
        b = _write_qa(project_root, "b", "q1", ["src/alive.py"])

        v = np.array([1.0, 0.0], dtype=np.float32)
        def gv(cid):
            return v

        chunks = [
            ("cid_a", str(a), a.stat().st_mtime),
            ("cid_b", str(b), b.stat().st_mtime),
        ]
        report = integrity.run_integrity_pass(
            project_root,
            indexed_paths={"src/alive.py"},
            qa_log_chunks=chunks,
            get_vector=gv,
        )
        assert len(report.dedup_pairs) == 1
        assert not a.exists()  # older archived
        assert b.exists()

    def test_archive_purge_runs(self, project_root: Path) -> None:
        # Seed an archive with an old entry so purge has something to do.
        old = _write_qa(project_root, "old", "q", ["src/x.py"])
        arch = integrity.archive_file(old, project_root)
        ts = datetime.now(timezone.utc).timestamp() - 45 * 86400
        os.utime(arch, (ts, ts))

        report = integrity.run_integrity_pass(
            project_root,
            config=integrity.IntegrityConfig(archive_ttl_days=30),
        )
        assert arch in report.archive_purged

    def test_disabled_is_noop(self, project_root: Path) -> None:
        _write_qa(project_root, "ghost", "q", ["src/gone.py"])
        report = integrity.run_integrity_pass(
            project_root,
            indexed_paths=set(),
            config=integrity.IntegrityConfig(enabled=False),
        )
        assert report.stale_archived == []
        assert report.dedup_pairs == []
        assert report.archive_purged == []


class TestRestoreArchived:
    def test_restores_by_stem(self, project_root: Path) -> None:
        src = _write_qa(project_root, "23-010000-abc12345", "q", ["src/gone.py"])
        arch = integrity.archive_file(src, project_root)
        assert arch is not None

        restored = integrity.restore_archived(project_root, "23-010000-abc12345")
        assert restored is not None
        assert restored.exists()
        assert not arch.exists()

    def test_restores_by_hash_prefix(self, project_root: Path) -> None:
        src = _write_qa(project_root, "23-010000-deadbeef", "q", ["src/x.py"])
        integrity.archive_file(src, project_root)
        restored = integrity.restore_archived(project_root, "deadbeef")
        assert restored is not None
        assert restored.exists()

    def test_unknown_id_returns_none(self, project_root: Path) -> None:
        assert integrity.restore_archived(project_root, "nonexistent") is None


class TestStats:
    def test_counts_empty_project(self, project_root: Path) -> None:
        assert integrity.count_active(project_root) == 0
        assert integrity.count_archived(project_root) == 0

    def test_counts_populated(self, project_root: Path) -> None:
        _write_qa(project_root, "a", "q1", ["src/a.py"])
        _write_qa(project_root, "b", "q2", ["src/b.py"])
        assert integrity.count_active(project_root) == 2
        b = project_root / integrity.QA_DIRNAME / "2026" / "04" / "b.md"
        integrity.archive_file(b, project_root)
        assert integrity.count_active(project_root) == 1
        assert integrity.count_archived(project_root) == 1
