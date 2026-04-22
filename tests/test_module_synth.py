"""Tests for module card synthesis — index/module_synth.py."""

from __future__ import annotations

import json
from pathlib import Path

from hybrid_search.index.module_synth import (
    _extract_rationale,
    _pick_entry_points,
    synthesize_modules,
)
from hybrid_search.index.modules import discover_modules
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


PROJECT_ID = "test-project"


def _make_db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


def _seed_file_with_chunks(
    db: StoreDB,
    rel_path: str,
    chunks: list[ChunkRecord],
    project_id: str = PROJECT_ID,
) -> str:
    fid = f"f_{hash(rel_path) & 0xffffffff:x}"
    with db.transaction() as conn:
        db.upsert_file(
            conn,
            FileRecord(
                id=fid, project_id=project_id,
                relative_path=rel_path, file_hash="h",
            ),
        )
        for c in chunks:
            c.file_id = fid
            c.project_id = project_id
        if chunks:
            db.insert_chunks(conn, chunks)
    return fid


def _chunk(cid: str, **kwargs) -> ChunkRecord:
    kwargs.setdefault("file_id", "")
    kwargs.setdefault("project_id", PROJECT_ID)
    return ChunkRecord(id=cid, **kwargs)


# ---------- _extract_rationale ----------

def test_extract_rationale_note_tag():
    chunks = [_chunk("c1", docstring="NOTE: same-file edges are excluded")]
    assert "NOTE: same-file edges are excluded" in _extract_rationale(chunks)


def test_extract_rationale_multiple_tags_dedup():
    c1 = _chunk("c1", docstring="NOTE: one\nWHY: two")
    c2 = _chunk("c2", docstring="NOTE: one\nTODO: three")
    text = _extract_rationale([c1, c2])
    lines = text.splitlines()
    assert lines.count("NOTE: one") == 1
    assert "WHY: two" in text
    assert "TODO: three" in text


def test_extract_rationale_ignores_plain_prose():
    chunks = [_chunk("c1", docstring="Just a description, no tags.")]
    assert _extract_rationale(chunks) == ""


def test_extract_rationale_handles_prefix_markers():
    # "# NOTE:" and "// NOTE:" should both parse
    chunks = [
        _chunk("c1", docstring="# NOTE: python style"),
        _chunk("c2", docstring="// NOTE: js style"),
    ]
    text = _extract_rationale(chunks)
    assert "NOTE: python style" in text
    assert "NOTE: js style" in text


# ---------- _pick_entry_points ----------

def test_pick_entry_points_prefers_longest_docstring():
    chunks = [
        _chunk("short", docstring="brief", node_type="function"),
        _chunk("long", docstring="much longer explanation of behavior",
               node_type="function"),
        _chunk("mid", docstring="medium", node_type="function"),
    ]
    picks = _pick_entry_points(chunks)
    assert picks[0] == "long"


def test_pick_entry_points_ranks_function_over_statement():
    chunks = [
        _chunk("stmt", docstring="same length docstring foo",
               node_type="statement"),
        _chunk("func", docstring="same length docstring foo",
               node_type="function"),
    ]
    picks = _pick_entry_points(chunks)
    assert picks[0] == "func"


def test_pick_entry_points_caps_at_five():
    chunks = [_chunk(f"c{i}", docstring=f"doc {i}", node_type="function") for i in range(10)]
    assert len(_pick_entry_points(chunks)) == 5


# ---------- synthesize_modules (integration with discover) ----------

def test_synthesize_populates_summary_and_entry_points(tmp_path):
    db = _make_db(tmp_path)
    _seed_file_with_chunks(db, "a/x.py", [
        _chunk("c1", name="login", qualified_name="a/x.py::login",
               docstring="Authenticate user given credentials.",
               node_type="function"),
    ])
    _seed_file_with_chunks(db, "a/y.py", [
        _chunk("c2", name="validate", qualified_name="a/y.py::validate",
               docstring="Verify session token.",
               node_type="function"),
    ])

    discover_modules(db, PROJECT_ID, tmp_path)
    stats = synthesize_modules(db, PROJECT_ID)
    assert stats["synthesized"] >= 1

    mods = db.get_modules(PROJECT_ID)
    assert len(mods) == 1
    m = mods[0]
    assert m.summary and "Authenticate" in m.summary
    entry_points = json.loads(m.entry_points)
    assert set(entry_points) >= {"c1", "c2"}


def test_synthesize_is_idempotent_when_nothing_changed(tmp_path):
    db = _make_db(tmp_path)
    _seed_file_with_chunks(db, "a/x.py", [
        _chunk("c1", docstring="hello", node_type="function"),
    ])
    _seed_file_with_chunks(db, "a/y.py", [
        _chunk("c2", docstring="world", node_type="function"),
    ])
    discover_modules(db, PROJECT_ID, tmp_path)

    s1 = synthesize_modules(db, PROJECT_ID)
    s2 = synthesize_modules(db, PROJECT_ID)
    assert s1["synthesized"] >= 1
    assert s2["skipped"] == s1["modules"]
    assert s2["synthesized"] == 0


def test_synthesize_rationale_extracted(tmp_path):
    db = _make_db(tmp_path)
    _seed_file_with_chunks(db, "svc/x.py", [
        _chunk("c1", docstring="Doc.\n\nWHY: we chose X over Y for perf.",
               node_type="function"),
    ])
    _seed_file_with_chunks(db, "svc/y.py", [
        _chunk("c2", docstring="Other.\n\nNOTE: async boundary here.",
               node_type="function"),
    ])
    discover_modules(db, PROJECT_ID, tmp_path)
    synthesize_modules(db, PROJECT_ID)
    m = db.get_modules(PROJECT_ID)[0]
    assert "WHY: we chose X" in (m.rationale or "")
    assert "NOTE: async boundary" in (m.rationale or "")


def test_synthesize_fallback_summary_when_no_docstrings(tmp_path):
    db = _make_db(tmp_path)
    _seed_file_with_chunks(db, "x/a.py", [_chunk("c1", node_type="function")])
    _seed_file_with_chunks(db, "x/b.py", [_chunk("c2", node_type="function")])
    discover_modules(db, PROJECT_ID, tmp_path)
    synthesize_modules(db, PROJECT_ID)
    m = db.get_modules(PROJECT_ID)[0]
    assert m.summary and "a.py" in m.summary and "b.py" in m.summary
