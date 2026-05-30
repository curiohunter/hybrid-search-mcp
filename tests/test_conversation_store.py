"""A3 — conversation_meta schema + StoreDB accessors.

Conversation turns live in the shared ``chunks`` table as ``node_type='conv_turn'``
(unified store, like qa_log/memory_card), while their agent-specific metadata
(source, session, turn, timestamp, touched files) lives in a dedicated
``conversation_meta`` side table — keeping the hot ``chunks`` table unpolluted,
mirroring ``file_modules`` / ``wiki_dependencies``.
"""

import sqlite3
from pathlib import Path

from hybrid_search.storage.db import (
    SCHEMA_VERSION,
    ChunkRecord,
    ConversationMeta,
    FileRecord,
    StoreDB,
)


def _seed_conv_chunk(db: StoreDB, chunk_id: str = "conv:claude:s1:0001:abcd") -> None:
    """Register a transcript-as-file and one conv_turn chunk under it."""
    with db.transaction() as conn:
        db.upsert_file(conn, FileRecord(
            id="conv-file-1", project_id="p1",
            relative_path=".conversations/claude/s1.jsonl", file_hash="h",
        ))
        db.insert_chunks(conn, [ChunkRecord(
            id=chunk_id, file_id="conv-file-1", project_id="p1",
            node_type="conv_turn", content="[claude turn] hook cwd 버그 수정",
        )])


def test_fresh_schema_has_conversation_meta(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    tables = {
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "conversation_meta" in tables
    assert db.get_meta("schema_version") == SCHEMA_VERSION


def test_upsert_and_get_conversation_meta(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    _seed_conv_chunk(db)
    cid = "conv:claude:s1:0001:abcd"
    with db.transaction() as conn:
        db.upsert_conversation_meta(conn, [ConversationMeta(
            chunk_id=cid, project_id="p1", source="claude",
            session_id="s1", turn_index=3, ts="2026-04-29T04:59:35Z",
            files='["src/hybrid_search/memory/hook_runtime.py"]',
        )])

    meta = db.get_conversation_meta(cid)
    assert meta is not None
    assert meta.source == "claude"
    assert meta.session_id == "s1"
    assert meta.turn_index == 3
    assert meta.ts == "2026-04-29T04:59:35Z"
    assert "hook_runtime.py" in (meta.files or "")


def test_upsert_conversation_meta_is_idempotent(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    _seed_conv_chunk(db)
    cid = "conv:claude:s1:0001:abcd"
    rec = ConversationMeta(
        chunk_id=cid, project_id="p1", source="claude",
        session_id="s1", turn_index=3, ts="t", files="[]",
    )
    with db.transaction() as conn:
        db.upsert_conversation_meta(conn, [rec])
    with db.transaction() as conn:
        db.upsert_conversation_meta(conn, [rec])  # second write must not duplicate

    rows = db._conn.execute(
        "SELECT COUNT(*) AS n FROM conversation_meta WHERE chunk_id = ?", (cid,)
    ).fetchone()
    assert rows["n"] == 1


def test_get_conversation_meta_batch(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    with db.transaction() as conn:
        db.upsert_file(conn, FileRecord(
            id="cf", project_id="p1",
            relative_path=".conversations/codex/s2.jsonl", file_hash="h",
        ))
        db.insert_chunks(conn, [
            ChunkRecord(id="c-a", file_id="cf", project_id="p1", node_type="conv_turn"),
            ChunkRecord(id="c-b", file_id="cf", project_id="p1", node_type="conv_turn"),
        ])
        db.upsert_conversation_meta(conn, [
            ConversationMeta(chunk_id="c-a", project_id="p1", source="codex",
                             session_id="s2", turn_index=0),
            ConversationMeta(chunk_id="c-b", project_id="p1", source="codex",
                             session_id="s2", turn_index=1),
        ])

    got = db.get_conversation_meta_batch(["c-a", "c-b", "missing"])
    assert set(got.keys()) == {"c-a", "c-b"}
    assert got["c-b"].turn_index == 1


def test_conversation_meta_cascades_on_chunk_delete(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    _seed_conv_chunk(db)
    cid = "conv:claude:s1:0001:abcd"
    with db.transaction() as conn:
        db.upsert_conversation_meta(conn, [ConversationMeta(
            chunk_id=cid, project_id="p1", source="claude",
            session_id="s1", turn_index=3,
        )])

    # Deleting the file cascades to chunks, which must cascade to conversation_meta.
    with db.transaction() as conn:
        db.delete_file(conn, "conv-file-1")

    assert db.get_conversation_meta(cid) is None
    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM conversation_meta"
    ).fetchone()["n"] == 0


class TestV7ToV8Migration:
    """v7 → v8 adds the conversation_meta table on an existing DB."""

    def _seed_v7(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                file_hash TEXT NOT NULL, file_size INTEGER, file_mtime TEXT, language TEXT,
                last_modified TEXT, chunk_count INTEGER DEFAULT 0,
                UNIQUE(project_id, relative_path)
            );
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL, project_id TEXT NOT NULL,
                name TEXT, qualified_name TEXT, node_type TEXT,
                start_line INTEGER, end_line INTEGER, start_byte INTEGER, end_byte INTEGER,
                content TEXT, embedding_input TEXT, docstring TEXT, parent_name TEXT,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            );
        """)
        conn.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '7')")
        conn.commit()
        conn.close()

    def test_migration_creates_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        self._seed_v7(db_path)

        db = StoreDB(db_path)
        try:
            assert db.get_meta("schema_version") == SCHEMA_VERSION
            tables = {
                row[0]
                for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "conversation_meta" in tables
        finally:
            db.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        self._seed_v7(db_path)
        StoreDB(db_path).close()  # migrate v7 → v8
        db = StoreDB(db_path)     # reopen: no-op
        try:
            assert db.get_meta("schema_version") == SCHEMA_VERSION
        finally:
            db.close()
