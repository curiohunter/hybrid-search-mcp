"""Tests for module card synthesis — index/module_synth.py."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from hybrid_search.index.module_synth import (
    _extract_rationale,
    _pick_entry_points,
    synthesize_modules,
    vector_input_text,
)
from hybrid_search.index.modules import discover_modules
from hybrid_search.storage.db import ChunkRecord, FileRecord, ModuleRecord, StoreDB


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


# ---------- Step C: embedding pass ----------

class _FakeEmbedder:
    """Returns a deterministic vector per text for test isolation."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_batch: list[str] = []

    def embed_texts(self, texts):
        self.call_count += 1
        self.last_batch = list(texts)
        # One-hot style: first char codepoint determines the axis in a tiny space.
        out = []
        for t in texts:
            v = np.zeros(8, dtype=np.float32)
            v[ord(t[0]) % 8] = 1.0
            out.append(v)
        return np.stack(out)


def test_vector_input_text_drops_hash_prefix():
    m = ModuleRecord(
        id="m1", project_id=PROJECT_ID, name="portal",
        summary="[hash:v1:aaaa] Portal shell for parents.",
        entry_points=None, depends_on=None, related_docs=None,
        rationale="NOTE: keep state local.", signals=None,
        member_hash="h", updated_at="x",
    )
    text = vector_input_text(m)
    assert "[hash:" not in text
    assert "Portal shell" in text
    assert "NOTE: keep state local." in text
    assert text.startswith("portal")


def _seed_two_files_module(db: StoreDB, tmp_path: Path) -> None:
    """Helper: module discovery requires ≥ 2 files per directory."""
    _seed_file_with_chunks(db, "a/x.py", [
        _chunk("c1", name="login", docstring="Authenticate user.",
               node_type="function"),
    ])
    _seed_file_with_chunks(db, "a/y.py", [
        _chunk("c2", name="session", docstring="Session token.",
               node_type="function"),
    ])
    discover_modules(db, PROJECT_ID, tmp_path)


def test_synthesize_embeds_when_embedder_provided(tmp_path):
    db = _make_db(tmp_path)
    _seed_two_files_module(db, tmp_path)
    emb = _FakeEmbedder()
    stats = synthesize_modules(db, PROJECT_ID, embedder=emb)
    assert stats["embedded"] == 1
    assert emb.call_count == 1

    m = db.get_modules(PROJECT_ID)[0]
    assert m.summary_vector is not None
    vec = np.frombuffer(m.summary_vector, dtype=np.float32)
    assert vec.size == 8
    assert m.vector_input_hash and len(m.vector_input_hash) == 16


def test_embedding_skipped_on_rerun_when_text_unchanged(tmp_path):
    db = _make_db(tmp_path)
    _seed_two_files_module(db, tmp_path)
    emb = _FakeEmbedder()

    s1 = synthesize_modules(db, PROJECT_ID, embedder=emb)
    s2 = synthesize_modules(db, PROJECT_ID, embedder=emb)
    # First run embeds, second run short-circuits on vector_input_hash match.
    assert s1["embedded"] >= 1
    assert s2["embedded"] == 0
    # _FakeEmbedder only called on the first run.
    assert emb.call_count == 1


def test_embedding_noop_when_embedder_is_none(tmp_path):
    """Backward compatibility: existing callers that don't pass an embedder
    still get text synthesis and never see the new 'embedded' path crash."""
    db = _make_db(tmp_path)
    _seed_two_files_module(db, tmp_path)
    stats = synthesize_modules(db, PROJECT_ID)
    assert stats["embedded"] == 0
    m = db.get_modules(PROJECT_ID)[0]
    assert m.summary_vector is None


def test_embedder_failure_does_not_abort_synthesis(tmp_path):
    """Embedding failures are non-fatal — synthesis text is still written so
    future runs can retry the vector pass."""
    class _FailingEmbedder:
        def embed_texts(self, texts):
            raise RuntimeError("simulated API outage")

    db = _make_db(tmp_path)
    _seed_two_files_module(db, tmp_path)
    stats = synthesize_modules(db, PROJECT_ID, embedder=_FailingEmbedder())
    assert stats["synthesized"] >= 1
    assert stats["embedded"] == 0
    m = db.get_modules(PROJECT_ID)[0]
    assert m.summary  # text still present
    assert m.summary_vector is None  # no vector after outage
