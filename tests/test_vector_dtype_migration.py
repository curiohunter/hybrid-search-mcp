"""BF16 → F32 vector index migration (0.7.1 → 0.7.2 upgrade path).

USearch persists the scalar kind in the index file header and load()
adopts it, discarding the constructor dtype — so pinning F32 in code
does NOT fix existing 0.7.1 (BF16) indexes on disk. VectorEngine._load
must rewrite the file (atomic, no re-embedding) before serving it.
"""

from __future__ import annotations

import numpy as np
import pytest
from usearch.index import Index, MetricKind, ScalarKind

from hybrid_search.search.vector import (
    HNSW_EF_CONSTRUCTION,
    HNSW_M,
    INDEX_DTYPE,
    VectorEngine,
)

DIM = 8


def _rand_vectors(n: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    vecs = rng.random((n, DIM), dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def _write_bf16_index(index_dir, vectors: np.ndarray) -> list[str]:
    """Materialize a 0.7.1-style on-disk index: BF16 (the pre-fix
    usearch default) plus the key_mapping.npz VectorEngine expects."""
    index_dir.mkdir(parents=True, exist_ok=True)
    old = Index(
        ndim=DIM,
        metric=MetricKind.Cos,
        connectivity=HNSW_M,
        expansion_add=HNSW_EF_CONSTRUCTION,
    )
    assert old.dtype == ScalarKind.BF16  # the bug's precondition
    chunk_ids = [f"chunk-{i}" for i in range(len(vectors))]
    for i, v in enumerate(vectors):
        old.add(i, v)
    old.save(str(index_dir / "vectors.usearch"))
    np.savez(
        str(index_dir / "key_mapping.npz"),
        keys=np.arange(len(vectors), dtype=np.int64),
        ids=np.array(chunk_ids, dtype=object),
        next_key=np.array([len(vectors)]),
    )
    return chunk_ids


def _disk_scalar_kind(index_dir):
    return Index.metadata(str(index_dir / "vectors.usearch"))["kind_scalar"]


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

        assert eng.count == len(chunk_ids)
        assert eng._index.dtype == ScalarKind.F32
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.F32
        # Every original vector survives (BF16 → F32 widening is lossless
        # relative to the BF16 file, so self-query must return itself).
        for i, cid in enumerate(chunk_ids):
            top = eng.search(vecs[i], limit=1)
            assert top and top[0].chunk_id == cid

    def test_second_startup_does_not_migrate_again(self, tmp_path, caplog) -> None:
        _write_bf16_index(tmp_path / "vec", _rand_vectors(3))
        VectorEngine(tmp_path / "vec", DIM)  # migrates
        import logging

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


class TestInterruptedMigration:
    def test_interrupted_migration_preserves_the_original_index(
        self, tmp_path, monkeypatch
    ) -> None:
        vecs = _rand_vectors(5)
        _write_bf16_index(tmp_path / "vec", vecs)
        original_bytes = (tmp_path / "vec" / "vectors.usearch").read_bytes()

        # Crash mid-migration: the new index fails to save (disk full,
        # SIGKILL, …). The original BF16 file must remain byte-identical.
        def boom(self, path):
            raise OSError("simulated crash during migration save")

        monkeypatch.setattr(Index, "save", boom)
        VectorEngine(tmp_path / "vec", DIM)  # falls back to fresh in-memory

        assert (tmp_path / "vec" / "vectors.usearch").read_bytes() == original_bytes
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.BF16

        # Next startup (no crash) completes the migration.
        monkeypatch.undo()
        eng = VectorEngine(tmp_path / "vec", DIM)
        assert eng.count == 5
        assert _disk_scalar_kind(tmp_path / "vec") == ScalarKind.F32

    def test_failed_migration_never_clobbers_disk_on_save(
        self, tmp_path, monkeypatch
    ) -> None:
        # After a failed migration the engine starts fresh in memory;
        # an immediate save() with zero vectors must not overwrite the
        # original index or mapping files.
        _write_bf16_index(tmp_path / "vec", _rand_vectors(5))
        original_bytes = (tmp_path / "vec" / "vectors.usearch").read_bytes()

        def boom(self, path):
            raise OSError("simulated crash during migration save")

        monkeypatch.setattr(Index, "save", boom)
        eng = VectorEngine(tmp_path / "vec", DIM)
        monkeypatch.undo()
        eng.save()

        assert (tmp_path / "vec" / "vectors.usearch").read_bytes() == original_bytes
        data = np.load(str(tmp_path / "vec" / "key_mapping.npz"), allow_pickle=True)
        assert len(data["keys"]) == 5


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
