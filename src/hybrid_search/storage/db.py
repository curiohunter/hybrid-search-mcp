"""SQLite store.db — per-project storage for files, chunks, call_edges, and index_meta."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator


SCHEMA_VERSION = "1"

SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_size INTEGER,
    file_mtime TEXT,
    language TEXT,
    last_modified TEXT,
    chunk_count INTEGER DEFAULT 0,
    UNIQUE(project_id, relative_path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT,
    qualified_name TEXT,
    node_type TEXT,
    start_line INTEGER,
    end_line INTEGER,
    start_byte INTEGER,
    end_byte INTEGER,
    content TEXT,
    embedding_input TEXT,
    docstring TEXT,
    parent_name TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_name ON chunks(name);
CREATE INDEX IF NOT EXISTS idx_chunks_qualified ON chunks(qualified_name);
CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(node_type);

CREATE TABLE IF NOT EXISTS call_edges (
    caller_chunk_id TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    callee_qualified_name TEXT,
    callee_chunk_id TEXT,
    callee_module TEXT,
    project_id TEXT NOT NULL,
    confidence TEXT DEFAULT 'low',
    FOREIGN KEY (caller_chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_callee_name ON call_edges(callee_name);
CREATE INDEX IF NOT EXISTS idx_callee_qualified ON call_edges(callee_qualified_name);
CREATE INDEX IF NOT EXISTS idx_caller ON call_edges(caller_chunk_id);
"""


@dataclass
class FileRecord:
    id: str
    project_id: str
    relative_path: str
    file_hash: str
    file_size: int | None = None
    file_mtime: str | None = None
    language: str | None = None
    chunk_count: int = 0


@dataclass
class ChunkRecord:
    id: str
    file_id: str
    project_id: str
    name: str | None = None
    qualified_name: str | None = None
    node_type: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    start_byte: int | None = None
    end_byte: int | None = None
    content: str | None = None
    embedding_input: str | None = None
    docstring: str | None = None
    parent_name: str | None = None


class StoreDB:
    """Per-project SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        # Set schema version if not present
        cur = self._conn.execute("SELECT value FROM index_meta WHERE key = 'schema_version'")
        if cur.fetchone() is None:
            self._conn.execute(
                "INSERT INTO index_meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for explicit transactions."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except Exception as e:
            logger.error("BEGIN IMMEDIATE failed (in_transaction=%s): %s", self._conn.in_transaction, e)
            raise
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except Exception as e:
            logger.error("Transaction failed, rolling back: %s", e)
            self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()

    # -- index_meta --

    def get_meta(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    # -- files --

    def get_file(self, file_id: str) -> FileRecord | None:
        cur = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return FileRecord(
            id=row["id"],
            project_id=row["project_id"],
            relative_path=row["relative_path"],
            file_hash=row["file_hash"],
            file_size=row["file_size"],
            file_mtime=row["file_mtime"],
            language=row["language"],
            chunk_count=row["chunk_count"],
        )

    def get_file_by_path(self, project_id: str, relative_path: str) -> FileRecord | None:
        cur = self._conn.execute(
            "SELECT * FROM files WHERE project_id = ? AND relative_path = ?",
            (project_id, relative_path),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return FileRecord(
            id=row["id"],
            project_id=row["project_id"],
            relative_path=row["relative_path"],
            file_hash=row["file_hash"],
            file_size=row["file_size"],
            file_mtime=row["file_mtime"],
            language=row["language"],
            chunk_count=row["chunk_count"],
        )

    def get_all_files(self, project_id: str) -> list[FileRecord]:
        cur = self._conn.execute("SELECT * FROM files WHERE project_id = ?", (project_id,))
        return [
            FileRecord(
                id=row["id"],
                project_id=row["project_id"],
                relative_path=row["relative_path"],
                file_hash=row["file_hash"],
                file_size=row["file_size"],
                file_mtime=row["file_mtime"],
                language=row["language"],
                chunk_count=row["chunk_count"],
            )
            for row in cur.fetchall()
        ]

    def upsert_file(self, conn: sqlite3.Connection, record: FileRecord) -> None:
        # Use INSERT + UPDATE instead of INSERT OR REPLACE.
        # REPLACE = DELETE + INSERT, which triggers FK CASCADE and wipes chunks.
        conn.execute(
            """INSERT INTO files
               (id, project_id, relative_path, file_hash, file_size, file_mtime, language, chunk_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   project_id = excluded.project_id,
                   relative_path = excluded.relative_path,
                   file_hash = excluded.file_hash,
                   file_size = excluded.file_size,
                   file_mtime = excluded.file_mtime,
                   language = excluded.language,
                   chunk_count = excluded.chunk_count""",
            (
                record.id,
                record.project_id,
                record.relative_path,
                record.file_hash,
                record.file_size,
                record.file_mtime,
                record.language,
                record.chunk_count,
            ),
        )

    def delete_file(self, conn: sqlite3.Connection, file_id: str) -> None:
        """Delete file and cascade to chunks and call_edges."""
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def get_all_file_paths(self, project_id: str) -> set[str]:
        cur = self._conn.execute(
            "SELECT relative_path FROM files WHERE project_id = ?", (project_id,)
        )
        return {row["relative_path"] for row in cur.fetchall()}

    # -- chunks --

    def insert_chunks(self, conn: sqlite3.Connection, chunks: list[ChunkRecord]) -> None:
        conn.executemany(
            """INSERT OR REPLACE INTO chunks
               (id, file_id, project_id, name, qualified_name, node_type,
                start_line, end_line, start_byte, end_byte,
                content, embedding_input, docstring, parent_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    c.id, c.file_id, c.project_id, c.name, c.qualified_name,
                    c.node_type, c.start_line, c.end_line, c.start_byte, c.end_byte,
                    c.content, c.embedding_input, c.docstring, c.parent_name,
                )
                for c in chunks
            ],
        )

    def insert_call_edges(
        self,
        conn: sqlite3.Connection,
        caller_chunk_id: str,
        calls: list[str],
        project_id: str,
    ) -> None:
        """Insert call edges from a chunk's extracted calls."""
        if not calls:
            return
        conn.executemany(
            """INSERT INTO call_edges
               (caller_chunk_id, callee_name, project_id, confidence)
               VALUES (?, ?, ?, 'low')""",
            [(caller_chunk_id, callee_name, project_id) for callee_name in calls],
        )

    def delete_call_edges_by_caller(self, conn: sqlite3.Connection, chunk_id: str) -> None:
        """Delete all call edges where this chunk is the caller."""
        conn.execute("DELETE FROM call_edges WHERE caller_chunk_id = ?", (chunk_id,))

    def delete_chunks_by_file(self, conn: sqlite3.Connection, file_id: str) -> list[str]:
        """Delete all chunks for a file, returning their IDs."""
        cur = conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
        chunk_ids = [row["id"] for row in cur.fetchall()]
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        return chunk_ids

    def get_chunks_by_project(self, project_id: str) -> list[ChunkRecord]:
        cur = self._conn.execute("SELECT * FROM chunks WHERE project_id = ?", (project_id,))
        return [self._row_to_chunk(row) for row in cur.fetchall()]

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        cur = self._conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,))
        row = cur.fetchone()
        return self._row_to_chunk(row) if row else None

    def search_chunks_by_name(
        self, name_pattern: str, project_id: str | None = None
    ) -> list[ChunkRecord]:
        if project_id:
            cur = self._conn.execute(
                """SELECT * FROM chunks
                   WHERE (name LIKE ? OR qualified_name LIKE ?) AND project_id = ?""",
                (f"%{name_pattern}%", f"%{name_pattern}%", project_id),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM chunks WHERE name LIKE ? OR qualified_name LIKE ?",
                (f"%{name_pattern}%", f"%{name_pattern}%"),
            )
        return [self._row_to_chunk(row) for row in cur.fetchall()]

    def get_chunk_count(self, project_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM chunks WHERE project_id = ?", (project_id,)
        )
        return cur.fetchone()["cnt"]

    def get_file_count(self, project_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM files WHERE project_id = ?", (project_id,)
        )
        return cur.fetchone()["cnt"]

    def _row_to_chunk(self, row: sqlite3.Row) -> ChunkRecord:
        return ChunkRecord(
            id=row["id"],
            file_id=row["file_id"],
            project_id=row["project_id"],
            name=row["name"],
            qualified_name=row["qualified_name"],
            node_type=row["node_type"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            start_byte=row["start_byte"],
            end_byte=row["end_byte"],
            content=row["content"],
            embedding_input=row["embedding_input"],
            docstring=row["docstring"],
            parent_name=row["parent_name"],
        )
