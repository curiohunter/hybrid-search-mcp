"""Indexing pipeline — orchestrates scanner → chunker → embedder → index update.

Implements the multi-store update order from §13 of the design doc.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from hybrid_search.config import Config
from hybrid_search.index.ast_chunker import CodeChunk, chunk_code_file
from hybrid_search.index.callgraph import resolve_call_edges
from hybrid_search.index.doc_chunker import chunk_doc_file
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.scanner import ScanResult, compute_file_hash, detect_language, scan_project
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger(__name__)

DOC_LANGUAGES = {"markdown", "json", "yaml", "toml"}


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
        idx_paths = IndexPaths(project_dir)
        idx_paths.ensure_dirs()

        # Open stores
        db = StoreDB(idx_paths.store_db)
        vector_engine = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)
        bm25_engine = BM25Engine(idx_paths.tantivy_dir)

        result = IndexingResult(project_id=pid, project_name=name)

        try:
            if force:
                # Force re-index: clear everything
                self._clear_project(db, vector_engine, bm25_engine, pid)

            # Scan for changes
            scan = scan_project(abs_path, pid, db, self._config.indexing)
            result.files_added = len(scan.added)
            result.files_changed = len(scan.changed)
            result.files_deleted = len(scan.deleted)

            # Process deletions
            self._process_deletions(db, vector_engine, bm25_engine, pid, scan.deleted)

            # Process added and changed files with periodic checkpoint
            all_files = scan.added + scan.changed
            total_files = len(all_files)
            batch_interval = 10  # Checkpoint every N files to limit crash-loss window
            for i, file_path in enumerate(all_files):
                if on_progress is not None:
                    try:
                        on_progress(i + 1, total_files, str(file_path.relative_to(abs_path)))
                    except Exception:
                        pass  # Progress callback failure is non-fatal
                try:
                    self._process_file(db, vector_engine, bm25_engine, file_path, abs_path, pid)
                except Exception as e:
                    logger.error("Error processing %s: %s", file_path, e)
                    result.errors.append(f"{file_path}: {e}")

                # Periodic checkpoint: commit Tantivy + save USearch every N files
                if (i + 1) % batch_interval == 0:
                    bm25_engine.commit()
                    vector_engine.save()

            # Final commit
            bm25_engine.commit()
            vector_engine.save()

            # Resolve call edges (§12: link callee_name → callee_chunk_id)
            if all_files:
                try:
                    edge_stats = resolve_call_edges(db, pid)
                    logger.info("Call graph resolution: %s", edge_stats)
                except Exception as e:
                    logger.warning("Call graph resolution failed (non-fatal): %s", e)
                    result.errors.append(f"call_graph_resolution: {e}")

            # Update stats
            file_count = db.get_file_count(pid)
            chunk_count = db.get_chunk_count(pid)
            result.chunks_total = chunk_count
            self._registry.update_stats(pid, file_count, chunk_count)

            # Consistency check (SQLite vs Tantivy vs USearch)
            vector_count = vector_engine.count
            bm25_count = bm25_engine.count
            if chunk_count != vector_count or chunk_count != bm25_count:
                logger.warning(
                    "Consistency mismatch: SQLite=%d, Tantivy=%d, USearch=%d. "
                    "Triggering automatic rebuild.",
                    chunk_count, bm25_count, vector_count,
                )
                if not force:
                    # Auto-rebuild: close current stores and re-index with force
                    db.close()
                    result.errors.append(
                        f"Auto-rebuild triggered: SQLite={chunk_count}, "
                        f"Tantivy={bm25_count}, USearch={vector_count}"
                    )
                    return self.index_project(project_path=str(abs_path), project_name=name, force=True)

        finally:
            db.close()

        result.elapsed_seconds = time.monotonic() - start
        logger.info(
            "Indexing complete for %s: +%d ~%d -%d files, %d chunks in %.1fs",
            name, result.files_added, result.files_changed,
            result.files_deleted, result.chunks_total, result.elapsed_seconds,
        )
        return result

    def _process_file(
        self,
        db: StoreDB,
        vector_engine: VectorEngine,
        bm25_engine: BM25Engine,
        file_path: Path,
        project_root: Path,
        project_id: str,
    ) -> None:
        """Process a single file: chunk → embed → store."""
        rel_path = str(file_path.relative_to(project_root))
        language = detect_language(file_path)
        if language is None:
            return

        source = file_path.read_text(errors="replace")
        file_hash = compute_file_hash(file_path)
        stat = file_path.stat()

        # Generate file ID
        import hashlib
        file_id = hashlib.sha256(f"{project_id}:{rel_path}".encode()).hexdigest()[:16]

        # Chunk the file
        if language in DOC_LANGUAGES:
            chunks = chunk_doc_file(file_path, project_root, project_id, language, source)
        else:
            chunks = chunk_code_file(file_path, project_root, project_id, language, source)

        if not chunks:
            return

        # Generate embeddings
        embedding_texts = [c.embedding_input for c in chunks]
        embeddings = self._embedder.embed_texts(embedding_texts)

        # Multi-store update — SQLite writes in a transaction for atomicity
        with db.transaction() as conn:
            # Step 0: Ensure file record exists (FK for chunks)
            file_record_init = FileRecord(
                id=file_id,
                project_id=project_id,
                relative_path=rel_path,
                file_hash="",  # placeholder — updated at Step 5
                file_size=stat.st_size,
                file_mtime=str(stat.st_mtime),
                language=language,
                chunk_count=0,
            )
            db.upsert_file(conn, file_record_init)

            # Step 1: Delete old call_edges, then old chunks
            old_chunk_ids = db.get_chunk_ids_by_file(file_id)
            for old_cid in old_chunk_ids:
                db.delete_call_edges_by_caller(conn, old_cid)
            db.delete_chunks_by_file(conn, file_id)

            # Step 2: Insert new chunks + call edges
            chunk_records = [
                ChunkRecord(
                    id=c.id,
                    file_id=file_id,
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
                for c in chunks
            ]
            db.insert_chunks(conn, chunk_records)

            for c in chunks:
                if c.calls:
                    db.insert_call_edges(conn, c.id, c.calls, project_id)

            # Step 5: Update file record last (crash recovery marker)
            file_record = FileRecord(
                id=file_id,
                project_id=project_id,
                relative_path=rel_path,
                file_hash=file_hash,
                file_size=stat.st_size,
                file_mtime=str(stat.st_mtime),
                language=language,
                chunk_count=len(chunks),
            )
            db.upsert_file(conn, file_record)

        # Step 3: Update BM25 index (outside transaction — non-SQLite)
        if old_chunk_ids:
            bm25_engine.delete_batch(old_chunk_ids)

        for c in chunks:
            bm25_engine.add(
                chunk_id=c.id,
                name=c.name,
                qualified_name=c.qualified_name,
                content=c.content,
                docstring=c.docstring,
            )

        # Step 4: Update vector index
        if old_chunk_ids:
            vector_engine.remove_batch(old_chunk_ids)

        chunk_ids = [c.id for c in chunks]
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
