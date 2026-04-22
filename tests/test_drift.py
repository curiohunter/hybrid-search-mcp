"""Tests for index/drift.py — Phase 6 L4 watchdog."""

from __future__ import annotations

from pathlib import Path

from hybrid_search.config import IndexingConfig
from hybrid_search.index.drift import DriftReport, detect_drift
from hybrid_search.storage.db import FileRecord, StoreDB


PROJECT_ID = "test-project"


def _make_db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


def _make_config() -> IndexingConfig:
    return IndexingConfig()


def _seed_file(db: StoreDB, rel_path: str, file_hash: str = "h1") -> None:
    with db.transaction() as conn:
        db.upsert_file(
            conn,
            FileRecord(
                id=f"f_{abs(hash(rel_path)) & 0xffffffff:x}",
                project_id=PROJECT_ID,
                relative_path=rel_path,
                file_hash=file_hash,
            ),
        )


def test_drift_report_is_drifted_property():
    r = DriftReport(project_id="p", added=["a"], changed=[], deleted=[], total_on_disk=1)
    assert r.is_drifted
    assert r.drift_count == 1


def test_drift_report_in_sync():
    r = DriftReport(project_id="p", added=[], changed=[], deleted=[], total_on_disk=5)
    assert not r.is_drifted
    assert r.drift_count == 0
    assert "in sync" in r.summary_line()


def test_drift_report_summary_line_shows_breakdown():
    r = DriftReport(
        project_id="p", added=["a"], changed=["b"], deleted=["c"], total_on_disk=3
    )
    line = r.summary_line()
    assert "drift 3" in line
    assert "+1 added" in line
    assert "~1 changed" in line
    assert "-1 deleted" in line


def test_detect_drift_added_files(tmp_path):
    """Files on disk but not in DB show up as added."""
    db = _make_db(tmp_path)
    # Create two files on disk
    (tmp_path / "a.py").write_text("print('a')")
    (tmp_path / "b.py").write_text("print('b')")
    # Only one is in DB
    _seed_file(db, "a.py")

    report = detect_drift(PROJECT_ID, tmp_path, db, _make_config())
    assert "b.py" in report.added
    assert "a.py" not in report.added
    assert report.is_drifted


def test_detect_drift_clean_state(tmp_path):
    """When DB and disk match, no drift."""
    db = _make_db(tmp_path)
    (tmp_path / "x.py").write_text("x = 1")
    # Seed file with the hash of the actual content
    from hybrid_search.index.scanner import compute_file_hash
    actual_hash = compute_file_hash(tmp_path / "x.py")
    _seed_file(db, "x.py", file_hash=actual_hash)

    report = detect_drift(PROJECT_ID, tmp_path, db, _make_config())
    assert not report.is_drifted


def test_detect_drift_deleted_files(tmp_path):
    """Files in DB but not on disk count as deleted."""
    db = _make_db(tmp_path)
    _seed_file(db, "ghost.py")  # no corresponding disk file
    report = detect_drift(PROJECT_ID, tmp_path, db, _make_config())
    assert "ghost.py" in report.deleted
    assert report.is_drifted


def test_detect_drift_changed_files(tmp_path):
    """Files whose content hash differs show up as changed."""
    db = _make_db(tmp_path)
    (tmp_path / "c.py").write_text("version 1")
    # Scanner prefilters on (size, mtime) before rehashing — seed wrong
    # size+mtime so the change actually routes through the hash check.
    with db.transaction() as conn:
        db.upsert_file(
            conn,
            FileRecord(
                id="f_c", project_id=PROJECT_ID, relative_path="c.py",
                file_hash="stale-hash", file_size=999, file_mtime="0",
            ),
        )

    report = detect_drift(PROJECT_ID, tmp_path, db, _make_config())
    assert "c.py" in report.changed


def test_detect_drift_is_readonly(tmp_path):
    """Drift detection must not mutate DB state."""
    db = _make_db(tmp_path)
    (tmp_path / "a.py").write_text("x")
    detect_drift(PROJECT_ID, tmp_path, db, _make_config())
    # No files should have been added to the DB
    assert db.get_file_count(PROJECT_ID) == 0
