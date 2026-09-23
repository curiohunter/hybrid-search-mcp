"""OpenAI-shaped HTTP providers for the embedding and chat lanes.

The project talks to these APIs with raw urllib and no SDK, so a provider
is fully described by a base URL, a key, and model defaults. Gemini
exposes an OpenAI-compatible surface at ``/v1beta/openai``, which is why
it drops into the same client rather than needing its own.

Adding a provider means adding a row here — the callers read the spec.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    key_env: str
    embed_model: str
    embed_dim: int
    # Hard per-input ceiling of the embedding model. Gemini truncates past
    # this *silently* — a 4k-token input returns HTTP 200 with a
    # full-length vector — so exceeding it loses text with no error to
    # catch. The guard in the embedder exists because of this.
    max_input_tokens: int
    # Whether /embeddings honours an explicit ``dimensions`` field. Gemini
    # does (MRL), which is what lets it match the 1536-wide index layout.
    supports_dimensions: bool
    chat_model: str
    # Divisor applied to ``max_input_tokens`` because token budgets are
    # measured with tiktoken (OpenAI's tokenizer, the only one vendored).
    # 1.0 means the counts are exact for this provider.
    tokenizer_skew: float = 1.0
    # Local servers (Ollama) accept any bearer token; a placeholder is
    # sent so the request shape stays identical across providers.
    requires_key: bool = True
    # Token ceiling for ONE embed request. Not a provider limit but a
    # politeness bound: Ollama serves a model from a single runner
    # (`-np 1`, and 0.33 ignores OLLAMA_NUM_PARALLEL for embedding-only
    # models — measured 2026-09-23), so one bulk request holds the server
    # for its whole duration and every interactive query queues behind it.
    # Request duration scales with the tokens in it while throughput does
    # not, so a smaller ceiling buys latency almost for free. 0 means "use
    # the global cap" — right for a hosted API, which serves many requests
    # at once.
    max_batch_tokens: int = 0


# Measured against the shared Mac-mini Ollama (2026-09-23,
# qwen3-embedding:0.6b, one runner). Two runs, the second with warm-up and
# order control; "wait" is what one interactive query paid while a bulk
# request was in flight:
#
#   요청 토큰   요청(중위)         처리량              검색 대기(중위)
#    2,000      6.5s               223 tok/s            9.2s
#    4,000      9.9s               334 tok/s            5.3s
#    8,000      2.7–14.9s          398–2,014 tok/s      2.5–6.4s
#   32,000     15.5s             2,292 tok/s           15.3s
#
# Absolute numbers swing five-fold with whatever else is hitting that
# server (other sessions, its own work), so only what both runs agree on
# is load-bearing here:
#   1. The wait tracks the in-flight request's duration. That is the
#      mechanism — not bandwidth, a queue.
#   2. Request duration grows with the tokens in the request.
#   3. Bigger batches embed faster per token, with the gain flattening
#      above ~8k.
# So the ceiling trades bulk throughput for interactive latency, and the
# number is set by the pre-fetch deadline (2.5s, past which the session's
# injected context degrades to BM25-only): 4,096 is ~10 typical chunks per
# request, about 2s at the idle rate, and it equals max_input_tokens so a
# maximal single chunk still forms a legal batch.
#
# It is necessary, not sufficient — under concurrent load even 4k requests
# took ~10s here. Nothing client-side fixes that; the fail-open does.
OLLAMA_MAX_BATCH_TOKENS = 4_096

PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        embed_model="text-embedding-3-small",
        embed_dim=1536,
        max_input_tokens=8000,
        supports_dimensions=False,
        chat_model="gpt-4o-mini",
        tokenizer_skew=1.0,   # tiktoken IS the OpenAI tokenizer
    ),
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        embed_model="gemini-embedding-2",
        embed_dim=1536,
        # gemini-embedding-2 takes 8192; the older -001 takes 2048. The
        # per-model table below is what actually guards a run, because the
        # model can be overridden without changing provider.
        max_input_tokens=8192,
        supports_dimensions=True,
        # Pinned, not an alias. `gemini-flash-lite-latest` resolves
        # server-side to whatever Google points it at — the usage report
        # then names a model nobody chose, and a repoint changes the price
        # silently. This is the model that alias was already resolving to
        # ($0.30/M as of 2026-08-27); pinning changes nothing about cost
        # today and removes the moving target. 2.5-flash-lite would be
        # cheaper but returns 404 — "no longer available to new users".
        chat_model="gemini-3.5-flash-lite",
        # Measured Gemini/tiktoken ratio on this corpus: 0.75x (Korean
        # prose), 0.90x (KO/EN mixed), 1.12x (Python source). 1.25 clears
        # the worst case observed with room to spare — deliberately
        # conservative, because over-budget text is dropped without a word.
        tokenizer_skew=1.25,
    ),
    "ollama": ProviderSpec(
        name="ollama",
        base_url="http://localhost:11434/v1",
        key_env="OLLAMA_API_KEY",
        embed_model="qwen3-embedding:0.6b",
        embed_dim=1024,
        # Ollama serves the model with num_ctx=4096 by default and
        # truncates anything past it without an error, same failure mode
        # as Gemini — the ceiling here is the server's window, not the
        # model's 32k.
        max_input_tokens=4096,
        supports_dimensions=False,
        # No chat lane: translation resolves its provider independently
        # and must not land on a server with only an embedding model.
        chat_model="",
        # Qwen3/tiktoken ratio is near 1.0 on code and below 1.0 on
        # Korean prose; 1.25 clears the observed worst case, matching the
        # margin used for Gemini.
        tokenizer_skew=1.25,
        requires_key=False,
        max_batch_tokens=OLLAMA_MAX_BATCH_TOKENS,
    ),
}

# Per-input token ceilings that differ from the provider default. The
# embedding model is overridable (config or env) while the provider is
# not, so the guard has to key off the model actually being called —
# otherwise pinning the older gemini-embedding-001 would silently inherit
# an 8192 budget and lose every tail past 2048 with no error.
MODEL_INPUT_LIMITS: dict[str, int] = {
    "gemini-embedding-001": 2048,
    "gemini-embedding-2": 8192,
    "gemini-embedding-2-preview": 8192,
    "text-embedding-3-small": 8000,
    "text-embedding-3-large": 8000,
    "qwen3-embedding:0.6b": 4096,
}


# USD per million input tokens, per embedding model. Used only to show an
# estimate before a rebuild — a wrong number here misleads, it never
# charges anyone, so unknown models simply produce no estimate.
MODEL_INPUT_USD_PER_MTOK: dict[str, float] = {
    "gemini-embedding-2": 0.20,
    "gemini-embedding-2-preview": 0.20,
    "gemini-embedding-001": 0.15,
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "qwen3-embedding:0.6b": 0.0,
}


def input_price(model: str) -> float | None:
    """USD per million input tokens for ``model``, or None if unknown."""
    return MODEL_INPUT_USD_PER_MTOK.get(model)


def input_limit(spec: "ProviderSpec", model: str) -> int:
    """Per-input token ceiling for ``model``, defaulting to the provider's."""
    return MODEL_INPUT_LIMITS.get(model, spec.max_input_tokens)


# index_meta key holding the vector space an index was written in.
EMBEDDING_FINGERPRINT_KEY = "embedding_fingerprint"

# Indexes built before fingerprints were recorded. Every one of them came
# from the single hard-coded backend, so a missing value is not "unknown"
# — it is precisely this. Treating it as unknown-and-therefore-fine is
# what would let a provider switch quietly compare across vector spaces.
LEGACY_FINGERPRINT = "openai:text-embedding-3-small:1536"


def vector_space_matches(stored: str | None, current: str) -> bool:
    """Whether an index's stored vectors are comparable to ``current``.

    Equal width is not enough: a 1536-wide Gemini vector and a 1536-wide
    OpenAI vector share no axes, so their cosine is noise rather than a
    weaker signal. Callers must drop the vector lane on a mismatch, not
    down-weight it.
    """
    return (stored or LEGACY_FINGERPRINT) == current


DEFAULT_PROVIDER = "openai"

# Env override so a provider can be swapped for an A/B run without editing
# config.toml. Empty or unknown values fall back to the configured one.
PROVIDER_ENV = "HYBRID_SEARCH_PROVIDER"

def resolve(name: str | None) -> ProviderSpec:
    """Provider for ``name``, honouring the env override.

    Unknown names fall back to the default rather than raising: a typo in
    config.toml should degrade to the documented provider, not take the
    whole index offline.
    """
    override = os.environ.get(PROVIDER_ENV, "").strip().lower()
    key = (override or (name or "") or DEFAULT_PROVIDER).strip().lower()
    return PROVIDERS.get(key, PROVIDERS[DEFAULT_PROVIDER])


def load_dotenv_key(key: str) -> str:
    """Read ``key`` from the nearest ``.env.local``, walking up from cwd."""
    current = Path.cwd()
    for _ in range(10):  # max 10 levels up
        env_file = current / ".env.local"
        if env_file.exists():
            try:
                lines = env_file.read_text().splitlines()
            except OSError:
                lines = []
            for line in lines:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ""


def api_key(spec: ProviderSpec) -> str:
    """Key for ``spec`` from the environment, then ``.env.local``."""
    found = os.environ.get(spec.key_env, "") or load_dotenv_key(spec.key_env)
    if not found and not spec.requires_key:
        return "local"
    return found
