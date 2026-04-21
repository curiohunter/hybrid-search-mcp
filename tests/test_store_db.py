import sqlite3
from pathlib import Path

from hybrid_search.storage.db import (
    CONFIDENCE_SCORES,
    SCHEMA_VERSION,
    ChunkRecord,
    FileRecord,
    StoreDB,
)


def test_upsert_file_does_not_delete_existing_chunks(tmp_path: Path) -> None:
    db = StoreDB(tmp_path / "store.db")
    conn = db._conn
    file_id = "file1"

    db.upsert_file(
        conn,
        FileRecord(
            id=file_id,
            project_id="project1",
            relative_path="src/example.py",
            file_hash="",
            file_size=10,
            file_mtime="1",
            language="python",
            chunk_count=0,
        ),
    )
    db.insert_chunks(
        conn,
        [
            ChunkRecord(
                id="chunk1",
                file_id=file_id,
                project_id="project1",
                content="print('hello')",
            )
        ],
    )

    db.upsert_file(
        conn,
        FileRecord(
            id=file_id,
            project_id="project1",
            relative_path="src/example.py",
            file_hash="abc123",
            file_size=10,
            file_mtime="1",
            language="python",
            chunk_count=1,
        ),
    )

    assert db.get_file_count("project1") == 1
    assert db.get_chunk_count("project1") == 1


class TestConfidenceScoreMigration:
    """M1 — v3 → v4 adds confidence_score to call_edges and backfills from label."""

    def _seed_chunks(self, db: StoreDB, project_id: str) -> None:
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f1", project_id=project_id,
                relative_path="src/a.py", file_hash="h",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(id="callee-1", file_id="f1", project_id=project_id),
                ChunkRecord(id="callee-2", file_id="f1", project_id=project_id),
                ChunkRecord(id="callee-3", file_id="f1", project_id=project_id),
                ChunkRecord(id="caller-1", file_id="f1", project_id=project_id),
            ])

    def test_fresh_schema_has_column_and_default(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(call_edges)").fetchall()}
        assert "confidence_score" in cols
        assert db.get_meta("schema_version") == SCHEMA_VERSION

    def test_v3_to_v4_backfills_from_label(self, tmp_path: Path) -> None:
        """An existing v3 DB should gain confidence_score and be backfilled from label."""
        db_path = tmp_path / "store.db"

        # Simulate a v3 DB: build schema without confidence_score and stamp v3.
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
            CREATE TABLE call_edges (
                caller_chunk_id TEXT NOT NULL, callee_name TEXT NOT NULL,
                callee_qualified_name TEXT, callee_chunk_id TEXT, callee_module TEXT,
                project_id TEXT NOT NULL, confidence TEXT DEFAULT 'low',
                FOREIGN KEY (caller_chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
            );
            CREATE TABLE wiki_pages (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, query_key TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, accessed_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 1, version INTEGER DEFAULT 1,
                UNIQUE(project_id, query_key)
            );
        """)
        # Seed one file+chunk, three edges at different labels.
        conn.execute("INSERT INTO files (id, project_id, relative_path, file_hash) VALUES ('f1','p1','a.py','h')")
        for cid in ("c1", "c2", "c3", "caller"):
            conn.execute(
                "INSERT INTO chunks (id, file_id, project_id) VALUES (?, 'f1', 'p1')",
                (cid,),
            )
        for cid, label in (("c1", "high"), ("c2", "medium"), ("c3", "low")):
            conn.execute(
                """INSERT INTO call_edges
                   (caller_chunk_id, callee_name, callee_chunk_id, project_id, confidence)
                   VALUES ('caller', 'fn', ?, 'p1', ?)""",
                (cid, label),
            )
        conn.execute(
            "INSERT INTO index_meta (key, value) VALUES ('schema_version', '3')"
        )
        conn.commit()
        conn.close()

        # Reopen with StoreDB → migration runs.
        db = StoreDB(db_path)
        assert db.get_meta("schema_version") == SCHEMA_VERSION
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(call_edges)").fetchall()}
        assert "confidence_score" in cols

        authority = db.get_chunk_authority_scores("p1")
        assert authority["c1"] == CONFIDENCE_SCORES["extracted"]
        assert authority["c2"] == CONFIDENCE_SCORES["inferred"]
        assert authority["c3"] == CONFIDENCE_SCORES["ambiguous"]

        # M1.1: v4→v5 migration also renames labels in the same open() pass.
        rows = db._conn.execute(
            "SELECT callee_chunk_id, confidence FROM call_edges ORDER BY callee_chunk_id"
        ).fetchall()
        labels = {r["callee_chunk_id"]: r["confidence"] for r in rows}
        assert labels == {"c1": "extracted", "c2": "inferred", "c3": "ambiguous"}

    def test_get_chunk_authority_scores_takes_max_per_chunk(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        self._seed_chunks(db, "p1")
        with db.transaction() as conn:
            # Two edges pointing at callee-1 (ambiguous + extracted) → MAX = extracted.
            conn.execute(
                """INSERT INTO call_edges
                   (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                    confidence, confidence_score)
                   VALUES ('caller-1', 'fn', 'callee-1', 'p1', 'ambiguous', 0.3)"""
            )
            conn.execute(
                """INSERT INTO call_edges
                   (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                    confidence, confidence_score)
                   VALUES ('caller-1', 'fn', 'callee-1', 'p1', 'extracted', 1.0)"""
            )
            # Unresolved edge (no callee_chunk_id) is ignored.
            conn.execute(
                """INSERT INTO call_edges
                   (caller_chunk_id, callee_name, project_id, confidence, confidence_score)
                   VALUES ('caller-1', 'mystery', 'p1', 'ambiguous', 0.3)"""
            )

        authority = db.get_chunk_authority_scores("p1")
        assert authority == {"callee-1": 1.0}


class TestConfidenceLabelRename:
    """M1.1 — v4 → v5 migration renames legacy labels to graphify-aligned ones."""

    def _seed_v4(self, db_path: Path) -> None:
        """Build a v4 DB (with confidence_score column) carrying legacy labels."""
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
            CREATE TABLE call_edges (
                caller_chunk_id TEXT NOT NULL, callee_name TEXT NOT NULL,
                callee_qualified_name TEXT, callee_chunk_id TEXT, callee_module TEXT,
                project_id TEXT NOT NULL, confidence TEXT DEFAULT 'low',
                confidence_score REAL DEFAULT 0.0,
                FOREIGN KEY (caller_chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
            );
            CREATE TABLE wiki_pages (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, query_key TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, accessed_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 1, version INTEGER DEFAULT 1,
                synthesis_model TEXT, synthesis_version INTEGER DEFAULT 0,
                synthesis_hash TEXT, last_synthesized_at TEXT,
                UNIQUE(project_id, query_key)
            );
        """)
        conn.execute("INSERT INTO files (id, project_id, relative_path, file_hash) VALUES ('f1','p1','a.py','h')")
        for cid in ("c1", "c2", "c3", "caller"):
            conn.execute(
                "INSERT INTO chunks (id, file_id, project_id) VALUES (?, 'f1', 'p1')",
                (cid,),
            )
        for cid, label, score in (
            ("c1", "high", 1.0), ("c2", "medium", 0.8), ("c3", "low", 0.3),
        ):
            conn.execute(
                """INSERT INTO call_edges
                   (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                    confidence, confidence_score)
                   VALUES ('caller', 'fn', ?, 'p1', ?, ?)""",
                (cid, label, score),
            )
        conn.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '4')")
        conn.commit()
        conn.close()

    def test_v4_to_v5_renames_labels(self, tmp_path: Path) -> None:
        """A v4 DB with low/medium/high should be rewritten to ambiguous/inferred/extracted."""
        db_path = tmp_path / "store.db"
        self._seed_v4(db_path)

        db = StoreDB(db_path)
        try:
            assert db.get_meta("schema_version") == SCHEMA_VERSION

            rows = db._conn.execute(
                "SELECT callee_chunk_id, confidence, confidence_score "
                "FROM call_edges ORDER BY callee_chunk_id"
            ).fetchall()
            labels = {r["callee_chunk_id"]: r["confidence"] for r in rows}
            scores = {r["callee_chunk_id"]: r["confidence_score"] for r in rows}

            assert labels == {"c1": "extracted", "c2": "inferred", "c3": "ambiguous"}
            # Numeric scores must survive the rename untouched.
            assert scores == {"c1": 1.0, "c2": 0.8, "c3": 0.3}
        finally:
            db.close()

    def test_v5_db_is_idempotent(self, tmp_path: Path) -> None:
        """Reopening a v5 DB must not double-migrate or rewrite anything."""
        db_path = tmp_path / "store.db"
        self._seed_v4(db_path)

        StoreDB(db_path).close()  # First open → migrates to v5.
        # Second open: should be a no-op; labels stay as-is.
        db = StoreDB(db_path)
        try:
            rows = db._conn.execute(
                "SELECT confidence FROM call_edges ORDER BY callee_chunk_id"
            ).fetchall()
            assert [r["confidence"] for r in rows] == ["extracted", "inferred", "ambiguous"]
        finally:
            db.close()
