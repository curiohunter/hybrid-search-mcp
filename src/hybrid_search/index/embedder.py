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
MAX_BATCH_TOKENS = 250_000  # OpenAI limit is ~300k; leave headroom


class _BatchTooLargeError(Exception):
    """Raised when OpenAI returns 400, likely due to batch size."""
    pass


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
        """Call OpenAI embeddings API with halve-and-retry on 400 errors."""
        if not texts:
            return []

        api_key = self._get_api_key()
        model = self._config.openai_model or DEFAULT_MODEL
        truncated = [self._truncate(t) for t in texts]

        # Try the full batch first; on 400 error, halve and retry recursively
        try:
            return self._openai_embed_single_batch(truncated, model, api_key)
        except _BatchTooLargeError:
            if len(truncated) == 1:
                # Single text still too large — truncate more aggressively
                logger.warning("Single text too large, truncating to 4000 tokens")
                truncated = [self._truncate(texts[0], max_tokens=4000)]
                return self._openai_embed_single_batch(truncated, model, api_key)

            mid = len(truncated) // 2
            logger.info("Batch too large (%d texts), splitting into %d + %d", len(truncated), mid, len(truncated) - mid)
            left = self._openai_embed_request(texts[:mid])
            right = self._openai_embed_request(texts[mid:])
            return left + right

    def _openai_embed_single_batch(
        self, texts: list[str], model: str, api_key: str,
    ) -> list[list[float]]:
        """Send a single batch to OpenAI. Raises _BatchTooLargeError on 400."""
        payload = json.dumps({
            "model": model,
            "input": texts,
        }).encode("utf-8")

        max_retries = 12
        for attempt in range(max_retries):
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
                return [item["embedding"] for item in data["data"]]
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                if e.code == 400:
                    raise _BatchTooLargeError(body) from e
                if e.code == 429 and attempt < max_retries - 1:
                    import re as _re
                    import time as _time
                    wait = min(30.0, 2.0 * (attempt + 1))
                    m = _re.search(r"try again in ([\d.]+)s", body)
                    ms = _re.search(r"try again in ([\d.]+)ms", body)
                    if m:
                        wait = float(m.group(1)) + 0.5
                    elif ms:
                        wait = float(ms.group(1)) / 1000.0 + 0.5
                    logger.info("Rate limited, waiting %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
                    _time.sleep(wait)
                    continue
                raise ConnectionError(
                    f"OpenAI API error {e.code}: {body}"
                ) from e
            except urllib.error.URLError as e:
                raise ConnectionError(
                    f"OpenAI API not reachable: {e}"
                ) from e
        raise ConnectionError("OpenAI API: max retries exhausted")

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

    def _split_into_token_batches(self, texts: list[str]) -> list[list[str]]:
        """Split texts into batches respecting both count and token limits."""
        if Embedder._enc is None:
            import tiktoken
            Embedder._enc = tiktoken.encoding_for_model("text-embedding-3-small")

        max_count = self._config.batch_size
        batches: list[list[str]] = []
        current_batch: list[str] = []
        current_tokens = 0

        for text in texts:
            token_count = len(Embedder._enc.encode(text))
            # Start new batch if adding this text would exceed limits
            if current_batch and (
                len(current_batch) >= max_count
                or current_tokens + token_count > MAX_BATCH_TOKENS
            ):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
            current_batch.append(text)
            current_tokens += token_count

        if current_batch:
            batches.append(current_batch)
        return batches

    def _embed_all(self, texts: list[str]) -> np.ndarray:
        """Embed texts via OpenAI API in token-aware batches."""
        import time as _time
        all_embeddings: list[np.ndarray] = []
        batches = self._split_into_token_batches(texts)

        for i, batch in enumerate(batches):
            raw = self._openai_embed_request(batch)
            all_embeddings.append(np.array(raw, dtype=np.float32))
            if i < len(batches) - 1:
                _time.sleep(0.2)

        embeddings = np.vstack(all_embeddings)
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
