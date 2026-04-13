"""Embedding generation via Ollama (GPU).

Uses Ollama's /api/embed endpoint for GPU-accelerated embedding.
Supports Qwen3-Embedding, nomic-embed-text, etc.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

import numpy as np

from hybrid_search.config import EmbeddingConfig

logger = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"


class Embedder:
    """Generates embeddings via Ollama GPU backend."""

    def __init__(self, config: EmbeddingConfig, models_dir=None) -> None:
        self._config = config
        self._embedding_dim: int | None = None

    @property
    def embedding_dim(self) -> int:
        if self._embedding_dim is None:
            self._ensure_loaded()
        return self._embedding_dim  # type: ignore[return-value]

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Embed a list of texts. Returns (N, dim) float32 array."""
        self._ensure_loaded()
        if not texts:
            return np.empty((0, self._embedding_dim), dtype=np.float32)
        return self._embed_all(texts)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query with 'query:' prefix. Returns (dim,) array."""
        prefixed = f"query: {query}"
        result = self.embed_texts([prefixed])
        return result[0]

    def _ensure_loaded(self) -> None:
        if self._embedding_dim is not None:
            return
        ollama_model = self._config.ollama_model
        if not ollama_model:
            raise ValueError(
                "No ollama_model configured. Set [embedding].ollama_model in config.toml "
                "(e.g., 'qwen3-embedding:0.6b')"
            )
        logger.info("Testing Ollama embedding model: %s", ollama_model)
        test_result = self._ollama_embed_request(["dimension probe"])
        self._embedding_dim = len(test_result[0])
        logger.info("Ollama model ready: %s, dim=%d", ollama_model, self._embedding_dim)

    def _ollama_embed_request(self, texts: list[str]) -> list[list[float]]:
        """Call Ollama POST /api/embed endpoint."""
        url = f"{OLLAMA_DEFAULT_URL}/api/embed"
        payload = json.dumps({
            "model": self._config.ollama_model,
            "input": texts,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Ollama server not reachable at {OLLAMA_DEFAULT_URL}. "
                f"Is Ollama running? ('brew services start ollama') Error: {e}"
            ) from e

        if "embeddings" not in data:
            raise ValueError(f"Unexpected Ollama response: {list(data.keys())}")
        return data["embeddings"]

    def _embed_all(self, texts: list[str]) -> np.ndarray:
        """Embed texts via Ollama in batches."""
        all_embeddings: list[np.ndarray] = []
        batch_size = self._config.batch_size
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            raw = self._ollama_embed_request(batch)
            all_embeddings.append(np.array(raw, dtype=np.float32))
        embeddings = np.vstack(all_embeddings)
        # L2 normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        return (embeddings / norms).astype(np.float32)
