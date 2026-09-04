"""Embedding generation via an OpenAI-shaped HTTP API.

Zero local resource usage — no model loading, no GPU, no CPU overhead,
just HTTP calls. The concrete endpoint comes from ``providers.py``, so
any OpenAI-compatible service (OpenAI, Gemini's /v1beta/openai surface)
works without a second client.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error

import numpy as np

from hybrid_search import providers, usage
from hybrid_search.config import EmbeddingConfig

logger = logging.getLogger(__name__)

OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIM = 1536
MAX_BATCH_TOKENS = 250_000  # OpenAI limit is ~300k; leave headroom

# Env overrides — let an A/B run switch endpoint or model without editing
# config.toml. See providers.PROVIDER_ENV for the provider itself.
BASE_URL_ENV = "HYBRID_SEARCH_EMBED_BASE_URL"
MODEL_ENV = "HYBRID_SEARCH_EMBED_MODEL"
DIM_ENV = "HYBRID_SEARCH_EMBED_DIM"


class _BatchTooLargeError(Exception):
    """Raised when OpenAI returns 400, likely due to batch size."""
    pass


class Embedder:
    """Generates embeddings via OpenAI API. Zero local resource usage."""

    def __init__(self, config: EmbeddingConfig, models_dir=None) -> None:
        self._config = config
        self._api_key: str | None = None
        self._spec = providers.resolve(config.backend)
        self._embed_url = (
            os.environ.get(BASE_URL_ENV, "").rstrip("/")
            or (config.base_url or "").rstrip("/")
            or self._spec.base_url
        ) + "/embeddings"
        self._model = _resolve_model(config, self._spec)
        self._embedding_dim = _resolve_dim(config, self._spec)
        # Texts dropped to the model's per-input ceiling since construction.
        # Truncation is lossy and, on Gemini, silent — surfaced as one
        # summary warning per run instead of a line per chunk.
        self._truncated = 0

    @property
    def embedding_dim(self) -> int:
        return self._embedding_dim

    @property
    def fingerprint(self) -> str:
        """Identity of the vector space this embedder produces.

        Two embedders agreeing on width still produce incomparable
        vectors when the model differs, so the stored index records this
        and the search side refuses to mix spaces.
        """
        return f"{self._spec.name}:{self._model}:{self._embedding_dim}"

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
        key = providers.api_key(self._spec)
        if not key:
            raise ValueError(
                f"{self._spec.key_env} not found. Set it in environment "
                f"or .env.local (embedding provider: {self._spec.name})"
            )
        self._api_key = key
        return key

    def _openai_embed_request(self, texts: list[str]) -> list[list[float]]:
        """Call OpenAI embeddings API with halve-and-retry on 400 errors."""
        if not texts:
            return []

        api_key = self._get_api_key()
        model = self._model
        truncated = [self._truncate(t) for t in texts]

        # Try the full batch first; on 400 error, halve and retry recursively
        try:
            return self._openai_embed_single_batch(truncated, model, api_key)
        except _BatchTooLargeError:
            if len(truncated) == 1:
                # Single text still too large — truncate more aggressively
                half = max(256, self._token_budget() // 2)
                logger.warning("Single text too large, truncating to %d tokens", half)
                truncated = [self._truncate(texts[0], max_tokens=half)]
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
        body: dict = {"model": model, "input": texts}
        if self._spec.supports_dimensions:
            # Gemini's default output is 3072-wide; asking for the index's
            # width (MRL) is what keeps the on-disk layout unchanged.
            body["dimensions"] = self._embedding_dim
        payload = json.dumps(body).encode("utf-8")

        # Optional wall-clock deadline (seconds) for this embed call.
        # Set by the BLOCKING pre-fetch hook only: a batch job saturating
        # the shared Ollama makes query embeds queue for tens of seconds,
        # the hook times out, and the whole injected context is discarded
        # (2026-09-04 Mac-mini field check: 22s embeds, 10s hook budget).
        # Expiring raises ConnectionError so the existing fail-open serves
        # BM25-only — a degraded context beats a discarded one. Unset
        # (indexing, MCP server) keeps the generous 120s per attempt.
        import time as _t

        deadline_env = os.environ.get("HYBRID_SEARCH_EMBED_DEADLINE")
        deadline = _t.monotonic() + float(deadline_env) if deadline_env else None

        max_retries = 12
        for attempt in range(max_retries):
            attempt_timeout = 120.0
            if deadline is not None:
                remaining = deadline - _t.monotonic()
                if remaining <= 0.05:
                    raise ConnectionError(
                        f"{self._spec.name} embeddings: deadline exceeded "
                        f"({deadline_env}s) — degrading to BM25-only"
                    )
                attempt_timeout = min(attempt_timeout, remaining)
            req = urllib.request.Request(
                self._embed_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=attempt_timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                # Accounting, not content: how much was sent, never what.
                usage.record(
                    kind="embed", provider=self._spec.name, model=model,
                    items=len(texts),
                    tokens=int((data.get("usage") or {}).get("prompt_tokens")
                               or self._estimate_tokens(texts)),
                )
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
                    if deadline is not None and _t.monotonic() + wait >= deadline:
                        raise ConnectionError(
                            f"{self._spec.name} embeddings: rate-limited past "
                            "the deadline — degrading to BM25-only"
                        ) from e
                    logger.info("Rate limited, waiting %.1fs (attempt %d/%d)", wait, attempt + 1, max_retries)
                    _time.sleep(wait)
                    continue
                raise ConnectionError(
                    f"{self._spec.name} embeddings error {e.code}: {body}"
                ) from e
            except urllib.error.URLError as e:
                raise ConnectionError(
                    f"{self._spec.name} embeddings not reachable: {e}"
                ) from e
        raise ConnectionError(
            f"{self._spec.name} embeddings: max retries exhausted"
        )

    _enc = None  # lazy-loaded tiktoken encoder

    def _estimate_tokens(self, texts: list[str]) -> int:
        """Fallback when the provider does not report usage."""
        if Embedder._enc is None:
            import tiktoken
            Embedder._enc = tiktoken.encoding_for_model("text-embedding-3-small")
        return sum(len(Embedder._enc.encode(t)) for t in texts)

    def _token_budget(self) -> int:
        """Per-input token ceiling, discounted for tokenizer mismatch.

        Counting happens with tiktoken regardless of provider, so the raw
        model limit is divided by the provider's measured skew. Erring low
        costs a little tail text; erring high loses it invisibly on
        providers that truncate without saying so.
        """
        limit = providers.input_limit(self._spec, self._model)
        return max(256, int(limit / self._spec.tokenizer_skew))

    def _truncate(self, text: str, max_tokens: int | None = None) -> str:
        """Truncate ``text`` to the provider's per-input token ceiling."""
        budget = max_tokens if max_tokens is not None else self._token_budget()
        if Embedder._enc is None:
            import tiktoken
            Embedder._enc = tiktoken.encoding_for_model("text-embedding-3-small")
        tokens = Embedder._enc.encode(text)
        if len(tokens) <= budget:
            return text
        self._truncated += 1
        return Embedder._enc.decode(tokens[:budget])

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

        if self._truncated:
            # Not a crash and not recoverable after the fact — but a silent
            # drop would show up much later as "why is this code missing
            # from search", so it gets said out loud exactly once.
            logger.warning(
                "%d text(s) exceeded the %s per-input limit (%d tokens) and "
                "were truncated — their tails are not in the embedding",
                self._truncated, self._spec.name, self._token_budget(),
            )
            self._truncated = 0

        embeddings = np.vstack(all_embeddings)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        return (embeddings / norms).astype(np.float32)


def _resolve_model(config: EmbeddingConfig, spec: providers.ProviderSpec) -> str:
    """Embedding model name: env, then config, then the provider default.

    ``openai_model`` carries a non-empty default, so it may only speak for
    the OpenAI provider — otherwise switching backends would silently ask
    Gemini for text-embedding-3-small.
    """
    env = os.environ.get(MODEL_ENV, "").strip()
    if env:
        return env
    if spec.name == "openai":
        return config.openai_model or spec.embed_model
    return config.model or spec.embed_model


def _resolve_dim(config: EmbeddingConfig, spec: providers.ProviderSpec) -> int:
    """Embedding width: env, then config, then the provider default."""
    env = os.environ.get(DIM_ENV, "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return config.dimensions or spec.embed_dim


# Kept as a module-level name: tests patch it, and translation.py imports it.
_load_dotenv_key = providers.load_dotenv_key
