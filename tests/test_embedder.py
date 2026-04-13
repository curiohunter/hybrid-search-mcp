"""Tests for Embedder — index/embedder.py (Ollama GPU backend)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np

from hybrid_search.config import EmbeddingConfig
from hybrid_search.index.embedder import Embedder, OLLAMA_DEFAULT_URL


class TestEmbedderBasics:
    """Basic Embedder construction and empty-input handling."""

    def test_default_config_uses_ollama(self) -> None:
        cfg = EmbeddingConfig()
        assert cfg.ollama_model == "qwen3-embedding:0.6b"

    def test_embed_texts_empty_returns_empty_array(self) -> None:
        cfg = EmbeddingConfig(ollama_model="test-model")
        emb = Embedder(cfg)
        emb._embedding_dim = 1024
        result = emb.embed_texts([])
        assert result.shape == (0, 1024)


class TestOllamaBackend:
    """Ollama backend validation (no live server needed)."""

    def test_missing_ollama_model_raises(self) -> None:
        cfg = EmbeddingConfig(ollama_model="")
        emb = Embedder(cfg)
        try:
            emb._ensure_loaded()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "ollama_model" in str(e)

    def test_ollama_embed_request_builds_correct_payload(self) -> None:
        cfg = EmbeddingConfig(ollama_model="qwen3-embedding:0.6b")
        emb = Embedder(cfg)

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"embeddings": [[0.1, 0.2, 0.3]]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            result = emb._ollama_embed_request(["test text"])
            assert result == [[0.1, 0.2, 0.3]]

            call_args = mock_open.call_args
            req = call_args[0][0]
            assert req.full_url == f"{OLLAMA_DEFAULT_URL}/api/embed"

    def test_ollama_connection_error_gives_clear_message(self) -> None:
        cfg = EmbeddingConfig(ollama_model="qwen3-embedding:0.6b")
        emb = Embedder(cfg)

        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            try:
                emb._ollama_embed_request(["test"])
                assert False, "Should have raised ConnectionError"
            except ConnectionError as e:
                assert "Ollama server not reachable" in str(e)

    def test_embed_all_normalizes(self) -> None:
        cfg = EmbeddingConfig(ollama_model="test-model", batch_size=2)
        emb = Embedder(cfg)
        emb._embedding_dim = 3

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"embeddings": [[3.0, 4.0, 0.0], [0.0, 1.0, 0.0]]}'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = emb._embed_all(["a", "b"])
            # Should be L2 normalized
            norms = np.linalg.norm(result, axis=1)
            np.testing.assert_allclose(norms, [1.0, 1.0], atol=1e-6)


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
