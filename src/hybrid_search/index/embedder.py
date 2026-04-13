"""Embedding generation via OpenAI API.

Uses text-embedding-3-small for lightweight, zero-local-resource embedding.
No model loading, no GPU, no CPU overhead — just HTTP calls.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from pathlib import Path

import numpy as np

from hybrid_search.config import EmbeddingConfig

logger = logging.getLogger(__name__)

OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIM = 1536


class Embedder:
    """Generates embeddings via OpenAI API. Zero local resource usage."""

    def __init__(self, config: EmbeddingConfig, models_dir=None) -> None:
        self._config = config
        self._api_key: str | None = None
        self._embedding_dim: int = DEFAULT_DIM

    @property
    def embedding_dim(self) -> int:
        return self._embedding_dim

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Embed a list of texts. Returns (N, dim) float32 array."""
        if not texts:
            return np.empty((0, self._embedding_dim), dtype=np.float32)
        return self._embed_all(texts)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query. Returns (dim,) array."""
        result = self.embed_texts([query])
        return result[0]

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key

        # 1. Environment variable
        key = os.environ.get("OPENAI_API_KEY", "")

        # 2. .env.local in project root (walk up from cwd)
        if not key:
            key = _load_dotenv_key("OPENAI_API_KEY")

        if not key:
            raise ValueError(
                "OPENAI_API_KEY not found. Set it in environment or .env.local"
            )
        self._api_key = key
        return key

    def _openai_embed_request(self, texts: list[str]) -> list[list[float]]:
        """Call OpenAI embeddings API."""
        api_key = self._get_api_key()
        model = self._config.openai_model or DEFAULT_MODEL

        truncated = [self._truncate(t) for t in texts]

        payload = json.dumps({
            "model": model,
            "input": truncated,
        }).encode("utf-8")

        req = urllib.request.Request(
            OPENAI_EMBED_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise ConnectionError(
                f"OpenAI API error {e.code}: {body}"
            ) from e
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"OpenAI API not reachable: {e}"
            ) from e

        # Response: {"data": [{"embedding": [...], "index": 0}, ...]}
        embeddings = [item["embedding"] for item in data["data"]]
        return embeddings

    _enc = None  # lazy-loaded tiktoken encoder

    def _truncate(self, text: str, max_tokens: int = 8000) -> str:
        """Truncate text to fit within OpenAI's 8192 token limit."""
        if Embedder._enc is None:
            import tiktoken
            Embedder._enc = tiktoken.encoding_for_model("text-embedding-3-small")
        tokens = Embedder._enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return Embedder._enc.decode(tokens[:max_tokens])

    def _embed_all(self, texts: list[str]) -> np.ndarray:
        """Embed texts via OpenAI API in batches."""
        all_embeddings: list[np.ndarray] = []
        batch_size = self._config.batch_size
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            raw = self._openai_embed_request(batch)
            all_embeddings.append(np.array(raw, dtype=np.float32))
        embeddings = np.vstack(all_embeddings)
        # OpenAI returns normalized vectors, but verify
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        return (embeddings / norms).astype(np.float32)


def _load_dotenv_key(key: str) -> str:
    """Load a key from .env.local file, searching up from cwd."""
    current = Path.cwd()
    for _ in range(10):  # max 10 levels up
        env_file = current / ".env.local"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ""
