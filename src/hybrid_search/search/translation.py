"""KO→EN query translation for the cross-language memory lane (ADV3 fix).

The ripgrep holdout ADV3 case: a Korean probe over English qa memories
retrieves nothing — BM25 has no lexical bridge and the KO question ↔ EN
answer cosine sits under the retrieval depth. The fix is query-side: when
a query is Hangul-dominant, retrieve the memory lane a second time with
an English translation and merge. Query-side means no index rebuild and
no behavior change for English queries.

Design constraints (spec P0-2):
- Same key and failure domain as the embedder (OpenAI, raw urllib — the
  project deliberately has no SDK dependency).
- Fail open: any error or timeout returns None and the caller falls back
  to the single-lane behavior, which is exactly the pre-fix state.
- Translations are cached on disk keyed by query hash — repeat recall
  questions (the common case for memory queries) pay the API once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
# Small, fast instruction model. Override via env when needed — this is a
# one-sentence translation, not a reasoning task.
DEFAULT_TRANSLATION_MODEL = "gpt-4o-mini"
_MODEL_ENV = "HYBRID_SEARCH_TRANSLATION_MODEL"
# Hard wall for the first (uncached) call. A missed translation degrades
# to the pre-fix behavior, so the walltime cost of waiting longer is
# worse than the recall cost of skipping.
_TIMEOUT_S = 2.5

_SYSTEM_PROMPT = (
    "Translate the user's Korean developer question into natural English. "
    "Keep code identifiers, file names, and technical terms exactly as "
    "written. Reply with the translation only."
)

_HANGUL_DOMINANCE = 0.3

# Kill switch. "0" disables the cross-language lane entirely — the test
# suite sets this (conftest.py) so no test can accidentally hit the
# network, and users can opt out without config surgery.
_TOGGLE_ENV = "HYBRID_SEARCH_TRANSLATION"


def is_enabled() -> bool:
    return os.environ.get(_TOGGLE_ENV, "1") != "0"


def is_korean_dominant(query: str) -> bool:
    """True when Hangul carries the query's content.

    Ratio over *letters* only — digits, punctuation, and whitespace say
    nothing about language. Identifiers count as Latin letters, so a
    Korean sentence quoting one symbol still qualifies.
    """
    hangul = sum(1 for c in query if "가" <= c <= "힣")
    latin = sum(1 for c in query if c.isalpha() and not ("가" <= c <= "힣"))
    total = hangul + latin
    if total == 0:
        return False
    return hangul / total >= _HANGUL_DOMINANCE


class QueryTranslator:
    """Cached KO→EN translation with fail-open semantics.

    ``request_fn(payload_dict) -> str`` is injectable for tests; the
    default posts to the OpenAI chat completions API with the same
    key-resolution the embedder uses (env, then .env.local).
    """

    def __init__(
        self,
        cache_path: Path,
        model: str | None = None,
        timeout_s: float = _TIMEOUT_S,
        request_fn=None,
    ) -> None:
        self._cache_path = cache_path
        self._model = model or os.environ.get(_MODEL_ENV) or DEFAULT_TRANSLATION_MODEL
        self._timeout_s = timeout_s
        self._request_fn = request_fn or self._openai_request
        self._cache: dict[str, str] | None = None

    # -- cache ------------------------------------------------------------

    @staticmethod
    def _key(query: str) -> str:
        return hashlib.sha1(query.strip().encode("utf-8")).hexdigest()

    def _load_cache(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        cache: dict[str, str] = {}
        try:
            with open(self._cache_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                        cache[row["k"]] = row["v"]
                    except (json.JSONDecodeError, KeyError):
                        continue  # one corrupt line must not kill the cache
        except OSError:
            pass
        self._cache = cache
        return cache

    def _append_cache(self, key: str, value: str) -> None:
        assert self._cache is not None
        self._cache[key] = value
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"k": key, "v": value}, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("translation cache write failed", exc_info=True)

    # -- translation ------------------------------------------------------

    def translate(self, query: str) -> str | None:
        """English translation of ``query``, or None (fail open)."""
        key = self._key(query)
        cache = self._load_cache()
        if key in cache:
            return cache[key] or None
        try:
            translated = (self._request_fn(query) or "").strip()
        except Exception:
            logger.debug("query translation failed", exc_info=True)
            return None
        if not translated:
            return None
        self._append_cache(key, translated)
        return translated

    def _openai_request(self, query: str) -> str:
        from hybrid_search.index.embedder import _load_dotenv_key

        api_key = os.environ.get("OPENAI_API_KEY", "") or _load_dotenv_key(
            "OPENAI_API_KEY"
        )
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found")
        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            OPENAI_CHAT_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
