"""A4 — index external agent transcripts into the unified stores.

Conversation turns come from outside the project tree (Claude Code + Codex
JSONL), so they bypass the file scanner. This indexer reads them via
``transcript_source``, embeds each turn, and writes to all three stores:

- SQLite ``chunks`` with ``node_type='conv_turn'`` (unified store)
- ``conversation_meta`` side table (source / session / turn / ts / files)
- BM25 (Tantivy) + vector (USearch)

Writing to all three keeps the project-wide chunk==vector==bm25 invariant the
file pipeline enforces. Lane *separation* happens at query time (filter by
node_type), per the 2026-04-16 design — never by holding conv out of the index.

Each session maps to one synthetic file under the reserved
``.conversations/<source>/<session>.jsonl`` path; its ``file_hash`` is an
aggregate of the turn texts, so an unchanged session is skipped without
re-embedding (delta), and a changed session fully replaces its chunks.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from hybrid_search.config import Config
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.transcript_source import (
    ConvChunk,
    collect_project_chunks,
)
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import (
    ChunkRecord,
    ConversationMeta,
    FileRecord,
    StoreDB,
)
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger(__name__)

CONV_NODE_TYPE = "conv_turn"
_NAME_MAX = 80


@dataclass
class ConvIndexingResult:
    project_id: str
    project_name: str
    sessions_indexed: int = 0
    sessions_skipped: int = 0
    chunks_total: int = 0


def _conv_rel_path(source: str, session_id: str) -> str:
    return f".conversations/{source}/{session_id}.jsonl"


def _session_hash(chunks: list[ConvChunk]) -> str:
    """Aggregate fingerprint of a session's turns — the delta key."""
    joined = "\n".join(c.text for c in chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _chunk_id(source: str, session_id: str, chunk: ConvChunk) -> str:
    digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()[:8]
    return f"conv:{source}:{session_id}:{chunk.turn_index:04d}:{digest}"


class ConversationIndexer:
    """Indexes Claude Code + Codex transcripts for a project."""

    def __init__(self, config: Config, registry: ProjectRegistry, embedder: Embedder) -> None:
        self._config = config
        self._registry = registry
        self._embedder = embedder

    def index_conversations(
        self,
        project_path: str,
        project_name: str | None = None,
        claude_root: Path | None = None,
        codex_root: Path | None = None,
    ) -> ConvIndexingResult:
        """Discover, embed, and store all conversation turns for a project."""
        abs_path = Path(project_path).resolve()
        if not abs_path.is_dir():
            raise ValueError(f"Project path does not exist: {abs_path}")

        pid = project_hash(str(abs_path))
        name = project_name or abs_path.name
        self._registry.register(name, str(abs_path))

        idx_paths = IndexPaths(get_project_dir(self._config.projects_dir, pid))
        idx_paths.ensure_dirs()

        db = StoreDB(idx_paths.store_db)
        bm25 = BM25Engine(idx_paths.tantivy_dir)
        vector = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)
        result = ConvIndexingResult(project_id=pid, project_name=name)

        try:
            chunks = collect_project_chunks(
                abs_path, claude_root=claude_root, codex_root=codex_root
            )
            sessions: dict[tuple[str, str], list[ConvChunk]] = defaultdict(list)
            for chunk in chunks:
                sessions[(chunk.source, chunk.session_id)].append(chunk)

            for (source, session_id), session_chunks in sessions.items():
                session_chunks.sort(key=lambda c: c.turn_index)
                if self._index_session(db, bm25, vector, pid, source, session_id, session_chunks):
                    result.sessions_indexed += 1
                    result.chunks_total += len(session_chunks)
                else:
                    result.sessions_skipped += 1

            bm25.commit()
            vector.save()
        finally:
            db.close()

        logger.info(
            "Conversation indexing for %s: %d sessions indexed, %d skipped, %d chunks",
            name, result.sessions_indexed, result.sessions_skipped, result.chunks_total,
        )
        return result

    def _index_session(
        self,
        db: StoreDB,
        bm25: BM25Engine,
        vector: VectorEngine,
        project_id: str,
        source: str,
        session_id: str,
        chunks: list[ConvChunk],
    ) -> bool:
        """Store one session. Returns False if skipped (unchanged), True if (re)indexed."""
        if not chunks:
            return False

        rel_path = _conv_rel_path(source, session_id)
        file_id = hashlib.sha256(f"{project_id}:{rel_path}".encode()).hexdigest()[:16]
        new_hash = _session_hash(chunks)

        existing = db.get_file_by_path(project_id, rel_path)
        if existing is not None and existing.file_hash == new_hash:
            return False  # delta: unchanged session, no re-embed

        old_chunk_ids = db.get_chunk_ids_by_file(file_id)
        chunk_ids = [_chunk_id(source, session_id, c) for c in chunks]
        embeddings = self._embedder.embed_texts([c.text for c in chunks])

        with db.transaction() as conn:
            # Placeholder file row (FK target). Final hash written after chunks
            # land, so a crash leaves file_hash="" → re-indexed next run.
            db.upsert_file(conn, FileRecord(
                id=file_id, project_id=project_id, relative_path=rel_path,
                file_hash="", language="conversation", chunk_count=0,
            ))
            db.delete_chunks_by_file(conn, file_id)  # cascades conversation_meta

            db.insert_chunks(conn, [
                ChunkRecord(
                    id=cid, file_id=file_id, project_id=project_id,
                    name=(c.user_prompt or "")[:_NAME_MAX],
                    qualified_name=f"{source}:{session_id}#{c.turn_index}",
                    node_type=CONV_NODE_TYPE,
                    content=c.text, embedding_input=c.text,
                )
                for cid, c in zip(chunk_ids, chunks)
            ])
            db.upsert_conversation_meta(conn, [
                ConversationMeta(
                    chunk_id=cid, project_id=project_id, source=source,
                    session_id=session_id, turn_index=c.turn_index,
                    ts=c.timestamp or None, files=json.dumps(list(c.files)),
                )
                for cid, c in zip(chunk_ids, chunks)
            ])
            db.upsert_file(conn, FileRecord(
                id=file_id, project_id=project_id, relative_path=rel_path,
                file_hash=new_hash, language="conversation", chunk_count=len(chunks),
            ))

        if old_chunk_ids:
            bm25.delete_batch(old_chunk_ids)
            vector.remove_batch(old_chunk_ids)
        for cid, c in zip(chunk_ids, chunks):
            bm25.add(
                chunk_id=cid, name=c.user_prompt[:_NAME_MAX],
                qualified_name=f"{source}:{session_id}#{c.turn_index}",
                content=c.text, docstring=None,
            )
        vector.add_batch(chunk_ids, embeddings)
        return True
