"""Carry conversation sessions from the previous index into a rebuilt one.

An atomic rebuild reconstructs the index from disk. Conversation chunks live
in a synthetic namespace with no on-disk file, so the rebuild itself cannot
produce them; the caller re-derives them from the agent transcripts afterwards.
That only restores what the transcripts still hold — and Claude Code deletes
transcripts older than ``cleanupPeriodDays`` (30 by default). Every rebuild
therefore silently cut conversation memory back to the last month.

The previous index is the only remaining copy of those sessions, and it is
still intact until the directory swap. This module copies every conversation
session out of it into the freshly built index *before* the swap. The ordinary
delta indexer then runs on top: a session whose transcript is unchanged is
skipped by hash, a changed one is reconciled by chunk id, and one whose
transcript is gone is simply left alone. Chunk and file ids are deterministic,
so nothing is duplicated.

Vectors are taken from the previous index when it was written by the same
embedding model; otherwise the turns are re-embedded from ``embedding_input``.

Write order mirrors ``ConversationIndexer``: rows land under an empty file
hash, BM25 is committed and the vector index saved, and only then is the hash
stamped. A crash or failure in between leaves an "unfinished" session, which
the consistency check tolerates and the next run repairs.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from hybrid_search.index.scanner import CONVERSATION_PATH_PREFIX
from hybrid_search.providers import EMBEDDING_FINGERPRINT_KEY, vector_space_matches
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import ChunkRecord, ConversationMeta, FileRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths

logger = logging.getLogger(__name__)

# Same trade as the conversation indexer: every checkpoint commits BM25 and
# rewrites the vector index, so this bounds rework against I/O.
_CHECKPOINT_CHUNKS = 500
# Stay well under SQLite's bound-parameter limit for the meta lookup.
_META_BATCH = 500


@dataclass
class CarryoverResult:
    sessions: int = 0
    chunks: int = 0
    reembedded: int = 0


@dataclass
class _Pending:
    file: FileRecord
    file_hash: str
    chunk_ids: list[str]


def carry_over_conversations(
    previous_dir: Path, rebuilt_dir: Path, embedder, project_id: str
) -> CarryoverResult:
    """Copy conversation sessions from ``previous_dir`` into ``rebuilt_dir``.

    Never raises — losing the carry-over is bad, failing the whole rebuild on
    top of it is worse. Sessions that were not fully carried are left
    unstamped (or absent) and reported as not carried.
    """
    result = CarryoverResult()
    old_paths = IndexPaths(previous_dir)
    if not old_paths.store_db.is_file():
        return result

    old_db = new_db = None
    try:
        old_db = StoreDB(old_paths.store_db)
        sessions = [
            f for f in old_db.get_all_files(project_id)
            if f.relative_path.startswith(CONVERSATION_PATH_PREFIX)
        ]
        if not sessions:
            return result

        new_paths = IndexPaths(rebuilt_dir)
        new_db = StoreDB(new_paths.store_db)
        bm25 = BM25Engine(new_paths.tantivy_dir)
        vector = VectorEngine(new_paths.vectors_dir, embedder.embedding_dim)
        old_vectors = _open_previous_vectors(old_db, old_paths, embedder)
        _copy_sessions(
            old_db, old_vectors, new_db, bm25, vector, embedder, project_id,
            sessions, result,
        )
    except Exception as exc:
        logger.warning(
            "conversation carry-over failed after %d session(s) — the rest "
            "must be re-derived from transcripts: %s", result.sessions, exc,
        )
    finally:
        for db in (old_db, new_db):
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
    return result


def _open_previous_vectors(old_db: StoreDB, old_paths: IndexPaths, embedder):
    """The previous vector index, or None when its vectors cannot be reused.

    A different embedding model means the old vectors live in another space;
    copying them would poison the rebuilt index, so they are re-embedded.
    """
    fingerprint = getattr(embedder, "fingerprint", None)
    if not isinstance(fingerprint, str) or not vector_space_matches(
        old_db.get_meta(EMBEDDING_FINGERPRINT_KEY), fingerprint
    ):
        return None
    try:
        vectors = VectorEngine(old_paths.vectors_dir, embedder.embedding_dim)
    except Exception:
        logger.debug("previous vectors unavailable", exc_info=True)
        return None
    return None if vectors.migration_failed else vectors


def _copy_sessions(
    old_db, old_vectors, new_db, bm25, vector, embedder, project_id,
    sessions: list[FileRecord], result: CarryoverResult,
) -> None:
    pending: list[_Pending] = []
    pending_chunks = 0
    try:
        for session in sessions:
            if new_db.get_file_by_path(project_id, session.relative_path) is not None:
                continue  # already in the rebuilt index — never overwrite
            chunks = old_db.get_chunks_by_file(session.id)
            if not chunks:
                continue
            metas = _load_meta(old_db, [c.id for c in chunks])
            vectors, embedded = _vectors_for(chunks, old_vectors, embedder)
            # Registered before the write so a failure halfway through this
            # session is withdrawn from the engines along with the rest.
            pending.append(_Pending(
                file=session,
                file_hash=session.file_hash or _fallback_hash(chunks, metas),
                chunk_ids=[c.id for c in chunks],
            ))
            _write_session(new_db, bm25, vector, session, chunks, metas, vectors)
            result.reembedded += embedded
            pending_chunks += len(chunks)
            if pending_chunks >= _CHECKPOINT_CHUNKS:
                _checkpoint(new_db, bm25, vector, pending, result)
                pending, pending_chunks = [], 0
        _checkpoint(new_db, bm25, vector, pending, result)
    except Exception:
        _withdraw(bm25, vector, pending)
        raise


def _load_meta(old_db: StoreDB, chunk_ids: list[str]) -> dict[str, ConversationMeta]:
    metas: dict[str, ConversationMeta] = {}
    for start in range(0, len(chunk_ids), _META_BATCH):
        metas.update(
            old_db.get_conversation_meta_batch(chunk_ids[start:start + _META_BATCH])
        )
    return metas


def _vectors_for(
    chunks: list[ChunkRecord], old_vectors, embedder
) -> tuple[list[np.ndarray], int]:
    """One vector per chunk: reused where possible, re-embedded otherwise."""
    found = [
        old_vectors.get_vector(c.id) if old_vectors is not None else None
        for c in chunks
    ]
    missing = [i for i, vec in enumerate(found) if vec is None]
    if missing:
        embedded = embedder.embed_texts(
            [chunks[i].embedding_input or chunks[i].content or "" for i in missing]
        )
        fresh = dict(zip(missing, embedded))
        found = [vec if vec is not None else fresh[i] for i, vec in enumerate(found)]
    return [np.asarray(vec, dtype=np.float32) for vec in found], len(missing)


def _write_session(new_db, bm25, vector, session, chunks, metas, vectors) -> None:
    with new_db.transaction() as conn:
        # Placeholder row, exactly as the indexer writes it: the real hash
        # is stamped only after the search engines are durable.
        new_db.upsert_file(conn, replace(session, file_hash="", chunk_count=0))
        new_db.insert_chunks(conn, chunks)
        new_db.upsert_conversation_meta(
            conn, [metas[c.id] for c in chunks if c.id in metas]
        )
    for chunk, vec in zip(chunks, vectors):
        # BM25 and embeddings index the raw turn; ``content`` may carry the
        # untrusted-content banner, which is display-only.
        bm25.add(
            chunk_id=chunk.id, name=chunk.name or "",
            qualified_name=chunk.qualified_name or "",
            content=chunk.embedding_input or chunk.content or "", docstring=None,
        )
        vector.add(chunk.id, vec)


def _checkpoint(new_db, bm25, vector, pending: list[_Pending], result) -> None:
    """Make the search engines durable, then mark the sessions done."""
    if not pending:
        return
    bm25.commit()
    vector.save()
    with new_db.transaction() as conn:
        for p in pending:
            new_db.upsert_file(conn, replace(
                p.file, file_hash=p.file_hash, chunk_count=len(p.chunk_ids),
            ))
    result.sessions += len(pending)
    result.chunks += sum(len(p.chunk_ids) for p in pending)


def _withdraw(bm25, vector, pending: list[_Pending]) -> None:
    """Take unstamped sessions back out of the search engines.

    Their rows stay in SQLite under an empty hash — the "unfinished" state
    the consistency check excludes — so the engines must not hold them
    either, or the next index run reads the surplus as damage and answers
    with another full rebuild.
    """
    ids = [cid for p in pending for cid in p.chunk_ids]
    try:
        bm25.delete_batch(ids)
        bm25.commit()
        vector.remove_batch(ids)
        vector.save()
    except Exception:
        logger.warning("conversation carry-over rollback failed", exc_info=True)


def _fallback_hash(
    chunks: list[ChunkRecord], metas: dict[str, ConversationMeta]
) -> str:
    """A stamp for a session the previous index never finished.

    It only has to be non-empty: if the transcript still exists the indexer
    sees a differing hash and reconciles by chunk id, re-embedding nothing
    that is already here.
    """
    def turn(c: ChunkRecord) -> tuple[int, str]:
        meta = metas.get(c.id)
        return (meta.turn_index if meta else 0, c.id)

    joined = "\n".join(c.embedding_input or "" for c in sorted(chunks, key=turn))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
