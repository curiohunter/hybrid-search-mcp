"""SQLite store.db — per-project storage for files, chunks, call_edges, and index_meta."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "8"

# Semantic labels for call-edge confidence (M1.1).
# Order: weakest → strongest; _confidence_filter relies on this ordering.
#   ambiguous — name-only match, multiple candidates or common names
#   inferred  — qualified_name or single-candidate name match
#   extracted — import path + symbol name, authoritatively bound
CONFIDENCE_LEVELS = ("ambiguous", "inferred", "extracted")

# Numeric scores attached to each confidence label (M1).
# Used by fusion for authority-aware ranking; Leiden/DAG stays confidence-blind.
CONFIDENCE_SCORES: dict[str, float] = {
    "extracted": 1.0,
    "inferred": 0.8,
    "ambiguous": 0.3,
}


def _confidence_filter(min_confidence: str) -> tuple[str, ...]:
    """Return confidence levels >= min_confidence."""
    idx = CONFIDENCE_LEVELS.index(min_confidence) if min_confidence in CONFIDENCE_LEVELS else 0
    return CONFIDENCE_LEVELS[idx:]

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
    confidence TEXT DEFAULT 'ambiguous',
    confidence_score REAL DEFAULT 0.0,
    FOREIGN KEY (caller_chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_callee_name ON call_edges(callee_name);
CREATE INDEX IF NOT EXISTS idx_callee_qualified ON call_edges(callee_qualified_name);
CREATE INDEX IF NOT EXISTS idx_callee_chunk ON call_edges(callee_chunk_id);
CREATE INDEX IF NOT EXISTS idx_caller ON call_edges(caller_chunk_id);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    query_key TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    access_count INTEGER DEFAULT 1,
    version INTEGER DEFAULT 1,
    synthesis_model TEXT,
    synthesis_version INTEGER DEFAULT 0,
    synthesis_hash TEXT,
    last_synthesized_at TEXT,
    UNIQUE(project_id, query_key)
);

CREATE INDEX IF NOT EXISTS idx_wiki_project ON wiki_pages(project_id);
CREATE INDEX IF NOT EXISTS idx_wiki_accessed ON wiki_pages(accessed_at);

CREATE TABLE IF NOT EXISTS wiki_dependencies (
    wiki_page_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_hash_at_compile TEXT NOT NULL,
    chunk_ids TEXT,
    FOREIGN KEY (wiki_page_id) REFERENCES wiki_pages(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (wiki_page_id, file_id)
);

CREATE TABLE IF NOT EXISTS wiki_links (
    source_page_id TEXT NOT NULL,
    target_page_id TEXT NOT NULL,
    link_text TEXT NOT NULL,
    FOREIGN KEY (source_page_id) REFERENCES wiki_pages(id) ON DELETE CASCADE,
    FOREIGN KEY (target_page_id) REFERENCES wiki_pages(id) ON DELETE CASCADE,
    PRIMARY KEY (source_page_id, target_page_id, link_text)
);

CREATE INDEX IF NOT EXISTS idx_wiki_links_source ON wiki_links(source_page_id);
CREATE INDEX IF NOT EXISTS idx_wiki_links_target ON wiki_links(target_page_id);

CREATE TABLE IF NOT EXISTS modules (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT,
    entry_points TEXT,
    depends_on TEXT,
    related_docs TEXT,
    rationale TEXT,
    signals TEXT,
    member_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary_vector BLOB,
    vector_input_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_modules_project ON modules(project_id);
CREATE INDEX IF NOT EXISTS idx_modules_name ON modules(name);

CREATE TABLE IF NOT EXISTS file_modules (
    file_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    project_id TEXT NOT NULL,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY (module_id) REFERENCES modules(id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, module_id)
);

CREATE INDEX IF NOT EXISTS idx_file_modules_module ON file_modules(module_id);
CREATE INDEX IF NOT EXISTS idx_file_modules_project ON file_modules(project_id);

CREATE TABLE IF NOT EXISTS conversation_meta (
    chunk_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    ts TEXT,
    files TEXT,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conv_meta_project ON conversation_meta(project_id);
CREATE INDEX IF NOT EXISTS idx_conv_meta_session ON conversation_meta(source, session_id);

CREATE TABLE IF NOT EXISTS qa_supersession (
    chunk_id TEXT PRIMARY KEY,
    superseded_by TEXT NOT NULL,
    project_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qa_supersession_project ON qa_supersession(project_id);

CREATE TABLE IF NOT EXISTS qa_revalidation (
    chunk_id TEXT PRIMARY KEY,
    cause_commit TEXT NOT NULL,
    changed_path TEXT NOT NULL,
    project_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qa_revalidation_project ON qa_revalidation(project_id);
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


@dataclass
class ModuleRecord:
    id: str
    project_id: str
    name: str
    summary: str | None = None
    entry_points: str | None = None
    depends_on: str | None = None
    related_docs: str | None = None
    rationale: str | None = None
    signals: str | None = None
    member_hash: str = ""
    updated_at: str = ""
    # Phase 5 Step C: semantic embedding of the card text (name + summary +
    # rationale). Raw float32 bytes of a unit-normalized vector; length is
    # (embedding_dim * 4). ``vector_input_hash`` fingerprints the text the
    # vector was computed over, so re-embedding is skipped when the card
    # text has not changed.
    summary_vector: bytes | None = None
    vector_input_hash: str | None = None


@dataclass
class ConversationMeta:
    """Agent-specific metadata for a ``node_type='conv_turn'`` chunk.

    Lives in the ``conversation_meta`` side table so the shared ``chunks``
    table stays free of conv-only columns. ``files`` is a JSON array of file
    paths the turn's tool calls touched — the conv↔code bridge seed for B.
    """

    chunk_id: str
    project_id: str
    source: str  # 'claude' | 'codex'
    session_id: str
    turn_index: int
    ts: str | None = None
    files: str | None = None


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
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO index_meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()
        else:
            self._migrate_schema(row["value"])

    def _migrate_schema(self, current_version: str) -> None:
        """Run schema migrations from current_version to SCHEMA_VERSION."""
        cur_ver = int(current_version)
        target_ver = int(SCHEMA_VERSION)
        if cur_ver >= target_ver:
            return

        if cur_ver < 3:
            # v2 → v3: add synthesis columns to wiki_pages
            # Safety: col/typedef are hardcoded literals below, never from user input.
            _SYNTH_COLUMNS = {
                "synthesis_model": "TEXT",
                "synthesis_version": "INTEGER DEFAULT 0",
                "synthesis_hash": "TEXT",
                "last_synthesized_at": "TEXT",
            }
            existing_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(wiki_pages)").fetchall()
            }
            for col, typedef in _SYNTH_COLUMNS.items():
                if col not in existing_cols:
                    self._conn.execute(
                        f"ALTER TABLE wiki_pages ADD COLUMN {col} {typedef}"
                    )
            logger.info("Migrated schema v2 → v3 (synthesis columns)")

        if cur_ver < 4:
            # v3 → v4: add call_edges.confidence_score and backfill from label.
            existing_edge_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(call_edges)").fetchall()
            }
            if "confidence_score" not in existing_edge_cols:
                self._conn.execute(
                    "ALTER TABLE call_edges ADD COLUMN confidence_score REAL DEFAULT 0.0"
                )
            # Backfill from existing confidence label so fusion gains signal without reindex.
            self._conn.execute(
                """UPDATE call_edges SET confidence_score = CASE confidence
                        WHEN 'high' THEN 1.0
                        WHEN 'medium' THEN 0.8
                        WHEN 'low' THEN 0.3
                        ELSE 0.0
                   END
                   WHERE confidence_score = 0.0 OR confidence_score IS NULL"""
            )
            logger.info("Migrated schema v3 → v4 (call_edges.confidence_score)")

        if cur_ver < 5:
            # v4 → v5: rename confidence labels to graphify-aligned semantics.
            #   low → ambiguous, medium → inferred, high → extracted.
            # Numeric confidence_score is unchanged (label is the only public surface).
            self._conn.execute(
                """UPDATE call_edges SET confidence = CASE confidence
                        WHEN 'high' THEN 'extracted'
                        WHEN 'medium' THEN 'inferred'
                        WHEN 'low' THEN 'ambiguous'
                        ELSE confidence
                   END
                   WHERE confidence IN ('low', 'medium', 'high')"""
            )
            logger.info("Migrated schema v4 → v5 (confidence label rename)")

        if cur_ver < 6:
            # v5 → v6: add modules + file_modules tables (Phase 5 subsystem retrieval).
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS modules (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    summary TEXT,
                    entry_points TEXT,
                    depends_on TEXT,
                    related_docs TEXT,
                    rationale TEXT,
                    signals TEXT,
                    member_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_modules_project ON modules(project_id);
                CREATE INDEX IF NOT EXISTS idx_modules_name ON modules(name);

                CREATE TABLE IF NOT EXISTS file_modules (
                    file_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    project_id TEXT NOT NULL,
                    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
                    FOREIGN KEY (module_id) REFERENCES modules(id) ON DELETE CASCADE,
                    PRIMARY KEY (file_id, module_id)
                );
                CREATE INDEX IF NOT EXISTS idx_file_modules_module ON file_modules(module_id);
                CREATE INDEX IF NOT EXISTS idx_file_modules_project ON file_modules(project_id);
                """
            )
            logger.info("Migrated schema v5 → v6 (modules + file_modules tables)")

        if cur_ver < 7:
            # v6 → v7: module card embedding columns. Lets modules_search blend
            # token overlap with semantic cosine so Korean NL queries can match
            # English module names without the hand-curated alias list carrying
            # all the weight.
            existing_module_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(modules)").fetchall()
            }
            if "summary_vector" not in existing_module_cols:
                self._conn.execute(
                    "ALTER TABLE modules ADD COLUMN summary_vector BLOB"
                )
            if "vector_input_hash" not in existing_module_cols:
                self._conn.execute(
                    "ALTER TABLE modules ADD COLUMN vector_input_hash TEXT"
                )
            logger.info("Migrated schema v6 → v7 (modules.summary_vector + vector_input_hash)")

        if cur_ver < 8:
            # v7 → v8: conversation_meta side table for conv_turn chunks
            # (cross-tool conversation indexer, A3). Conv turns live in the
            # shared chunks table; their source/session/turn metadata lives here.
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_meta (
                    chunk_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    ts TEXT,
                    files TEXT,
                    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_conv_meta_project ON conversation_meta(project_id);
                CREATE INDEX IF NOT EXISTS idx_conv_meta_session ON conversation_meta(source, session_id);
                """
            )
            logger.info("Migrated schema v7 → v8 (conversation_meta table)")

        self._conn.execute(
            "UPDATE index_meta SET value = ? WHERE key = 'schema_version'",
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

    def wiki_store(self, max_pages: int = 100) -> "WikiStore":
        """Create a WikiStore bound to this database's connection."""
        from hybrid_search.storage.wiki import WikiStore
        return WikiStore(self._conn, max_pages=max_pages)

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
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO files
               (id, project_id, relative_path, file_hash, file_size, file_mtime, language, last_modified, chunk_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   project_id = excluded.project_id,
                   relative_path = excluded.relative_path,
                   file_hash = excluded.file_hash,
                   file_size = excluded.file_size,
                   file_mtime = excluded.file_mtime,
                   language = excluded.language,
                   last_modified = excluded.last_modified,
                   chunk_count = excluded.chunk_count""",
            (
                record.id,
                record.project_id,
                record.relative_path,
                record.file_hash,
                record.file_size,
                record.file_mtime,
                record.language,
                now,
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
        calls: list[tuple[str, str | None]],
        project_id: str,
    ) -> None:
        """Insert call edges from a chunk's extracted calls (name, module) tuples."""
        if not calls:
            return
        conn.executemany(
            """INSERT INTO call_edges
               (caller_chunk_id, callee_name, callee_module, project_id, confidence)
               VALUES (?, ?, ?, ?, 'ambiguous')""",
            [(caller_chunk_id, name, module, project_id) for name, module in calls],
        )

    def delete_call_edges_by_caller(self, conn: sqlite3.Connection, chunk_id: str) -> None:
        """Delete all call edges where this chunk is the caller."""
        conn.execute("DELETE FROM call_edges WHERE caller_chunk_id = ?", (chunk_id,))

    def delete_call_edges_by_callee(self, conn: sqlite3.Connection, chunk_id: str) -> None:
        """Delete dangling call edges that reference a deleted callee chunk."""
        conn.execute("DELETE FROM call_edges WHERE callee_chunk_id = ?", (chunk_id,))

    def delete_all_call_edges(self, conn: sqlite3.Connection, project_id: str) -> None:
        """Delete all call edges for a project."""
        conn.execute("DELETE FROM call_edges WHERE project_id = ?", (project_id,))

    def get_chunks_by_file(self, file_id: str) -> list[ChunkRecord]:
        """Get all chunks for a file, ordered by start_line."""
        cur = self._conn.execute(
            "SELECT * FROM chunks WHERE file_id = ? ORDER BY start_line", (file_id,)
        )
        return [self._row_to_chunk(row) for row in cur.fetchall()]

    def get_chunk_ids_by_file(self, file_id: str) -> list[str]:
        """Get all chunk IDs for a file (read-only, no transaction needed)."""
        cur = self._conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
        return [row["id"] for row in cur.fetchall()]

    def delete_chunks_by_file(self, conn: sqlite3.Connection, file_id: str) -> list[str]:
        """Delete all chunks for a file, returning their IDs."""
        cur = conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
        chunk_ids = [row["id"] for row in cur.fetchall()]
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        return chunk_ids

    def delete_chunks_by_ids(self, conn: sqlite3.Connection, chunk_ids: list[str]) -> None:
        """Delete specific chunks by id (FK cascades to conversation_meta).

        Used for incremental conversation re-indexing — only stale turns are
        dropped, leaving unchanged turns (and their embeddings) in place.
        """
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", tuple(chunk_ids))

    def get_chunks_by_project(self, project_id: str) -> list[ChunkRecord]:
        cur = self._conn.execute("SELECT * FROM chunks WHERE project_id = ?", (project_id,))
        return [self._row_to_chunk(row) for row in cur.fetchall()]

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        cur = self._conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,))
        row = cur.fetchone()
        return self._row_to_chunk(row) if row else None

    def get_chunks_by_node_type(
        self, project_id: str, node_type: str
    ) -> list[ChunkRecord]:
        cur = self._conn.execute(
            "SELECT * FROM chunks WHERE project_id = ? AND node_type = ?",
            (project_id, node_type),
        )
        return [self._row_to_chunk(row) for row in cur.fetchall()]

    # --- qa supersession (R1 exposure fix) --------------------------------
    # Index-time mapping "stale qa chunk -> newest same-topic qa chunk",
    # computed over the whole qa corpus by memory/supersession.py. The
    # orchestrator reads it at query time to splice the correction in
    # next to a stale hit the retrievers surfaced on their own.

    def replace_qa_supersession(
        self, conn: sqlite3.Connection, project_id: str, mapping: dict[str, str]
    ) -> None:
        """Overwrite the project's supersession rows with ``mapping``."""
        conn.execute(
            "DELETE FROM qa_supersession WHERE project_id = ?", (project_id,)
        )
        if mapping:
            conn.executemany(
                "INSERT OR REPLACE INTO qa_supersession "
                "(chunk_id, superseded_by, project_id) VALUES (?, ?, ?)",
                [(old, new, project_id) for old, new in mapping.items()],
            )

    def get_qa_superseding(self, chunk_ids: list[str]) -> dict[str, str]:
        """``{chunk_id: superseding chunk_id}`` for the ids that have one."""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        cur = self._conn.execute(
            f"SELECT chunk_id, superseded_by FROM qa_supersession "
            f"WHERE chunk_id IN ({placeholders})",
            tuple(chunk_ids),
        )
        return {row["chunk_id"]: row["superseded_by"] for row in cur.fetchall()}

    # --- qa revalidation (P1-2 commit-aware invalidation) ------------------
    # Rows flag qa chunks whose anchored files changed in a later commit.
    # Side table on purpose: rewriting qa frontmatter would change the
    # content hash and re-embed every flagged memory.

    def add_qa_revalidations(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        rows: list[tuple[str, str, str]],
    ) -> None:
        """Upsert ``(chunk_id, cause_commit, changed_path)`` flags."""
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO qa_revalidation "
                "(chunk_id, cause_commit, changed_path, project_id) "
                "VALUES (?, ?, ?, ?)",
                [(c, cause, path, project_id) for c, cause, path in rows],
            )

    def get_qa_revalidations(
        self, chunk_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """``{chunk_id: (cause_commit, changed_path)}`` for flagged ids."""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        cur = self._conn.execute(
            f"SELECT chunk_id, cause_commit, changed_path FROM qa_revalidation "
            f"WHERE chunk_id IN ({placeholders})",
            tuple(chunk_ids),
        )
        return {
            row["chunk_id"]: (row["cause_commit"], row["changed_path"])
            for row in cur.fetchall()
        }

    def prune_orphan_qa_revalidations(self, conn: sqlite3.Connection) -> int:
        """Drop flags whose qa chunk no longer exists in the store."""
        cur = conn.execute(
            "DELETE FROM qa_revalidation WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.id = qa_revalidation.chunk_id)"
        )
        return cur.rowcount

    # Memory/derived lanes are excluded on purpose: a past *question* about
    # an absent topic echoes the topic word into qa/conv chunks, and the
    # whole point of this probe is "has the project's own source ever seen
    # this term". Source of truth = code + docs only.
    _SOURCE_EXCLUDED_NODE_TYPES = (
        "qa_log", "memory_card", "domain_term", "episodic_example",
        "commit", "conv_turn",
    )

    def source_contains_substring(self, project_id: str, needle: str) -> bool:
        """True when any code/doc chunk's content contains ``needle``.

        Case-insensitive for ASCII (SQLite LIKE default), exact for
        Hangul. Present terms return in ~1 ms (LIKE stops at the first
        hit); a genuinely absent term costs one full content scan
        (~150 ms on an 8k-chunk store) — callers should short-circuit.
        """
        escaped = (
            needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        placeholders = ",".join("?" for _ in self._SOURCE_EXCLUDED_NODE_TYPES)
        cur = self._conn.execute(
            f"""SELECT 1 FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE c.project_id = ?
                  AND c.node_type NOT IN ({placeholders})
                  AND f.relative_path NOT LIKE '.hybrid-search/%'
                  AND f.relative_path NOT LIKE '.conversations/%'
                  AND c.content LIKE ? ESCAPE '\\'
                LIMIT 1""",
            (project_id, *self._SOURCE_EXCLUDED_NODE_TYPES, f"%{escaped}%"),
        )
        return cur.fetchone() is not None

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

    # -- call graph queries --

    def get_callers(
        self,
        chunk_id: str,
        project_id: str | None = None,
        min_confidence: str = "ambiguous",
    ) -> list[dict]:
        """Find all chunks that call the given chunk (reverse call graph)."""
        conf_levels = _confidence_filter(min_confidence)
        placeholders = ",".join("?" for _ in conf_levels)
        if project_id:
            cur = self._conn.execute(
                f"""SELECT ce.caller_chunk_id, ce.callee_name, ce.confidence,
                           ce.confidence_score,
                           c.name, c.qualified_name, c.node_type,
                           c.start_line, c.end_line,
                           f.relative_path
                    FROM call_edges ce
                    JOIN chunks c ON c.id = ce.caller_chunk_id
                    JOIN files f ON f.id = c.file_id
                    WHERE ce.callee_chunk_id = ?
                      AND ce.project_id = ?
                      AND ce.confidence IN ({placeholders})""",
                (chunk_id, project_id, *conf_levels),
            )
        else:
            cur = self._conn.execute(
                f"""SELECT ce.caller_chunk_id, ce.callee_name, ce.confidence,
                           ce.confidence_score,
                           c.name, c.qualified_name, c.node_type,
                           c.start_line, c.end_line,
                           f.relative_path
                    FROM call_edges ce
                    JOIN chunks c ON c.id = ce.caller_chunk_id
                    JOIN files f ON f.id = c.file_id
                    WHERE ce.callee_chunk_id = ?
                      AND ce.confidence IN ({placeholders})""",
                (chunk_id, *conf_levels),
            )
        return [dict(row) for row in cur.fetchall()]

    def get_callers_by_name(
        self,
        symbol: str,
        project_id: str | None = None,
        min_confidence: str = "ambiguous",
    ) -> list[dict]:
        """Find callers by callee symbol name (when chunk_id not available)."""
        conf_levels = _confidence_filter(min_confidence)
        placeholders = ",".join("?" for _ in conf_levels)
        if project_id:
            cur = self._conn.execute(
                f"""SELECT ce.caller_chunk_id, ce.callee_name,
                           ce.callee_chunk_id, ce.confidence,
                           ce.confidence_score,
                           c.name, c.qualified_name, c.node_type,
                           c.start_line, c.end_line,
                           f.relative_path
                    FROM call_edges ce
                    JOIN chunks c ON c.id = ce.caller_chunk_id
                    JOIN files f ON f.id = c.file_id
                    WHERE (ce.callee_name = ? OR ce.callee_qualified_name = ?)
                      AND ce.project_id = ?
                      AND ce.confidence IN ({placeholders})""",
                (symbol, symbol, project_id, *conf_levels),
            )
        else:
            cur = self._conn.execute(
                f"""SELECT ce.caller_chunk_id, ce.callee_name,
                           ce.callee_chunk_id, ce.confidence,
                           ce.confidence_score,
                           c.name, c.qualified_name, c.node_type,
                           c.start_line, c.end_line,
                           f.relative_path
                    FROM call_edges ce
                    JOIN chunks c ON c.id = ce.caller_chunk_id
                    JOIN files f ON f.id = c.file_id
                    WHERE (ce.callee_name = ? OR ce.callee_qualified_name = ?)
                      AND ce.confidence IN ({placeholders})""",
                (symbol, symbol, *conf_levels),
            )
        return [dict(row) for row in cur.fetchall()]

    def get_callees(
        self,
        chunk_id: str,
        project_id: str | None = None,
        min_confidence: str = "ambiguous",
    ) -> list[dict]:
        """Find all chunks called by the given chunk (forward call graph)."""
        conf_levels = _confidence_filter(min_confidence)
        placeholders = ",".join("?" for _ in conf_levels)
        if project_id:
            cur = self._conn.execute(
                f"""SELECT ce.callee_chunk_id, ce.callee_name,
                           ce.callee_qualified_name, ce.confidence,
                           ce.confidence_score, ce.callee_module,
                           c.name, c.qualified_name, c.node_type,
                           c.start_line, c.end_line,
                           f.relative_path
                    FROM call_edges ce
                    LEFT JOIN chunks c ON c.id = ce.callee_chunk_id
                    LEFT JOIN files f ON f.id = c.file_id
                    WHERE ce.caller_chunk_id = ?
                      AND ce.project_id = ?
                      AND ce.confidence IN ({placeholders})""",
                (chunk_id, project_id, *conf_levels),
            )
        else:
            cur = self._conn.execute(
                f"""SELECT ce.callee_chunk_id, ce.callee_name,
                           ce.callee_qualified_name, ce.confidence,
                           ce.confidence_score, ce.callee_module,
                           c.name, c.qualified_name, c.node_type,
                           c.start_line, c.end_line,
                           f.relative_path
                    FROM call_edges ce
                    LEFT JOIN chunks c ON c.id = ce.callee_chunk_id
                    LEFT JOIN files f ON f.id = c.file_id
                    WHERE ce.caller_chunk_id = ?
                      AND ce.confidence IN ({placeholders})""",
                (chunk_id, *conf_levels),
            )
        return [dict(row) for row in cur.fetchall()]

    def get_all_call_edges(self, project_id: str) -> list[dict]:
        """Get all call edges for a project (for batch resolution)."""
        cur = self._conn.execute(
            """SELECT rowid, caller_chunk_id, callee_name, callee_qualified_name,
                      callee_chunk_id, callee_module, confidence, confidence_score
               FROM call_edges WHERE project_id = ?""",
            (project_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    def update_call_edge_resolution(
        self,
        conn: sqlite3.Connection,
        rowid: int,
        callee_chunk_id: str,
        callee_qualified_name: str | None,
        confidence: str,
        confidence_score: float = 0.0,
    ) -> None:
        """Update a call edge with resolved chunk ID, confidence label, and numeric score."""
        conn.execute(
            """UPDATE call_edges
               SET callee_chunk_id = ?, callee_qualified_name = ?,
                   confidence = ?, confidence_score = ?
               WHERE rowid = ?""",
            (callee_chunk_id, callee_qualified_name, confidence, confidence_score, rowid),
        )

    def get_chunk_authority_scores(self, project_id: str) -> dict[str, float]:
        """Return callee_chunk_id → max incoming confidence_score for a project.

        Used by the search orchestrator to nudge fusion ranks toward chunks that
        are called via high-confidence edges. Chunks with no incoming edges are
        absent from the result (fusion treats them as neutral).
        """
        cur = self._conn.execute(
            """SELECT callee_chunk_id, MAX(confidence_score) AS score
               FROM call_edges
               WHERE project_id = ? AND callee_chunk_id IS NOT NULL
               GROUP BY callee_chunk_id""",
            (project_id,),
        )
        return {row["callee_chunk_id"]: row["score"] for row in cur.fetchall()}

    def get_god_nodes(
        self,
        project_id: str,
        limit: int = 20,
        min_confidence: str = "inferred",
    ) -> list[dict]:
        """Return top-N chunks by in-degree (distinct callers), tiebreak by max confidence_score.

        'God nodes' = high-authority chunks — the most-called functions/classes
        in a project. Used by M5 graph exploration (CLI + /search routing).
        """
        conf_levels = _confidence_filter(min_confidence)
        placeholders = ",".join("?" for _ in conf_levels)
        cur = self._conn.execute(
            f"""SELECT c.id, c.name, c.qualified_name, c.node_type,
                       c.start_line, c.end_line,
                       f.relative_path,
                       COUNT(DISTINCT ce.caller_chunk_id) AS in_degree,
                       MAX(ce.confidence_score) AS max_score
                FROM chunks c
                JOIN call_edges ce ON ce.callee_chunk_id = c.id
                JOIN files f ON f.id = c.file_id
                WHERE c.project_id = ?
                  AND ce.confidence IN ({placeholders})
                GROUP BY c.id
                ORDER BY in_degree DESC, max_score DESC, c.qualified_name
                LIMIT ?""",
            (project_id, *conf_levels, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def find_chunk_by_qualified_name(
        self, qualified_name: str, project_id: str
    ) -> ChunkRecord | None:
        """Find a chunk by exact qualified_name within a project."""
        cur = self._conn.execute(
            "SELECT * FROM chunks WHERE qualified_name = ? AND project_id = ?",
            (qualified_name, project_id),
        )
        row = cur.fetchone()
        return self._row_to_chunk(row) if row else None

    def find_chunks_by_name(
        self, name: str, project_id: str
    ) -> list[ChunkRecord]:
        """Find chunks by exact name within a project."""
        cur = self._conn.execute(
            "SELECT * FROM chunks WHERE name = ? AND project_id = ?",
            (name, project_id),
        )
        return [self._row_to_chunk(row) for row in cur.fetchall()]

    def has_chunk_matching_name(self, name_pattern: str, project_id: str) -> bool:
        """Check if any chunk has a qualified_name matching the pattern (LIKE)."""
        cur = self._conn.execute(
            "SELECT 1 FROM chunks WHERE qualified_name LIKE ? AND project_id = ? LIMIT 1",
            (f"%{name_pattern}%", project_id),
        )
        return cur.fetchone() is not None

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

    # ---------- Conversation meta (A3) ----------

    def upsert_conversation_meta(
        self, conn: sqlite3.Connection, records: list[ConversationMeta]
    ) -> None:
        """Insert/replace conv_turn metadata. Idempotent on chunk_id."""
        if not records:
            return
        conn.executemany(
            """INSERT OR REPLACE INTO conversation_meta
               (chunk_id, project_id, source, session_id, turn_index, ts, files)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (r.chunk_id, r.project_id, r.source, r.session_id,
                 r.turn_index, r.ts, r.files)
                for r in records
            ],
        )

    def get_conversation_meta(self, chunk_id: str) -> ConversationMeta | None:
        cur = self._conn.execute(
            "SELECT * FROM conversation_meta WHERE chunk_id = ?", (chunk_id,)
        )
        row = cur.fetchone()
        return self._row_to_conversation_meta(row) if row else None

    def get_conversation_meta_batch(
        self, chunk_ids: list[str]
    ) -> dict[str, ConversationMeta]:
        """Batch lookup for enrichment — returns only chunk_ids that exist."""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        cur = self._conn.execute(
            f"SELECT * FROM conversation_meta WHERE chunk_id IN ({placeholders})",
            tuple(chunk_ids),
        )
        return {
            row["chunk_id"]: self._row_to_conversation_meta(row)
            for row in cur.fetchall()
        }

    def _row_to_conversation_meta(self, row: sqlite3.Row) -> ConversationMeta:
        return ConversationMeta(
            chunk_id=row["chunk_id"],
            project_id=row["project_id"],
            source=row["source"],
            session_id=row["session_id"],
            turn_index=row["turn_index"],
            ts=row["ts"],
            files=row["files"],
        )

    # ---------- Modules (Phase 5) ----------

    def upsert_module(self, conn: sqlite3.Connection, record: "ModuleRecord") -> None:
        conn.execute(
            """INSERT INTO modules (id, project_id, name, summary, entry_points,
                depends_on, related_docs, rationale, signals, member_hash, updated_at,
                summary_vector, vector_input_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name,
                 summary=excluded.summary,
                 entry_points=excluded.entry_points,
                 depends_on=excluded.depends_on,
                 related_docs=excluded.related_docs,
                 rationale=excluded.rationale,
                 signals=excluded.signals,
                 member_hash=excluded.member_hash,
                 updated_at=excluded.updated_at,
                 summary_vector=excluded.summary_vector,
                 vector_input_hash=excluded.vector_input_hash""",
            (
                record.id, record.project_id, record.name, record.summary,
                record.entry_points, record.depends_on, record.related_docs,
                record.rationale, record.signals, record.member_hash, record.updated_at,
                record.summary_vector, record.vector_input_hash,
            ),
        )

    def update_module_vector(
        self,
        conn: sqlite3.Connection,
        module_id: str,
        summary_vector: bytes,
        vector_input_hash: str,
    ) -> None:
        """Write only the embedding fields — avoids rewriting text columns
        when synthesis hasn't otherwise changed the card."""
        conn.execute(
            "UPDATE modules SET summary_vector = ?, vector_input_hash = ? WHERE id = ?",
            (summary_vector, vector_input_hash, module_id),
        )

    def set_file_modules(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        rows: list[tuple[str, str, float]],
    ) -> None:
        """Replace all file_modules rows for this project."""
        conn.execute("DELETE FROM file_modules WHERE project_id = ?", (project_id,))
        if not rows:
            return
        conn.executemany(
            "INSERT INTO file_modules (file_id, module_id, weight, project_id) VALUES (?, ?, ?, ?)",
            [(fid, mid, w, project_id) for fid, mid, w in rows],
        )

    def delete_project_modules(self, conn: sqlite3.Connection, project_id: str) -> None:
        conn.execute("DELETE FROM file_modules WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM modules WHERE project_id = ?", (project_id,))

    def get_modules(self, project_id: str) -> list["ModuleRecord"]:
        cur = self._conn.execute(
            "SELECT * FROM modules WHERE project_id = ? ORDER BY name", (project_id,),
        )
        return [self._row_to_module(r) for r in cur.fetchall()]

    def get_module(self, module_id: str) -> "ModuleRecord | None":
        cur = self._conn.execute("SELECT * FROM modules WHERE id = ?", (module_id,))
        row = cur.fetchone()
        return self._row_to_module(row) if row else None

    def get_module_count(self, project_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM modules WHERE project_id = ?", (project_id,),
        )
        return cur.fetchone()[0]

    def get_files_by_module(self, module_id: str) -> list[str]:
        """Return file_ids in this module, highest weight first."""
        cur = self._conn.execute(
            "SELECT file_id FROM file_modules WHERE module_id = ? ORDER BY weight DESC",
            (module_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def get_modules_by_file(self, file_id: str) -> list[str]:
        cur = self._conn.execute(
            "SELECT module_id FROM file_modules WHERE file_id = ? ORDER BY weight DESC",
            (file_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def search_modules_by_name(
        self, name_pattern: str, project_id: str, limit: int = 20,
    ) -> list["ModuleRecord"]:
        cur = self._conn.execute(
            """SELECT * FROM modules
               WHERE project_id = ?
                 AND (name LIKE ? OR summary LIKE ?)
               ORDER BY LENGTH(name)
               LIMIT ?""",
            (project_id, f"%{name_pattern}%", f"%{name_pattern}%", limit),
        )
        return [self._row_to_module(r) for r in cur.fetchall()]

    def _row_to_module(self, row: sqlite3.Row) -> "ModuleRecord":
        # v7 columns may not exist on older rows that predate the migration
        # check — tolerate missing keys rather than failing reads.
        keys = set(row.keys())
        return ModuleRecord(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            summary=row["summary"],
            entry_points=row["entry_points"],
            depends_on=row["depends_on"],
            related_docs=row["related_docs"],
            rationale=row["rationale"],
            signals=row["signals"],
            member_hash=row["member_hash"],
            updated_at=row["updated_at"],
            summary_vector=row["summary_vector"] if "summary_vector" in keys else None,
            vector_input_hash=row["vector_input_hash"] if "vector_input_hash" in keys else None,
        )
