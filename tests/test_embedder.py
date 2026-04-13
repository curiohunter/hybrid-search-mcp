"""Tests for Embedder — index/embedder.py (OpenAI API backend)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np

from hybrid_search.config import EmbeddingConfig
from hybrid_search.index.embedder import Embedder, OPENAI_EMBED_URL


class TestEmbedderBasics:
    """Basic Embedder construction and empty-input handling."""

    def test_default_config_uses_openai(self) -> None:
        cfg = EmbeddingConfig()
        assert cfg.openai_model == "text-embedding-3-small"

    def test_embedding_dim_is_1536(self) -> None:
        cfg = EmbeddingConfig()
        emb = Embedder(cfg)
        assert emb.embedding_dim == 1536

    def test_embed_texts_empty_returns_empty_array(self) -> None:
        cfg = EmbeddingConfig()
        emb = Embedder(cfg)
        result = emb.embed_texts([])
        assert result.shape == (0, 1536)


class TestOpenAIBackend:
    """OpenAI backend validation (no live API needed)."""

    def test_missing_api_key_raises(self) -> None:
        cfg = EmbeddingConfig()
        emb = Embedder(cfg)
        emb._api_key = None
        with patch.dict("os.environ", {}, clear=True):
            with patch("hybrid_search.index.embedder._load_dotenv_key", return_value=""):
                try:
                    emb._get_api_key()
                    assert False, "Should have raised ValueError"
                except ValueError as e:
                    assert "OPENAI_API_KEY" in str(e)

    def test_embed_request_calls_correct_url(self) -> None:
        cfg = EmbeddingConfig()
        emb = Embedder(cfg)
        emb._api_key = "sk-test"

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            result = emb._openai_embed_request(["test text"])
            assert result == [[0.1, 0.2, 0.3]]

            req = mock_open.call_args[0][0]
            assert req.full_url == OPENAI_EMBED_URL

    def test_embed_all_normalizes(self) -> None:
        cfg = EmbeddingConfig(batch_size=2)
        emb = Embedder(cfg)
        emb._api_key = "sk-test"
        emb._embedding_dim = 3

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"data": [{"embedding": [3.0, 4.0, 0.0], "index": 0}, {"embedding": [0.0, 1.0, 0.0], "index": 1}]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = emb._embed_all(["a", "b"])
            norms = np.linalg.norm(result, axis=1)
            np.testing.assert_allclose(norms, [1.0, 1.0], atol=1e-6)

    def test_api_error_gives_clear_message(self) -> None:
        cfg = EmbeddingConfig()
        emb = Embedder(cfg)
        emb._api_key = "sk-test"

        import urllib.error
        error = urllib.error.HTTPError(
            OPENAI_EMBED_URL, 429, "Too Many Requests", {}, MagicMock(read=lambda: b'{"error":"rate limit"}')
        )
        with patch("urllib.request.urlopen", side_effect=error):
            try:
                emb._openai_embed_request(["test"])
                assert False, "Should have raised ConnectionError"
            except ConnectionError as e:
                assert "OpenAI API error 429" in str(e)


class TestHotReloadableConfig:
    """_HotReloadableConfig tests."""

    def test_no_reload_when_unchanged(self, tmp_path: Path) -> None:
        from hybrid_search.server import _HotReloadableConfig
        from hybrid_search.config import Config

        config_path = tmp_path / "config.toml"
        config_path.write_text("[general]\nlog_level = 'info'\n")
        hrc = _HotReloadableConfig(Config(), config_path)
        assert hrc.check_reload() is False

    def test_reload_when_mtime_changes(self, tmp_path: Path) -> None:
        import os
        import time
        from hybrid_search.server import _HotReloadableConfig
        from hybrid_search.config import Config

        config_path = tmp_path / "config.toml"
        config_path.write_text("[general]\nlog_level = 'info'\n")
        hrc = _HotReloadableConfig(Config(), config_path)

        time.sleep(0.05)
        config_path.write_text("[general]\nlog_level = 'debug'\n")
        os.utime(config_path, (time.time() + 1, time.time() + 1))

        assert hrc.check_reload() is True
        assert hrc.config.log_level == "debug"

    def test_reload_survives_invalid_toml(self, tmp_path: Path) -> None:
        import os
        import time
        from hybrid_search.server import _HotReloadableConfig
        from hybrid_search.config import Config

        config_path = tmp_path / "config.toml"
        config_path.write_text("[general]\nlog_level = 'info'\n")
        hrc = _HotReloadableConfig(Config(), config_path)

        time.sleep(0.05)
        config_path.write_text("invalid {{{{ toml content")
        os.utime(config_path, (time.time() + 1, time.time() + 1))

        assert hrc.check_reload() is False
        assert hrc.config.log_level == "info"
