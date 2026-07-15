"""USearch-based HNSW vector search engine."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from usearch.index import Index, MetricKind, ScalarKind

logger = logging.getLogger(__name__)

# HNSW parameters per design doc §13
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200

# Store vectors as full float32. USearch's default scalar kind is BF16, which
# routes cosine distance through ``simsimd_cos_bf16_neon`` on Apple Silicon.
# That bf16 NEON kernel has crashed with SIGBUS (out-of-bounds read during
# HNSW traversal). f32 avoids that code path entirely and improves precision.
INDEX_DTYPE = ScalarKind.F32


class _MigrationLock:
    """Cross-process lock for the one-time dtype migration.

    Atomic ``mkdir`` acquires; an ``owner`` file records pid + wall time
    so a lock whose owner died (or that outlived any plausible
    migration) can be reclaimed. Losers wait for release rather than
    migrating in parallel — see _migrate_dtype_if_needed for why
    parallel migration is unsafe against incremental writers.
    """

    POLL_SECONDS = 0.05
    WAIT_TIMEOUT_SECONDS = 300.0
    STALE_AFTER_SECONDS = 600.0

    def __init__(self, index_path: Path) -> None:
        self._dir = index_path.with_name(index_path.name + ".dtype-migration.lock")
        self._owner_file = self._dir / "owner"

    def acquire_or_wait(self) -> bool:
        """Try to become the migrator. Returns True when acquired; False
        when another process held the lock and has since released it (the
        caller must re-check whether migration is still needed). Raises
        TimeoutError if the lock never frees up."""
        deadline = time.monotonic() + self.WAIT_TIMEOUT_SECONDS
        while True:
            try:
                self._dir.mkdir()
            except FileExistsError:
                self._reclaim_if_stale()
                if not self._dir.exists():
                    continue  # reclaimed — race for it again
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for dtype migration lock {self._dir}"
                    )
                time.sleep(self.POLL_SECONDS)
                if not self._dir.exists():
                    return False  # holder finished; caller re-checks header
                continue
            try:
                self._owner_file.write_text(f"{os.getpid()}:{time.time()}")
            except Exception:
                pass  # owner info is best-effort; the mkdir is the lock
            return True

    def release(self) -> None:
        self._owner_file.unlink(missing_ok=True)
        try:
            self._dir.rmdir()
        except OSError:
            pass

    def _reclaim_if_stale(self) -> None:
        try:
            pid_str, ts_str = self._owner_file.read_text().split(":", 1)
            pid, ts = int(pid_str), float(ts_str)
        except Exception:
            # No/garbled owner file: only reclaim on directory age.
            try:
                age = time.time() - self._dir.stat().st_mtime
            except OSError:
                return
            if age > self.STALE_AFTER_SECONDS:
                self._force_remove()
            return

        dead = False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead = True
        except OSError:
            pass  # e.g. permission — assume alive
        if dead or (time.time() - ts) > self.STALE_AFTER_SECONDS:
            self._force_remove()

    def _force_remove(self) -> None:
        logger.warning("Reclaiming stale dtype migration lock %s", self._dir)
        self._owner_file.unlink(missing_ok=True)
        try:
            self._dir.rmdir()
        except OSError:
            pass


class VectorMigrationError(RuntimeError):
    """BF16→F32 index migration failed. The engine refuses every write in
    this state: the process is holding an EMPTY in-memory index while the
    full (still-BF16) index sits on disk, so any add-then-save would
    replace the complete index with a partial one. Callers should route
    to a full atomic rebuild instead."""


@dataclass
class VectorResult:
    chunk_id: str
    score: float  # cosine similarity


class VectorEngine:
    """USearch HNSW vector index for semantic search."""

    def __init__(self, index_dir: Path, embedding_dim: int) -> None:
        self._index_dir = index_dir
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = index_dir / "vectors.usearch"
        self._dim = embedding_dim

        # Mapping between internal integer keys and chunk_id strings
        self._key_to_id: dict[int, str] = {}
        self._id_to_key: dict[str, int] = {}
        self._next_key: int = 0

        # USearch is not safe for concurrent mutation + search within a
        # process; a torn HNSW traversal reads a wild node pointer and
        # crashes with SIGBUS. Serialize all index access through this lock.
        self._lock = threading.Lock()

        # Set when the dtype migration fails: reads serve empty results,
        # writes raise so the on-disk index can never be overwritten with
        # this session's partial state.
        self._migration_failed = False

        self._index = Index(
            ndim=embedding_dim,
            metric=MetricKind.Cos,
            dtype=INDEX_DTYPE,
            connectivity=HNSW_M,
            expansion_add=HNSW_EF_CONSTRUCTION,
        )

        self._load()

    @property
    def migration_failed(self) -> bool:
        """True when the dtype migration failed and writes are blocked."""
        return self._migration_failed

    def _ensure_writable(self) -> None:
        if self._migration_failed:
            raise VectorMigrationError(
                "Vector index dtype migration failed; refusing writes so the "
                "existing on-disk index is not overwritten with partial state. "
                "Run a full reindex (atomic rebuild) to recover."
            )

    def add(self, chunk_id: str, vector: np.ndarray) -> None:
        """Add a single vector to the index."""
        self._ensure_writable()
        with self._lock:
            if chunk_id in self._id_to_key:
                # Remove existing before re-adding
                old_key = self._id_to_key[chunk_id]
                self._index.remove(old_key)
                del self._key_to_id[old_key]

            key = self._next_key
            self._next_key += 1
            self._index.add(key, vector.astype(np.float32))
            self._key_to_id[key] = chunk_id
            self._id_to_key[chunk_id] = key

    def add_batch(self, chunk_ids: list[str], vectors: np.ndarray) -> None:
        """Add multiple vectors in batch."""
        self._ensure_writable()
        for i, cid in enumerate(chunk_ids):
            self.add(cid, vectors[i])

    def remove(self, chunk_id: str) -> None:
        """Remove a vector by chunk_id."""
        self._ensure_writable()
        with self._lock:
            if chunk_id not in self._id_to_key:
                return
            key = self._id_to_key[chunk_id]
            self._index.remove(key)
            del self._key_to_id[key]
            del self._id_to_key[chunk_id]

    def remove_batch(self, chunk_ids: list[str]) -> None:
        """Remove multiple vectors."""
        self._ensure_writable()
        for cid in chunk_ids:
            self.remove(cid)

    def search(
        self,
        query_vector: np.ndarray,
        limit: int = 10,
        chunk_ids_filter: set[str] | None = None,
    ) -> list[VectorResult]:
        """Search for nearest neighbors. Returns sorted by cosine similarity descending."""
        with self._lock:
            if len(self._key_to_id) == 0:
                return []

            # Search more than needed to account for filtering
            search_limit = min(limit * 3, len(self._key_to_id))
            matches = self._index.search(query_vector.astype(np.float32), search_limit)
            key_to_id = dict(self._key_to_id)

        results: list[VectorResult] = []
        for key, distance in zip(matches.keys, matches.distances):
            key_int = int(key)
            if key_int not in key_to_id:
                continue

            chunk_id = key_to_id[key_int]
            if chunk_ids_filter and chunk_id not in chunk_ids_filter:
                continue

            # USearch cosine returns distance (1 - similarity), convert to similarity
            similarity = 1.0 - float(distance)
            results.append(VectorResult(chunk_id=chunk_id, score=similarity))

            if len(results) >= limit:
                break

        return results

    @property
    def count(self) -> int:
        return len(self._id_to_key)

    def get_vector(self, chunk_id: str) -> np.ndarray | None:
        """Return the stored vector for ``chunk_id`` or ``None`` if absent.

        Used by the Memory-Layer integrity pass to compute pairwise
        cosine similarity between indexed qa_log chunks without
        re-embedding their contents.
        """
        with self._lock:
            key = self._id_to_key.get(chunk_id)
            if key is None:
                return None
            try:
                vec = self._index.get(int(key))
            except Exception:
                return None
        if vec is None:
            return None
        arr = np.asarray(vec).reshape(-1).astype(np.float32)
        return arr if arr.size == self._dim else None

    def save(self) -> None:
        """Persist index and mappings to disk."""
        self._ensure_writable()
        with self._lock:
            if len(self._id_to_key) > 0:
                self._index.save(str(self._index_path))

            # Save key mappings
            mapping_path = self._index_dir / "key_mapping.npz"
            if self._key_to_id:
                keys = list(self._key_to_id.keys())
                ids = list(self._key_to_id.values())
                np.savez(
                    str(mapping_path),
                    keys=np.array(keys, dtype=np.int64),
                    ids=np.array(ids, dtype=object),
                    next_key=np.array([self._next_key]),
                )

    def _load(self) -> None:
        """Load index and mappings from disk if they exist."""
        mapping_path = self._index_dir / "key_mapping.npz"

        if self._index_path.exists() and mapping_path.exists():
            try:
                # Load mappings first to know the keys
                data = np.load(str(mapping_path), allow_pickle=True)
                keys = data["keys"].tolist()
                ids = data["ids"].tolist()
                self._next_key = int(data["next_key"][0])

                self._key_to_id = dict(zip(keys, ids))
                self._id_to_key = dict(zip(ids, keys))

                self._migrate_dtype_if_needed()

                # Load the HNSW index. USearch's load() adopts the FILE
                # header's scalar kind, discarding the constructor dtype —
                # which is why the dtype migration above must rewrite the
                # file first, not just construct with f32.
                self._index.load(str(self._index_path))
                logger.info("Loaded vector index: %d vectors", self.count)
            except VectorMigrationError as e:
                # The full index is still on disk (original preserved) but
                # this process only holds an empty one. Serving reads as
                # empty is degraded-but-safe; ACCEPTING WRITES IS NOT — an
                # incremental reindex would save a partial index over the
                # complete one. Block writes until a full rebuild.
                logger.error("Vector index migration failed (writes blocked): %s", e)
                self._migration_failed = True
                self._reset_in_memory()
            except Exception:
                logger.warning("Failed to load vector index, starting fresh")
                self._reset_in_memory()

    def _reset_in_memory(self) -> None:
        self._key_to_id = {}
        self._id_to_key = {}
        self._next_key = 0
        self._index = Index(
            ndim=self._dim,
            metric=MetricKind.Cos,
            dtype=INDEX_DTYPE,
            connectivity=HNSW_M,
            expansion_add=HNSW_EF_CONSTRUCTION,
        )

    def _needs_dtype_migration(self) -> bool:
        try:
            metadata = Index.metadata(str(self._index_path))
        except Exception:
            return False  # unreadable header: let load() surface the failure
        return bool(metadata) and metadata.get("kind_scalar") != INDEX_DTYPE

    def _migrate_dtype_if_needed(self) -> None:
        """One-time atomic rewrite of a pre-0.7.2 (BF16) index as f32.

        The scalar kind is persisted in the usearch file header, so
        existing 0.7.1 installs keep hitting the BF16 NEON kernel after a
        package upgrade unless the file itself is rewritten. Vectors are
        extracted losslessly (BF16 → F32 widening) and re-added to a
        fresh f32 index — no re-embedding, no API calls. The new file is
        written to a temp path and swapped in with os.replace, so an
        interrupted migration leaves the original index untouched.

        Migration is serialized by a cross-process lock. Unique temp
        files alone make concurrent migrators safe against EACH OTHER,
        but not against a migrator racing an incremental WRITER: without
        the lock, a slow migrator could os.replace its pre-migration
        snapshot over an index that a faster process had already
        migrated AND extended with new vectors — silently dropping them.
        The lock decides who migrates; everyone else waits, then
        re-checks the header (the winner usually finished the job).
        """
        lock = _MigrationLock(self._index_path)
        while self._needs_dtype_migration():
            try:
                acquired = lock.acquire_or_wait()
            except TimeoutError as e:
                raise VectorMigrationError(str(e)) from e
            if not acquired:
                # The holder released; loop re-checks the header. Usually
                # it migrated and we fall out — but if it FAILED, the
                # header is still BF16 and we must contend for the lock
                # again rather than migrate lockless.
                continue
            try:
                # Re-check under the lock: another process may have
                # completed the migration while we raced for the mkdir.
                if not self._needs_dtype_migration():
                    return
                self._perform_dtype_migration()
                return
            finally:
                lock.release()

    def _perform_dtype_migration(self) -> None:
        disk_kind = Index.metadata(str(self._index_path)).get("kind_scalar")
        logger.info(
            "Migrating vector index %s → %s (one-time, %d vectors)",
            disk_kind, INDEX_DTYPE, len(self._key_to_id),
        )
        # Temp path is unique per process AND thread: VectorEngine is
        # constructed per search, so two sessions opening the same project
        # right after an upgrade would otherwise truncate each other's
        # half-written temp file. Both may still migrate independently —
        # each produces a valid f32 index and os.replace is atomic, so
        # whichever lands last wins with identical content.
        tmp_path = self._index_path.with_name(
            f"{self._index_path.name}.migrating.{os.getpid()}.{threading.get_ident()}"
        )
        try:
            old_index = Index.restore(str(self._index_path))
            if len(old_index) != len(self._key_to_id):
                # More vectors than mapping entries = orphans that already
                # lost their chunk_id; fewer = the per-key check below
                # will name the missing one. Either way the stores
                # disagree — a full rebuild, not a migration, is the fix.
                raise VectorMigrationError(
                    f"existing index holds {len(old_index)} vectors but the "
                    f"mapping has {len(self._key_to_id)} entries"
                )
            migrated = Index(
                ndim=self._dim,
                metric=MetricKind.Cos,
                dtype=INDEX_DTYPE,
                connectivity=HNSW_M,
                expansion_add=HNSW_EF_CONSTRUCTION,
            )
            for key in self._key_to_id:
                # "Lossless" is the contract: a mapped key the old index
                # does not contain means the mapping and index disagree —
                # replacing the file would bake that corruption in. NOTE:
                # usearch get() returns an array even for absent keys, so
                # containment is the only reliable check.
                if int(key) not in old_index:
                    raise VectorMigrationError(
                        f"mapped vector key {key} missing from the existing index"
                    )
                vec = old_index.get(int(key))
                migrated.add(int(key), np.asarray(vec).reshape(-1).astype(np.float32))

            if len(migrated) != len(self._key_to_id):
                raise VectorMigrationError(
                    f"migrated vector count {len(migrated)} != mapping "
                    f"count {len(self._key_to_id)}"
                )

            migrated.save(str(tmp_path))
            tmp_meta = Index.metadata(str(tmp_path))
            if not tmp_meta or tmp_meta.get("kind_scalar") != INDEX_DTYPE:
                kind = tmp_meta.get("kind_scalar") if tmp_meta else None
                raise VectorMigrationError(
                    f"temporary index scalar kind is {kind}, expected {INDEX_DTYPE}"
                )
            os.replace(tmp_path, self._index_path)
        except VectorMigrationError:
            raise
        except Exception as e:
            raise VectorMigrationError(f"dtype migration failed: {e}") from e
        finally:
            tmp_path.unlink(missing_ok=True)
        logger.info("Vector index migrated to %s", INDEX_DTYPE)
