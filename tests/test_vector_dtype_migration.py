"""BF16 → F32 vector index migration (0.7.1 → 0.7.2 upgrade path).

USearch persists the scalar kind in the index file header and load()
adopts it, discarding the constructor dtype — so pinning F32 in code
does NOT fix existing 0.7.1 (BF16) indexes on disk. VectorEngine._load
must rewrite the file (atomic, no re-embedding) before serving it, and
a FAILED migration must block every write: the process holds an empty
in-memory index over a full on-disk one, so an incremental add + save
would replace the complete index with a partial one.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import pytest
from usearch.index import Index, MetricKind, ScalarKind

from hybrid_search.search.vector import (
    HNSW_EF_CONSTRUCTION,
    HNSW_M,
    INDEX_DTYPE,
    VectorEngine,
    VectorMigrationError,
)

DIM = 8


def _rand_vectors(n: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    vecs = rng.random((n, DIM), dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def _write_bf16_index(index_dir, vectors: np.ndarray, extra_keys: int = 0) -> list[str]:
    """Materialize a 0.7.1-style on-disk index: BF16 (explicit — some CPUs
    would auto-pick a different default) plus the key_mapping.npz
    VectorEngine expects. ``extra_keys`` appends mapping entries with no
    backing vector, simulating a mapping/index disagreement."""
    index_dir.mkdir(parents=True, exist_ok=True)
    old = Index(
        ndim=DIM,
        metric=MetricKind.Cos,
        dtype=ScalarKind.BF16,
        connectivity=HNSW_M,
        expansion_add=HNSW_EF_CONSTRUCTION,
    )
    n = len(vectors)
    chunk_ids = [f"chunk-{i}" for i in range(n + extra_keys)]
    for i, v in enumerate(vectors):
        old.add(i, v)
    old.save(str(index_dir / "vectors.usearch"))
    np.savez(
        str(index_dir / "key_mapping.npz"),
        keys=np.arange(n + extra_keys, dtype=np.int64),
        ids=np.array(chunk_ids, dtype=object),
        next_key=np.array([n + extra_keys]),
    )
    return chunk_ids


def _disk_scalar_kind(index_dir):
    return Index.metadata(str(index_dir / "vectors.usearch"))["kind_scalar"]


def _disk_bytes(index_dir) -> tuple[bytes, bytes]:
    return (
        (index_dir / "vectors.usearch").read_bytes(),
        (index_dir / "key_mapping.npz").read_bytes(),
    )


class TestNewIndexUsesF32:
    def test_new_index_uses_f32(self, tmp_path) -> None:
        eng = VectorEngine(tmp_path / "vec", DIM)
        assert eng._index.dtype == ScalarKind.F32
        eng.add("c1", _rand_vectors(1)[0])
        eng.save()
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.F32


class TestF32Roundtrip:
    def test_f32_index_roundtrip_preserves_count_and_search(self, tmp_path) -> None:
        vecs = _rand_vectors(6)
        eng = VectorEngine(tmp_path / "vec", DIM)
        for i, v in enumerate(vecs):
            eng.add(f"chunk-{i}", v)
        eng.save()

        reloaded = VectorEngine(tmp_path / "vec", DIM)
        assert reloaded.count == 6
        assert reloaded._index.dtype == ScalarKind.F32
        top = reloaded.search(vecs[3], limit=1)
        assert top and top[0].chunk_id == "chunk-3"


class TestBf16Migration:
    def test_bf16_index_is_migrated_to_f32_without_losing_vectors(self, tmp_path) -> None:
        vecs = _rand_vectors(10)
        chunk_ids = _write_bf16_index(tmp_path / "vec", vecs)

        eng = VectorEngine(tmp_path / "vec", DIM)  # triggers migration in _load

        assert not eng.migration_failed
        assert eng.count == len(chunk_ids)
        assert eng._index.dtype == ScalarKind.F32
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.F32
        for i, cid in enumerate(chunk_ids):
            top = eng.search(vecs[i], limit=1)
            assert top and top[0].chunk_id == cid

    def test_second_startup_does_not_migrate_again(self, tmp_path, caplog) -> None:
        _write_bf16_index(tmp_path / "vec", _rand_vectors(3))
        VectorEngine(tmp_path / "vec", DIM)  # migrates

        with caplog.at_level(logging.INFO, logger="hybrid_search.search.vector"):
            VectorEngine(tmp_path / "vec", DIM)
        assert not any("Migrating" in r.message for r in caplog.records)

    def test_f32_index_is_left_untouched(self, tmp_path) -> None:
        vecs = _rand_vectors(4)
        eng = VectorEngine(tmp_path / "vec", DIM)
        for i, v in enumerate(vecs):
            eng.add(f"chunk-{i}", v)
        eng.save()
        mtime = (tmp_path / "vec" / "vectors.usearch").stat().st_mtime_ns

        VectorEngine(tmp_path / "vec", DIM)
        assert (tmp_path / "vec" / "vectors.usearch").stat().st_mtime_ns == mtime

    def test_no_leftover_temp_files_after_migration(self, tmp_path) -> None:
        _write_bf16_index(tmp_path / "vec", _rand_vectors(3))
        VectorEngine(tmp_path / "vec", DIM)
        leftovers = list((tmp_path / "vec").glob("*.migrating*"))
        assert leftovers == []


class TestMigrationValidation:
    def test_missing_mapped_key_fails_migration(self, tmp_path) -> None:
        # A mapping key with no backing vector = mapping/index
        # disagreement. "Lossless" means fail, not skip-and-shrink.
        _write_bf16_index(tmp_path / "vec", _rand_vectors(5), extra_keys=2)
        before = _disk_bytes(tmp_path / "vec")

        eng = VectorEngine(tmp_path / "vec", DIM)

        assert eng.migration_failed
        assert _disk_bytes(tmp_path / "vec") == before
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.BF16


class TestFailedMigrationBlocksWrites:
    def _failed_engine(self, tmp_path, monkeypatch) -> VectorEngine:
        _write_bf16_index(tmp_path / "vec", _rand_vectors(5))

        def boom(self, path):
            raise OSError("simulated crash during migration save")

        monkeypatch.setattr(Index, "save", boom)
        eng = VectorEngine(tmp_path / "vec", DIM)
        monkeypatch.undo()
        assert eng.migration_failed
        return eng

    def test_failed_migration_cannot_overwrite_after_new_vectors_are_added(
        self, tmp_path, monkeypatch
    ) -> None:
        # The round-3 review scenario: migration fails, an incremental
        # reindex adds vectors for a few changed files, then save() —
        # which would replace the FULL on-disk index with a partial one.
        # Every write must raise instead.
        eng = self._failed_engine(tmp_path, monkeypatch)
        before = _disk_bytes(tmp_path / "vec")
        vec = _rand_vectors(1)[0]

        with pytest.raises(VectorMigrationError):
            eng.add("new-chunk", vec)
        with pytest.raises(VectorMigrationError):
            eng.add_batch(["new-chunk"], vec.reshape(1, -1))
        with pytest.raises(VectorMigrationError):
            eng.remove("chunk-0")
        with pytest.raises(VectorMigrationError):
            eng.remove_batch(["chunk-0"])
        with pytest.raises(VectorMigrationError):
            eng.save()

        assert _disk_bytes(tmp_path / "vec") == before
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.BF16

    def test_failed_migration_serves_empty_reads(self, tmp_path, monkeypatch) -> None:
        eng = self._failed_engine(tmp_path, monkeypatch)
        assert eng.count == 0
        assert eng.search(_rand_vectors(1)[0], limit=5) == []


class TestInterruptedMigration:
    def test_interrupted_migration_preserves_the_original_index(
        self, tmp_path, monkeypatch
    ) -> None:
        vecs = _rand_vectors(5)
        _write_bf16_index(tmp_path / "vec", vecs)
        before = _disk_bytes(tmp_path / "vec")

        def boom(self, path):
            raise OSError("simulated crash during migration save")

        monkeypatch.setattr(Index, "save", boom)
        failed = VectorEngine(tmp_path / "vec", DIM)
        assert failed.migration_failed

        assert _disk_bytes(tmp_path / "vec") == before
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.BF16
        assert list((tmp_path / "vec").glob("*.migrating*")) == []

        # Next startup (no crash) completes the migration.
        monkeypatch.undo()
        eng = VectorEngine(tmp_path / "vec", DIM)
        assert not eng.migration_failed
        assert eng.count == 5
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.F32


class TestConcurrentMigration:
    def test_two_concurrent_engines_migrate_one_bf16_index_safely(self, tmp_path) -> None:
        # Two sessions open the same project right after an upgrade. Force
        # both migrations to sit inside the save window simultaneously via
        # a barrier — with a shared temp path they would truncate each
        # other; with per-thread temp paths both produce a valid f32 index
        # and the atomic replaces land sequentially.
        vecs = _rand_vectors(6)
        chunk_ids = _write_bf16_index(tmp_path / "vec", vecs)

        barrier = threading.Barrier(2, timeout=10)
        original_save = Index.save
        crossed = {"count": 0}

        def synced_save(self, path):
            if ".migrating." in str(path):
                try:
                    barrier.wait()
                    crossed["count"] += 1
                except threading.BrokenBarrierError:
                    pass  # partner already finished — proceed alone
            return original_save(self, path)

        engines: list[VectorEngine | None] = [None, None]
        errors: list[Exception] = []

        def construct(slot: int) -> None:
            try:
                engines[slot] = VectorEngine(tmp_path / "vec", DIM)
            except Exception as e:  # pragma: no cover - fail the test below
                errors.append(e)

        import unittest.mock as mock

        with mock.patch.object(Index, "save", synced_save):
            threads = [threading.Thread(target=construct, args=(i,)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        assert not errors
        # Both threads actually overlapped inside the migration window.
        assert crossed["count"] == 2
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.F32
        assert list((tmp_path / "vec").glob("*.migrating*")) == []
        fresh = VectorEngine(tmp_path / "vec", DIM)
        assert fresh.count == len(chunk_ids)
        top = fresh.search(vecs[2], limit=1)
        assert top and top[0].chunk_id == chunk_ids[2]


class TestPipelineFailureBranch:
    def test_pipeline_rebuilds_atomically_when_migration_fails(self, tmp_path) -> None:
        """Migration failure during incremental indexing must never touch
        the existing stores — the pipeline routes to a full atomic
        rebuild in a fresh directory and ends healthy (f32, consistent)."""
        from hybrid_search.config import Config, EmbeddingConfig
        from hybrid_search.index.pipeline import IndexingPipeline
        from hybrid_search.project import ProjectRegistry, project_hash
        from hybrid_search.storage.indexes import get_project_dir

        class _FakeEmbedder:
            embedding_dim = DIM

            def embed_texts(self, texts):
                rng = np.random.default_rng(7)
                return rng.random((len(texts), DIM), dtype=np.float32)

        config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig(batch_size=8))
        registry = ProjectRegistry(config.global_dir)
        pipeline = IndexingPipeline(config, registry, _FakeEmbedder())

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        pipeline.index_project(str(repo))

        # Corrupt the vector store into an unmigratable 0.7.1 state:
        # BF16 index whose mapping references keys the index lacks.
        pid = project_hash(str(repo.resolve()))
        vec_dir = get_project_dir(config.projects_dir, pid) / "vectors"
        for f in vec_dir.iterdir():
            f.unlink()
        _write_bf16_index(vec_dir, _rand_vectors(3), extra_keys=2)

        (repo / "extra.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
        result = pipeline.index_project(str(repo), changed_paths=["extra.py"])

        assert any("vector dtype migration failed" in e for e in result.errors)
        vec_dir = get_project_dir(config.projects_dir, pid) / "vectors"
        assert _disk_scalar_kind(vec_dir) == ScalarKind.F32
        eng = VectorEngine(vec_dir, DIM)
        assert not eng.migration_failed
        assert eng.count == result.chunks_total > 0


class TestUpgradeE2E:
    def test_071_bf16_index_startup_on_072_code(self, tmp_path) -> None:
        """0.7.1-format BF16 index → 0.7.2 startup → F32, same count,
        same top result for a known query."""
        vecs = _rand_vectors(12)
        chunk_ids = _write_bf16_index(tmp_path / "vec", vecs)

        # Ground truth from the ORIGINAL BF16 index, before migration.
        bf16 = Index.restore(str(tmp_path / "vec" / "vectors.usearch"))
        query = vecs[7]
        expected_key = int(bf16.search(query, 1).keys[0])
        expected_chunk = chunk_ids[expected_key]

        eng = VectorEngine(tmp_path / "vec", DIM)
        assert eng._index.dtype == ScalarKind.F32
        assert INDEX_DTYPE == ScalarKind.F32
        assert eng.count == len(chunk_ids)
        top = eng.search(query, limit=1)
        assert top and top[0].chunk_id == expected_chunk


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
