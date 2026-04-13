"""USearch-based HNSW vector search engine."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from usearch.index import Index, MetricKind

logger = logging.getLogger(__name__)

# HNSW parameters per design doc §13
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200


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

        self._index = Index(
            ndim=embedding_dim,
            metric=MetricKind.Cos,
            connectivity=HNSW_M,
            expansion_add=HNSW_EF_CONSTRUCTION,
        )

        self._load()

    def add(self, chunk_id: str, vector: np.ndarray) -> None:
        """Add a single vector to the index."""
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
        for i, cid in enumerate(chunk_ids):
            self.add(cid, vectors[i])

    def remove(self, chunk_id: str) -> None:
        """Remove a vector by chunk_id."""
        if chunk_id not in self._id_to_key:
            return
        key = self._id_to_key[chunk_id]
        self._index.remove(key)
        del self._key_to_id[key]
        del self._id_to_key[chunk_id]

    def remove_batch(self, chunk_ids: list[str]) -> None:
        """Remove multiple vectors."""
        for cid in chunk_ids:
            self.remove(cid)

    def search(
        self,
        query_vector: np.ndarray,
        limit: int = 10,
        chunk_ids_filter: set[str] | None = None,
    ) -> list[VectorResult]:
        """Search for nearest neighbors. Returns sorted by cosine similarity descending."""
        if len(self._key_to_id) == 0:
            return []

        # Search more than needed to account for filtering
        search_limit = min(limit * 3, len(self._key_to_id))
        matches = self._index.search(query_vector.astype(np.float32), search_limit)

        results: list[VectorResult] = []
        for key, distance in zip(matches.keys, matches.distances):
            key_int = int(key)
            if key_int not in self._key_to_id:
                continue

            chunk_id = self._key_to_id[key_int]
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

    def save(self) -> None:
        """Persist index and mappings to disk."""
        if self.count > 0:
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

                # Load the HNSW index
                self._index.load(str(self._index_path))
                logger.info("Loaded vector index: %d vectors", self.count)
            except Exception:
                logger.warning("Failed to load vector index, starting fresh")
                self._key_to_id = {}
                self._id_to_key = {}
                self._next_key = 0
                self._index = Index(
                    ndim=self._dim,
                    metric=MetricKind.Cos,
                    connectivity=HNSW_M,
                    expansion_add=HNSW_EF_CONSTRUCTION,
                )
