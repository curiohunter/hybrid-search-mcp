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
    parse_claude_transcript,
    parse_codex_session,
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


def conv_file_id(project_id: str, rel_path: str) -> str:
    """Synthetic file id for a session's conv chunks — the dedup key.

    Shared with the query-time in-flight overlay so both sides derive the same
    id; if this formula ever changes, dedup stays consistent in one edit.
    """
    return hashlib.sha256(f"{project_id}:{rel_path}".encode()).hexdigest()[:16]


def _session_hash(chunks: list[ConvChunk]) -> str:
    """Aggregate fingerprint of a session's turns — the delta key."""
    joined = "\n".join(c.text for c in chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _chunk_id(source: str, session_id: str, chunk: ConvChunk) -> str:
    digest = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()[:8]
    return f"conv:{source}:{session_id}:{chunk.turn_index:04d}:{digest}"


def _detect_source(transcript_path: Path) -> str:
    """Codex sessions open with a ``session_meta`` record; Claude do not."""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                return "codex" if rec.get("type") == "session_meta" else "claude"
    except OSError:
        pass
    return "claude"


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
        abs_path, pid, name = self._resolve_project(project_path, project_name)
        chunks = collect_project_chunks(
            abs_path, claude_root=claude_root, codex_root=codex_root
        )
        return self._index_chunks(pid, name, chunks)

    def index_transcript(
        self,
        transcript_path: str | Path,
        project_path: str,
        source: str | None = None,
    ) -> ConvIndexingResult:
        """Index a single transcript file (one session) — the per-turn hook path."""
        abs_path, pid, name = self._resolve_project(project_path, project_path)
        tpath = Path(transcript_path)
        if not tpath.is_file():
            return ConvIndexingResult(project_id=pid, project_name=name)

        src = source if source in ("claude", "codex") else _detect_source(tpath)
        chunks = parse_codex_session(tpath) if src == "codex" else parse_claude_transcript(tpath)
        return self._index_chunks(pid, name, chunks)

    def _resolve_project(
        self, project_path: str, project_name: str | None
    ) -> tuple[Path, str, str]:
        abs_path = Path(project_path).resolve()
        if not abs_path.is_dir():
            raise ValueError(f"Project path does not exist: {abs_path}")
        pid = project_hash(str(abs_path))
        name = (project_name if project_name and project_name != project_path else None) or abs_path.name
        self._registry.register(name, str(abs_path))
        return abs_path, pid, name

    def _index_chunks(
        self, pid: str, name: str, chunks: list[ConvChunk]
    ) -> ConvIndexingResult:
        idx_paths = IndexPaths(get_project_dir(self._config.projects_dir, pid))
        idx_paths.ensure_dirs()
        db = StoreDB(idx_paths.store_db)
        bm25 = BM25Engine(idx_paths.tantivy_dir)
        vector = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)
        result = ConvIndexingResult(project_id=pid, project_name=name)

        try:
            sessions: dict[tuple[str, str], list[ConvChunk]] = defaultdict(list)
            for chunk in chunks:
                sessions[(chunk.source, chunk.session_id)].append(chunk)

            for (source, session_id), session_chunks in sessions.items():
                session_chunks.sort(key=lambda c: c.turn_index)
                added = self._index_session(
                    db, bm25, vector, pid, source, session_id, session_chunks
                )
                if added is None:
                    result.sessions_skipped += 1
                else:
                    result.sessions_indexed += 1
                    result.chunks_total += added

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
    ) -> int | None:
        """Incrementally store one session.

        Returns None if skipped (unchanged), else the number of newly embedded
        turns. Only turns whose chunk id (turn + content hash) is new get
        embedded; unchanged turns keep their existing chunks/vectors, so a Stop
        hook firing every turn re-embeds just the latest turn, not the session.
        """
        if not chunks:
            return None

        rel_path = _conv_rel_path(source, session_id)
        file_id = conv_file_id(project_id, rel_path)
        new_hash = _session_hash(chunks)

        existing = db.get_file_by_path(project_id, rel_path)
        if existing is not None and existing.file_hash == new_hash:
            return None  # delta: unchanged session, no work

        existing_ids = set(db.get_chunk_ids_by_file(file_id))
        desired = [(_chunk_id(source, session_id, c), c) for c in chunks]
        desired_ids = {cid for cid, _ in desired}
        to_add = [(cid, c) for cid, c in desired if cid not in existing_ids]
        to_delete = [cid for cid in existing_ids if cid not in desired_ids]

        embeddings = (
            self._embedder.embed_texts([c.text for _, c in to_add]) if to_add else None
        )

        with db.transaction() as conn:
            # Placeholder file row (FK target). Final hash written after chunks
            # land, so a crash leaves file_hash="" → re-indexed next run.
            db.upsert_file(conn, FileRecord(
                id=file_id, project_id=project_id, relative_path=rel_path,
                file_hash="", language="conversation", chunk_count=0,
            ))
            if to_delete:
                db.delete_chunks_by_ids(conn, to_delete)  # cascades conversation_meta
            if to_add:
                db.insert_chunks(conn, [
                    ChunkRecord(
                        id=cid, file_id=file_id, project_id=project_id,
                        name=(c.user_prompt or "")[:_NAME_MAX],
                        qualified_name=f"{source}:{session_id}#{c.turn_index}",
                        node_type=CONV_NODE_TYPE,
                        content=c.text, embedding_input=c.text,
                    )
                    for cid, c in to_add
                ])
                db.upsert_conversation_meta(conn, [
                    ConversationMeta(
                        chunk_id=cid, project_id=project_id, source=source,
                        session_id=session_id, turn_index=c.turn_index,
                        ts=c.timestamp or None, files=json.dumps(list(c.files)),
                    )
                    for cid, c in to_add
                ])
            db.upsert_file(conn, FileRecord(
                id=file_id, project_id=project_id, relative_path=rel_path,
                file_hash=new_hash, language="conversation", chunk_count=len(chunks),
            ))

        if to_delete:
            bm25.delete_batch(to_delete)
            vector.remove_batch(to_delete)
        if to_add:
            for cid, c in to_add:
                bm25.add(
                    chunk_id=cid, name=c.user_prompt[:_NAME_MAX],
                    qualified_name=f"{source}:{session_id}#{c.turn_index}",
                    content=c.text, docstring=None,
                )
            vector.add_batch([cid for cid, _ in to_add], embeddings)
        return len(to_add)
