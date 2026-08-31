"""Provider selection for the embedding and translation lanes.

The OpenAI account outage made the single-provider hard-coding a
liability: endpoint, key env, model, output width, and per-input token
ceiling were all baked into the embedder. These tests pin the seams that
replaced them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from hybrid_search import providers
from hybrid_search.config import EmbeddingConfig
from hybrid_search.index.embedder import Embedder
from hybrid_search.search.translation import QueryTranslator


class TestResolve:
    def test_default_is_openai(self):
        assert providers.resolve(None).name == "openai"

    def test_unknown_name_falls_back_rather_than_raising(self):
        """A typo in config.toml must degrade, not take the index offline."""
        assert providers.resolve("gemnii").name == "openai"

    def test_env_overrides_config(self):
        with patch.dict("os.environ", {providers.PROVIDER_ENV: "gemini"}):
            assert providers.resolve("openai").name == "gemini"


class TestEmbedderWiring:
    def test_gemini_endpoint_model_and_width(self):
        emb = Embedder(EmbeddingConfig(backend="gemini"))
        assert emb._embed_url.endswith("/v1beta/openai/embeddings")
        assert emb._model == "gemini-embedding-2"
        # 1536 is an MRL prefix of Gemini's native 3072 — chosen so the
        # on-disk vector width is unchanged.
        assert emb.embedding_dim == 1536

    def test_openai_default_model_does_not_leak_to_gemini(self):
        """openai_model carries a non-empty default; it must not be sent
        to a provider that has never heard of it."""
        cfg = EmbeddingConfig(backend="gemini")
        assert cfg.openai_model == "text-embedding-3-small"
        assert Embedder(cfg)._model == "gemini-embedding-2"

    def test_token_budget_discounts_tokenizer_skew(self):
        """Budgets are counted with tiktoken whatever the provider, so a
        provider whose tokenizer runs hotter gets a smaller budget."""
        assert Embedder(EmbeddingConfig(backend="openai"))._token_budget() == 8000
        assert Embedder(EmbeddingConfig(backend="gemini"))._token_budget() == 6553

    @staticmethod
    def _capture_payload(emb: Embedder) -> dict:
        resp = MagicMock()
        resp.read.return_value = b'{"data": [{"embedding": [0.1], "index": 0}]}'
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        emb._api_key = "k"
        with patch("urllib.request.urlopen", return_value=resp) as opened:
            emb._openai_embed_request(["hello"])
        return json.loads(opened.call_args[0][0].data.decode())

    def test_gemini_request_asks_for_the_index_width(self):
        payload = self._capture_payload(Embedder(EmbeddingConfig(backend="gemini")))
        assert payload["dimensions"] == 1536

    def test_openai_request_omits_dimensions(self):
        """text-embedding-3-small is fixed-width; sending the field would
        be noise at best."""
        payload = self._capture_payload(Embedder(EmbeddingConfig(backend="openai")))
        assert "dimensions" not in payload


class TestModelAwareInputLimit:
    """The model is overridable while the provider is not, so the ceiling
    has to follow the model — gemini-embedding-001 takes 2048 where
    gemini-embedding-2 takes 8192."""

    def test_pinning_the_older_model_lowers_the_budget(self):
        with patch.dict(
            "os.environ", {"HYBRID_SEARCH_EMBED_MODEL": "gemini-embedding-001"}
        ):
            emb = Embedder(EmbeddingConfig(backend="gemini"))
        assert emb._model == "gemini-embedding-001"
        assert emb._token_budget() == 1638

    def test_unknown_model_falls_back_to_provider_ceiling(self):
        with patch.dict(
            "os.environ", {"HYBRID_SEARCH_EMBED_MODEL": "gemini-embedding-99"}
        ):
            emb = Embedder(EmbeddingConfig(backend="gemini"))
        assert emb._token_budget() == 6553


class TestTruncationIsCounted:
    def test_overlong_text_is_cut_to_budget_and_counted(self):
        """Gemini truncates past its ceiling silently — a 4k-token input to
        gemini-embedding-001 returns HTTP 200 with a full-length vector.
        The tail is gone with no error, so the embedder has to notice."""
        with patch.dict(
            "os.environ", {"HYBRID_SEARCH_EMBED_MODEL": "gemini-embedding-001"}
        ):
            emb = Embedder(EmbeddingConfig(backend="gemini"))
        long_text = "word " * 4000
        out = emb._truncate(long_text)
        assert len(out) < len(long_text)
        assert emb._truncated == 1

    def test_text_within_budget_is_untouched(self):
        emb = Embedder(EmbeddingConfig(backend="gemini"))
        assert emb._truncate("short enough") == "short enough"
        assert emb._truncated == 0


class TestTranslationFollowsProvider:
    def test_the_chat_model_is_pinned_not_an_alias(self):
        """A `-latest` alias moves under you: usage reports name a model
        nobody chose and a repoint changes the price without notice."""
        tr = QueryTranslator(Path("/tmp/unused.jsonl"), provider="gemini")
        assert "latest" not in tr._model

    def test_gemini_uses_gemini_chat_endpoint_and_model(self):
        tr = QueryTranslator(Path("/tmp/unused.jsonl"), provider="gemini")
        assert tr._spec.base_url.endswith("/v1beta/openai")
        assert tr._model == "gemini-3.5-flash-lite"
        assert tr._spec.key_env == "GEMINI_API_KEY"

    def test_openai_keeps_its_own_model(self):
        tr = QueryTranslator(Path("/tmp/unused.jsonl"), provider="openai")
        assert tr._model == "gpt-4o-mini"
        assert tr._spec.key_env == "OPENAI_API_KEY"

    def test_explicit_model_overrides_provider_default(self):
        tr = QueryTranslator(
            Path("/tmp/unused.jsonl"), provider="gemini", model="gemini-3.5-flash"
        )
        assert tr._model == "gemini-3.5-flash"


class TestVectorSpaceFingerprint:
    """Equal width is not equal meaning. A provider switch must drop the
    vector lane rather than compare cosines across incompatible spaces."""

    def test_missing_fingerprint_reads_as_the_old_hard_coded_backend(self):
        """Every index predating this field came from one backend, so an
        absent value is known, not unknown — trusting it blindly is the
        bug this guards."""
        assert providers.vector_space_matches(None, providers.LEGACY_FINGERPRINT)
        assert not providers.vector_space_matches(
            None, "gemini:gemini-embedding-2:1536"
        )

    def test_same_width_different_model_does_not_match(self):
        assert not providers.vector_space_matches(
            "openai:text-embedding-3-small:1536", "gemini:gemini-embedding-2:1536"
        )

    def test_identical_space_matches(self):
        fp = "gemini:gemini-embedding-2:1536"
        assert providers.vector_space_matches(fp, fp)

    def test_embedder_fingerprint_tracks_provider_model_and_width(self):
        assert (
            Embedder(EmbeddingConfig(backend="gemini")).fingerprint
            == "gemini:gemini-embedding-2:1536"
        )
        assert (
            Embedder(EmbeddingConfig(backend="openai")).fingerprint
            == providers.LEGACY_FINGERPRINT
        )


class TestOnlyFullRebuildsClaimTheVectorSpace:
    """An incremental pass re-embeds the changed files and nothing else.
    Stamping the fingerprint there would relabel a half-converted index as
    clean and switch off the guard that was protecting it — which is
    exactly what happened to a live project mid-migration.

    The one exception is an index with no chunks at all: a first `index`
    run is a full rebuild in everything but the flag, and has no prior
    vectors it could mislabel."""

    @staticmethod
    def _pipeline(fingerprint="gemini:gemini-embedding-2:1536"):
        from hybrid_search.index.pipeline import IndexingPipeline
        pipe = IndexingPipeline.__new__(IndexingPipeline)
        pipe._embedder = MagicMock()
        pipe._embedder.fingerprint = fingerprint
        return pipe

    @staticmethod
    def _db(*, chunks: int, stored: str | None = None):
        db = MagicMock()
        db.get_chunk_count.return_value = chunks
        db.get_meta.return_value = stored
        return db

    def test_full_rebuild_stamps(self):
        db = self._db(chunks=900)
        self._pipeline()._record_vector_space(db, full_rebuild=True, project_id="p")
        db.set_meta.assert_called_once_with(
            providers.EMBEDDING_FINGERPRINT_KEY, "gemini:gemini-embedding-2:1536"
        )

    def test_first_index_stamps_even_unforced(self):
        """A new project has no vectors to relabel. Leaving the marker unset
        made the search side read the absence as LEGACY_FINGERPRINT and drop
        the vector lane on an index built entirely by the current model."""
        db = self._db(chunks=0)
        self._pipeline()._record_vector_space(db, full_rebuild=False, project_id="p")
        db.set_meta.assert_called_once_with(
            providers.EMBEDDING_FINGERPRINT_KEY, "gemini:gemini-embedding-2:1536"
        )

    def test_incremental_over_a_legacy_index_does_not_stamp(self):
        """Missing marker + existing chunks is a pre-fingerprint index, not a
        new one. This is the case the emptiness test must not swallow."""
        db = self._db(chunks=900, stored=None)
        self._pipeline()._record_vector_space(db, full_rebuild=False, project_id="p")
        db.set_meta.assert_not_called()

    def test_incremental_in_the_same_space_does_not_stamp_either(self):
        """Nothing to record — the marker already says this."""
        db = self._db(chunks=900, stored="gemini:gemini-embedding-2:1536")
        self._pipeline()._record_vector_space(db, full_rebuild=False, project_id="p")
        db.set_meta.assert_not_called()

    def test_a_stubbed_embedder_is_ignored(self):
        db = self._db(chunks=0)
        pipe = self._pipeline(fingerprint=MagicMock())
        pipe._record_vector_space(db, full_rebuild=True, project_id="p")
        db.set_meta.assert_not_called()
