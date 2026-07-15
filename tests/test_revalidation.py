"""P1-2 tests — commit-aware invalidation.

A qa answered from files that a LATER commit changed gets flagged
``needs_revalidation`` (side table, no frontmatter rewrite → no
re-embedding). Search then marks, demotes, and strong-blocks it via the
same quarantine lane as P1-1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hybrid_search.memory.revalidation import anchor_paths, compute_revalidations
from hybrid_search.search.orchestrator import (
    HybridResult,
    _apply_revalidation_flag,
    _memory_verification,
)
from hybrid_search.storage.db import StoreDB


def _qa(query: str, ts: str | None, paths: list[str]) -> str:
    fm = f'---\nquery: "{query}"\n'
    if ts:
        fm += f"timestamp: {ts}\n"
    fm += "---\n\n## Answer excerpt\n\nanswer\n\n## Top results\n\n"
    for i, p in enumerate(paths, 1):
        fm += f"### {i}. `{p}:1-10` — name\n- chunk_id: `c{i}`\n\n"
    return fm


_COMMIT_TIME = datetime(2026, 7, 10, tzinfo=timezone.utc)
_BEFORE = "2026-07-01T00:00:00+00:00"
_AFTER = "2026-07-14T00:00:00+00:00"


class TestAnchorPaths:
    def test_top_three_distinct(self) -> None:
        content = _qa("q", _BEFORE, [
            "a.py", "b.py", "a.py", "c.py", "d.py",
        ])
        assert anchor_paths(content) == ["a.py", "b.py", "c.py"]

    def test_no_results_section(self) -> None:
        assert anchor_paths("---\nquery: \"q\"\n---\nbody") == []


class TestComputeRevalidations:
    def test_older_qa_anchored_to_changed_file_is_flagged(self) -> None:
        entries = [("qa-1", _qa("auth 어디서 처리해?", _BEFORE, ["src/auth.py"]))]
        rows = compute_revalidations(
            entries, {"src/auth.py"}, cause_commit="abc1234",
            commit_time=_COMMIT_TIME,
        )
        assert rows == [("qa-1", "abc1234", "src/auth.py")]

    def test_qa_written_after_commit_is_never_flagged(self) -> None:
        entries = [("qa-1", _qa("q", _AFTER, ["src/auth.py"]))]
        assert compute_revalidations(
            entries, {"src/auth.py"}, cause_commit="abc1234",
            commit_time=_COMMIT_TIME,
        ) == []

    def test_undated_qa_skipped_when_commit_time_known(self) -> None:
        entries = [("qa-1", _qa("q", None, ["src/auth.py"]))]
        assert compute_revalidations(
            entries, {"src/auth.py"}, cause_commit="abc1234",
            commit_time=_COMMIT_TIME,
        ) == []

    def test_deep_rank_anchor_does_not_flag(self) -> None:
        """Only the top-3 anchors count — incidental co-retrievals at
        rank 4+ must not invalidate the memory."""
        entries = [("qa-1", _qa("q", _BEFORE, ["a.py", "b.py", "c.py", "hot.py"]))]
        assert compute_revalidations(
            entries, {"hot.py"}, cause_commit="abc1234",
            commit_time=_COMMIT_TIME,
        ) == []

    def test_unrelated_change_does_not_flag(self) -> None:
        entries = [("qa-1", _qa("q", _BEFORE, ["src/auth.py"]))]
        assert compute_revalidations(
            entries, {"docs/readme.md"}, cause_commit="abc1234",
            commit_time=_COMMIT_TIME,
        ) == []


class TestCommitBatching:
    """Round-1 fix 3: >cap backlogs must resume, never skip to HEAD."""

    def test_backlog_of_51_processes_oldest_50_and_resumes(self) -> None:
        from hybrid_search.memory.revalidation import next_commit_batch

        commits = [f"c{i:03d}" for i in range(51)]  # oldest → newest
        batch, cursor = next_commit_batch(commits, cap=50)
        assert batch == commits[:50]
        assert cursor == "c049"          # NOT head — resume point
        # Next reindex picks up the remainder and lands on head.
        batch2, cursor2 = next_commit_batch(commits[50:], cap=50)
        assert batch2 == ["c050"]
        assert cursor2 == "c050"

    def test_small_range_cursor_is_head(self) -> None:
        from hybrid_search.memory.revalidation import next_commit_batch

        batch, cursor = next_commit_batch(["a", "b", "head"], cap=50)
        assert batch == ["a", "b", "head"] and cursor == "head"

    def test_empty_range(self) -> None:
        from hybrid_search.memory.revalidation import next_commit_batch

        assert next_commit_batch([], cap=50) == ([], None)


class TestRevalidationStore:
    def test_roundtrip_and_orphan_prune(self, tmp_path: Path) -> None:
        from hybrid_search.storage.db import ChunkRecord, FileRecord

        db = StoreDB(tmp_path / "store.db")
        try:
            with db.transaction() as conn:
                db.upsert_file(conn, FileRecord(
                    id="f1", project_id="p1", relative_path="qa/a.md",
                    file_hash="h",
                ))
                db.insert_chunks(conn, [ChunkRecord(
                    id="qa-live", file_id="f1", project_id="p1",
                    node_type="qa_log",
                )])
                db.add_qa_revalidations(conn, "p1", [
                    ("qa-live", "abc1234", "src/auth.py"),
                    ("qa-gone", "abc1234", "src/auth.py"),
                ])
            assert db.get_qa_revalidations(["qa-live", "qa-gone"]) == {
                "qa-live": ("abc1234", "src/auth.py"),
                "qa-gone": ("abc1234", "src/auth.py"),
            }
            with db.transaction() as conn:
                pruned = db.prune_orphan_qa_revalidations(conn)
            assert pruned == 1
            assert db.get_qa_revalidations(["qa-live", "qa-gone"]) == {
                "qa-live": ("abc1234", "src/auth.py"),
            }
        finally:
            db.close()


def _result(chunk_id: str = "qa-1") -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id, rrf_score=0.02, bm25_rank=1, vector_rank=1,
        file_path=f"qa/{chunk_id}.md", project="p", name=chunk_id,
        qualified_name=chunk_id, node_type="qa_log", start_line=1, end_line=5,
        content='---\nquery: "q"\n---\nbody', snippet="s",
        trust_meta="[qa - 10d ago]",
    )


class TestReadPath:
    def test_flag_stamps_trust_meta_and_cause(self) -> None:
        flagged = _apply_revalidation_flag(_result(), ("abc1234", "src/auth.py"))
        assert flagged.revalidation_cause == "abc1234"
        assert "needs_revalidation — src/auth.py changed in abc1234" in flagged.trust_meta
        assert flagged.snippet.startswith("[needs_revalidation")

    def test_no_flag_is_identity(self) -> None:
        r = _result()
        assert _apply_revalidation_flag(r, None) is r

    def test_flag_overrides_written_verification(self) -> None:
        """CA-T2 core — whatever the record claimed at write time, a
        changed anchor forces needs_revalidation (which the quarantine
        then strong-blocks and decays)."""
        r = _result()
        r = _apply_revalidation_flag(r, ("abc1234", "src/auth.py"))
        assert _memory_verification(r) == "needs_revalidation"
