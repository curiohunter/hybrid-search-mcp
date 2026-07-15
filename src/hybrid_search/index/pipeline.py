"""Indexing pipeline — orchestrates scanner → chunker → embedder → index update.

Implements the multi-store update order from §13 of the design doc.
Uses 2-pass architecture for efficient cross-file embedding batching.
"""

from __future__ import annotations

import gc
import hashlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from hybrid_search.config import Config
from hybrid_search.index.ast_chunker import CodeChunk, chunk_code_file
from hybrid_search.index.callgraph import resolve_call_edges
from hybrid_search.index.doc_chunker import chunk_doc_file
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.module_synth import synthesize_modules
from hybrid_search.index.modules import discover_modules
from hybrid_search.index.scanner import (
    ScanResult,
    compute_file_hash,
    detect_language,
    scan_project,
    scan_project_subset,
)
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine, VectorMigrationError
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger(__name__)

DOC_LANGUAGES = {"markdown", "json", "yaml", "toml"}

# Flush embedding buffer and write to stores after accumulating this many chunks.
# Keeps memory bounded while still batching efficiently across files.
EMBED_FLUSH_THRESHOLD = 128


@dataclass
class IndexingResult:
    project_id: str
    project_name: str
    files_added: int = 0
    files_changed: int = 0
    files_deleted: int = 0
    chunks_total: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


@dataclass
class _FileChunkResult:
    """Intermediate result from Pass 1 (chunking only, no embedding yet)."""

    file_id: str
    rel_path: str
    file_hash: str
    file_size: int
    file_mtime: str
    language: str
    chunks: list[CodeChunk]
    old_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class _ConsistencyMismatchError(RuntimeError):
    sqlite_count: int
    bm25_count: int
    vector_count: int


# Type for progress callbacks: (current_file_index, total_files, file_path)
ProgressCallback = Callable[[int, int, str], None]


class IndexingPipeline:
    """Orchestrates the full indexing flow for a project."""

    def __init__(self, config: Config, registry: ProjectRegistry, embedder: Embedder) -> None:
        self._config = config
        self._registry = registry
        self._embedder = embedder

    def index_project(
        self,
        project_path: str,
        project_name: str | None = None,
        force: bool = False,
        changed_paths: list[str] | None = None,
        deleted_paths: list[str] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> IndexingResult:
        """Index or re-index a project."""
        start = time.monotonic()
        abs_path = Path(project_path).resolve()

        if not abs_path.is_dir():
            raise ValueError(f"Project path does not exist: {abs_path}")

        # Register project
        pid = project_hash(str(abs_path))
        name = project_name or abs_path.name
        self._registry.register(name, str(abs_path))

        # Setup paths
        project_dir = get_project_dir(self._config.projects_dir, pid)
        self._recover_atomic_rebuild(project_dir)

        try:
            if force:
                result = self._rebuild_project_atomically(
                    abs_path=abs_path,
                    project_name=name,
                    project_id=pid,
                    project_dir=project_dir,
                    on_progress=on_progress,
                )
            else:
                result = self._index_project_once(
                    abs_path=abs_path,
                    project_name=name,
                    project_id=pid,
                    project_dir=project_dir,
                    changed_paths=changed_paths,
                    deleted_paths=deleted_paths,
                    on_progress=on_progress,
                    update_registry_stats=True,
                )
        except VectorMigrationError as e:
            logger.warning(
                "Vector dtype migration failed (%s). Rebuilding the project "
                "atomically in a fresh directory.", e,
            )
            result = self._rebuild_project_atomically(
                abs_path=abs_path,
                project_name=name,
                project_id=pid,
                project_dir=project_dir,
                on_progress=on_progress,
            )
            result.errors.insert(
                0, "Auto-rebuild triggered: vector dtype migration failed"
            )
        except _ConsistencyMismatchError as mismatch:
            logger.warning(
                "Consistency mismatch: SQLite=%d, Tantivy=%d, USearch=%d. "
                "Triggering atomic rebuild.",
                mismatch.sqlite_count,
                mismatch.bm25_count,
                mismatch.vector_count,
            )
            result = self._rebuild_project_atomically(
                abs_path=abs_path,
                project_name=name,
                project_id=pid,
                project_dir=project_dir,
                on_progress=on_progress,
            )
            result.errors.insert(
                0,
                "Auto-rebuild triggered: "
                f"SQLite={mismatch.sqlite_count}, "
                f"Tantivy={mismatch.bm25_count}, "
                f"USearch={mismatch.vector_count}",
            )

        result.elapsed_seconds = time.monotonic() - start
        logger.info(
            "Indexing complete for %s: +%d ~%d -%d files, %d chunks in %.1fs",
            name, result.files_added, result.files_changed,
            result.files_deleted, result.chunks_total, result.elapsed_seconds,
        )
        return result

    def _index_project_once(
        self,
        abs_path: Path,
        project_name: str,
        project_id: str,
        project_dir: Path,
        changed_paths: list[str] | None,
        deleted_paths: list[str] | None,
        on_progress: ProgressCallback | None,
        update_registry_stats: bool,
    ) -> IndexingResult:
        idx_paths = IndexPaths(project_dir)
        idx_paths.ensure_dirs()

        db = StoreDB(idx_paths.store_db)
        vector_engine = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)
        bm25_engine = BM25Engine(idx_paths.tantivy_dir)
        result = IndexingResult(project_id=project_id, project_name=project_name)

        try:
            if vector_engine.migration_failed:
                # The engine is holding an empty index over a full on-disk
                # one; an incremental pass would persist partial state.
                # Bail out BEFORE touching SQLite/BM25 — the caller routes
                # this to a full atomic rebuild in a fresh directory.
                raise VectorMigrationError(
                    "vector index dtype migration failed — incremental "
                    "indexing refused, full rebuild required"
                )
            if changed_paths is not None:
                scan = scan_project_subset(
                    abs_path,
                    project_id,
                    db,
                    self._config.indexing,
                    changed_paths=changed_paths,
                    deleted_paths=deleted_paths,
                )
            else:
                scan = scan_project(abs_path, project_id, db, self._config.indexing)
            result.files_added = len(scan.added)
            result.files_changed = len(scan.changed)
            result.files_deleted = len(scan.deleted)

            self._process_deletions(db, vector_engine, bm25_engine, project_id, scan.deleted)

            all_files = scan.added + scan.changed
            total_files = len(all_files)
            pending: list[_FileChunkResult] = []
            pending_chunk_count = 0

            for i, file_path in enumerate(all_files):
                if on_progress is not None:
                    try:
                        on_progress(i + 1, total_files, str(file_path.relative_to(abs_path)))
                    except Exception:
                        pass

                try:
                    fcr = self._chunk_file(db, file_path, abs_path, project_id)
                except Exception as e:
                    logger.error("Error chunking %s: %s", file_path, e)
                    result.errors.append(f"{file_path}: {e}")
                    continue

                if fcr is None:
                    continue

                pending.append(fcr)
                pending_chunk_count += len(fcr.chunks)
                if pending_chunk_count >= EMBED_FLUSH_THRESHOLD:
                    self._flush_pending(
                        pending, db, vector_engine, bm25_engine, project_id, result,
                    )
                    pending.clear()
                    pending_chunk_count = 0

            if pending:
                self._flush_pending(
                    pending, db, vector_engine, bm25_engine, project_id, result,
                )

            bm25_engine.commit()
            vector_engine.save()

            if all_files:
                try:
                    edge_stats = resolve_call_edges(db, project_id)
                    logger.info("Call graph resolution: %s", edge_stats)
                except Exception as e:
                    logger.warning("Call graph resolution failed (non-fatal): %s", e)
                    result.errors.append(f"call_graph_resolution: {e}")

                try:
                    mod_stats = discover_modules(db, project_id, abs_path)
                    logger.info("Module discovery: %s", mod_stats)
                except Exception as e:
                    logger.warning("Module discovery failed (non-fatal): %s", e)
                    result.errors.append(f"module_discovery: {e}")

                try:
                    synth_stats = synthesize_modules(
                        db, project_id, embedder=self._embedder
                    )
                    logger.info("Module synthesis: %s", synth_stats)
                except Exception as e:
                    logger.warning("Module synthesis failed (non-fatal): %s", e)
                    result.errors.append(f"module_synthesis: {e}")

            file_count = db.get_file_count(project_id)
            chunk_count = db.get_chunk_count(project_id)
            vector_count = vector_engine.count
            bm25_count = bm25_engine.count
            if chunk_count != vector_count or chunk_count != bm25_count:
                raise _ConsistencyMismatchError(
                    sqlite_count=chunk_count,
                    bm25_count=bm25_count,
                    vector_count=vector_count,
                )

            result.chunks_total = chunk_count
            if update_registry_stats:
                self._registry.update_stats(project_id, file_count, chunk_count)
            return result
        finally:
            db.close()

    def _rebuild_project_atomically(
        self,
        abs_path: Path,
        project_name: str,
        project_id: str,
        project_dir: Path,
        on_progress: ProgressCallback | None,
    ) -> IndexingResult:
        rebuilding_dir = project_dir.parent / f"{project_dir.name}.rebuilding"
        backup_dir = project_dir.parent / f"{project_dir.name}.backup"

        self._recover_atomic_rebuild(project_dir)
        shutil.rmtree(rebuilding_dir, ignore_errors=True)
        shutil.rmtree(backup_dir, ignore_errors=True)

        try:
            result = self._index_project_once(
                abs_path=abs_path,
                project_name=project_name,
                project_id=project_id,
                project_dir=rebuilding_dir,
                changed_paths=None,
                deleted_paths=None,
                on_progress=on_progress,
                update_registry_stats=False,
            )
            gc.collect()
            self._swap_project_dirs(project_dir, rebuilding_dir, backup_dir)
            file_count = self._read_project_file_count(project_dir, project_id)
            self._registry.update_stats(project_id, file_count, result.chunks_total)
            return result
        except Exception:
            shutil.rmtree(rebuilding_dir, ignore_errors=True)
            raise

    def _recover_atomic_rebuild(self, project_dir: Path) -> None:
        rebuilding_dir = project_dir.parent / f"{project_dir.name}.rebuilding"
        backup_dir = project_dir.parent / f"{project_dir.name}.backup"

        if rebuilding_dir.exists():
            shutil.rmtree(rebuilding_dir, ignore_errors=True)

        if backup_dir.exists() and not project_dir.exists():
            backup_dir.rename(project_dir)
        elif backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

    def _swap_project_dirs(self, project_dir: Path, rebuilding_dir: Path, backup_dir: Path) -> None:
        if not rebuilding_dir.exists():
            raise RuntimeError(f"Atomic rebuild directory missing: {rebuilding_dir}")

        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

        moved_current = False
        if project_dir.exists():
            project_dir.rename(backup_dir)
            moved_current = True

        try:
            rebuilding_dir.rename(project_dir)
        except Exception:
            if moved_current and backup_dir.exists() and not project_dir.exists():
                backup_dir.rename(project_dir)
            raise

        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

    def _read_project_file_count(self, project_dir: Path, project_id: str) -> int:
        idx_paths = IndexPaths(project_dir)
        db = StoreDB(idx_paths.store_db)
        try:
            return db.get_file_count(project_id)
        finally:
            db.close()

    # ── Pass 1: chunk only ──

    def _chunk_file(
        self,
        db: StoreDB,
        file_path: Path,
        project_root: Path,
        project_id: str,
    ) -> _FileChunkResult | None:
        """Chunk a single file without embedding. Returns None if skipped."""
        rel_path = str(file_path.relative_to(project_root))
        language = detect_language(file_path)
        if language is None:
            return None

        source = file_path.read_text(errors="replace")
        file_hash = compute_file_hash(file_path)
        stat = file_path.stat()
        file_id = hashlib.sha256(f"{project_id}:{rel_path}".encode()).hexdigest()[:16]

        if language in DOC_LANGUAGES:
            chunks = chunk_doc_file(file_path, project_root, project_id, language, source)
        else:
            chunks = chunk_code_file(file_path, project_root, project_id, language, source)

        if not chunks:
            return None

        old_chunk_ids = db.get_chunk_ids_by_file(file_id)

        return _FileChunkResult(
            file_id=file_id,
            rel_path=rel_path,
            file_hash=file_hash,
            file_size=stat.st_size,
            file_mtime=str(stat.st_mtime),
            language=language,
            chunks=chunks,
            old_chunk_ids=old_chunk_ids,
        )

    # ── Pass 2: batch embed + store ──

    def _flush_pending(
        self,
        pending: list[_FileChunkResult],
        db: StoreDB,
        vector_engine: VectorEngine,
        bm25_engine: BM25Engine,
        project_id: str,
        result: IndexingResult,
    ) -> None:
        """Batch-embed all pending chunks and write to stores."""
        # Collect all embedding texts across files
        all_texts: list[str] = []
        for fcr in pending:
            for chunk in fcr.chunks:
                all_texts.append(chunk.embedding_input)

        # Single batched embedding call across all pending files
        all_embeddings = self._embedder.embed_texts(all_texts)

        # Distribute embeddings back to files and write to stores
        embed_offset = 0
        for fcr in pending:
            n_chunks = len(fcr.chunks)
            file_embeddings = all_embeddings[embed_offset : embed_offset + n_chunks]
            embed_offset += n_chunks

            try:
                self._store_file(db, vector_engine, bm25_engine, fcr, file_embeddings, project_id)
            except Exception as e:
                logger.error("Error storing %s: %s", fcr.rel_path, e)
                result.errors.append(f"{fcr.rel_path}: {e}")

        # Checkpoint after each flush
        bm25_engine.commit()
        vector_engine.save()

    def _store_file(
        self,
        db: StoreDB,
        vector_engine: VectorEngine,
        bm25_engine: BM25Engine,
        fcr: _FileChunkResult,
        embeddings: np.ndarray,
        project_id: str,
    ) -> None:
        """Write chunked + embedded file data to all stores."""
        # Multi-store update — SQLite writes in a transaction for atomicity
        with db.transaction() as conn:
            # Step 0: Ensure file record exists (FK for chunks)
            file_record_init = FileRecord(
                id=fcr.file_id,
                project_id=project_id,
                relative_path=fcr.rel_path,
                file_hash="",  # placeholder — updated at Step 5
                file_size=fcr.file_size,
                file_mtime=fcr.file_mtime,
                language=fcr.language,
                chunk_count=0,
            )
            db.upsert_file(conn, file_record_init)

            # Step 1: Delete old call_edges, then old chunks
            for old_cid in fcr.old_chunk_ids:
                db.delete_call_edges_by_caller(conn, old_cid)
            db.delete_chunks_by_file(conn, fcr.file_id)

            # Step 2: Insert new chunks + call edges
            chunk_records = [
                ChunkRecord(
                    id=c.id,
                    file_id=fcr.file_id,
                    project_id=project_id,
                    name=c.name,
                    qualified_name=c.qualified_name,
                    node_type=c.node_type,
                    start_line=c.start_line,
                    end_line=c.end_line,
                    start_byte=c.start_byte,
                    end_byte=c.end_byte,
                    content=c.content,
                    embedding_input=c.embedding_input,
                    docstring=c.docstring,
                    parent_name=c.parent_name,
                )
                for c in fcr.chunks
            ]
            db.insert_chunks(conn, chunk_records)

            for c in fcr.chunks:
                if c.calls:
                    db.insert_call_edges(conn, c.id, c.calls, project_id)

            # Step 5: Update file record last (crash recovery marker)
            file_record = FileRecord(
                id=fcr.file_id,
                project_id=project_id,
                relative_path=fcr.rel_path,
                file_hash=fcr.file_hash,
                file_size=fcr.file_size,
                file_mtime=fcr.file_mtime,
                language=fcr.language,
                chunk_count=len(fcr.chunks),
            )
            db.upsert_file(conn, file_record)

        # Step 3: Update BM25 index (outside transaction — non-SQLite)
        if fcr.old_chunk_ids:
            bm25_engine.delete_batch(fcr.old_chunk_ids)

        for c in fcr.chunks:
            bm25_engine.add(
                chunk_id=c.id,
                name=c.name,
                qualified_name=c.qualified_name,
                content=c.content,
                docstring=c.docstring,
            )

        # Step 4: Update vector index
        if fcr.old_chunk_ids:
            vector_engine.remove_batch(fcr.old_chunk_ids)

        chunk_ids = [c.id for c in fcr.chunks]
        vector_engine.add_batch(chunk_ids, embeddings)

    def _process_deletions(
        self,
        db: StoreDB,
        vector_engine: VectorEngine,
        bm25_engine: BM25Engine,
        project_id: str,
        deleted_paths: list[str],
    ) -> None:
        """Remove deleted files from all stores."""
        for rel_path in deleted_paths:
            file_rec = db.get_file_by_path(project_id, rel_path)
            if file_rec is None:
                continue

            with db.transaction() as conn:
                old_chunk_ids = db.delete_chunks_by_file(conn, file_rec.id)
                if old_chunk_ids:
                    # Clean up dangling callee references (no FK on callee_chunk_id)
                    for cid in old_chunk_ids:
                        db.delete_call_edges_by_callee(conn, cid)
                    vector_engine.remove_batch(old_chunk_ids)
                    bm25_engine.delete_batch(old_chunk_ids)
                db.delete_file(conn, file_rec.id)

    def _clear_project(
        self,
        db: StoreDB,
        vector_engine: VectorEngine,
        bm25_engine: BM25Engine,
        project_id: str,
    ) -> None:
        """Clear all data for a project (for force re-index)."""
        all_files = db.get_all_files(project_id)
        # Delete all call_edges for the project first (before chunks, to avoid FK issues)
        with db.transaction() as conn:
            db.delete_all_call_edges(conn, project_id)

        for file_rec in all_files:
            with db.transaction() as conn:
                old_chunk_ids = db.delete_chunks_by_file(conn, file_rec.id)
                if old_chunk_ids:
                    vector_engine.remove_batch(old_chunk_ids)
                    bm25_engine.delete_batch(old_chunk_ids)
                db.delete_file(conn, file_rec.id)
