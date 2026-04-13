"""Tests for configuration loading — config.py."""

from pathlib import Path

from hybrid_search.config import (
    DEFAULT_DATA_DIR,
    DEFAULT_EXCLUDE_PATTERNS,
    DEFAULT_SUPPORTED_EXTENSIONS,
    MODEL_MAX_TOKENS,
    Config,
    EmbeddingConfig,
    IndexingConfig,
    SearchConfig,
    load_config,
)


class TestDefaultConfig:
    """Default Config values."""

    def test_default_data_dir(self) -> None:
        cfg = Config()
        assert cfg.data_dir == DEFAULT_DATA_DIR

    def test_default_log_level(self) -> None:
        cfg = Config()
        assert cfg.log_level == "info"

    def test_models_dir(self) -> None:
        cfg = Config()
        assert cfg.models_dir == DEFAULT_DATA_DIR / "models"

    def test_projects_dir(self) -> None:
        cfg = Config()
        assert cfg.projects_dir == DEFAULT_DATA_DIR / "projects"

    def test_global_dir(self) -> None:
        cfg = Config()
        assert cfg.global_dir == DEFAULT_DATA_DIR / "global"

    def test_no_projects_by_default(self) -> None:
        cfg = Config()
        assert cfg.projects == ()


class TestEmbeddingConfig:
    """EmbeddingConfig with effective_max_tokens auto-detection."""

    def test_explicit_max_tokens(self) -> None:
        emb = EmbeddingConfig(max_tokens=256)
        assert emb.effective_max_tokens == 256

    def test_auto_detect_e5_small(self) -> None:
        emb = EmbeddingConfig(model="intfloat/multilingual-e5-small")
        assert emb.effective_max_tokens == 512

    def test_auto_detect_qwen3(self) -> None:
        emb = EmbeddingConfig(model="Alibaba-NLP/Qwen3-Embedding-0.6B")
        assert emb.effective_max_tokens == 8192

    def test_unknown_model_defaults_512(self) -> None:
        emb = EmbeddingConfig(model="some/unknown-model")
        assert emb.effective_max_tokens == 512

    def test_empty_model_defaults_512(self) -> None:
        emb = EmbeddingConfig()
        assert emb.effective_max_tokens == 512

    def test_default_backend(self) -> None:
        emb = EmbeddingConfig()
        assert emb.backend == "onnx"

    def test_default_device(self) -> None:
        emb = EmbeddingConfig()
        assert emb.device == "cpu"


class TestSearchConfig:
    """SearchConfig defaults."""

    def test_defaults(self) -> None:
        cfg = SearchConfig()
        assert cfg.default_limit == 10
        assert cfg.rrf_k == 60
        assert cfg.query_classifier is True
        assert cfg.default_bm25_weight == 0.5


class TestIndexingConfig:
    """IndexingConfig defaults."""

    def test_default_exclude_patterns(self) -> None:
        cfg = IndexingConfig()
        assert "node_modules" in cfg.exclude_patterns
        assert ".git" in cfg.exclude_patterns
        assert "*.lock" in cfg.exclude_patterns

    def test_default_supported_extensions(self) -> None:
        cfg = IndexingConfig()
        assert ".py" in cfg.supported_extensions
        assert ".ts" in cfg.supported_extensions
        assert ".md" in cfg.supported_extensions

    def test_max_file_size(self) -> None:
        cfg = IndexingConfig()
        assert cfg.max_file_size_kb == 512


class TestLoadConfig:
    """load_config() from TOML file."""

    def test_missing_file_creates_default(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        cfg = load_config(config_path)
        assert isinstance(cfg, Config)
        assert config_path.exists()

    def test_load_custom_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("""\
[general]
data_dir = "~/.my-search"
log_level = "debug"

[embedding]
model = "intfloat/multilingual-e5-base"
backend = "sentence-transformers"
batch_size = 64
max_tokens = 1024

[search]
default_limit = 20
rrf_k = 30
default_bm25_weight = 0.7

[indexing]
max_file_size_kb = 1024

[[projects]]
name = "my-project"
path = "/home/user/project"
""")
        cfg = load_config(config_path)
        assert cfg.log_level == "debug"
        assert cfg.embedding.model == "intfloat/multilingual-e5-base"
        assert cfg.embedding.backend == "sentence-transformers"
        assert cfg.embedding.batch_size == 64
        assert cfg.embedding.effective_max_tokens == 1024
        assert cfg.search.default_limit == 20
        assert cfg.search.rrf_k == 30
        assert cfg.search.default_bm25_weight == 0.7
        assert cfg.indexing.max_file_size_kb == 1024
        assert len(cfg.projects) == 1
        assert cfg.projects[0].name == "my-project"

    def test_partial_config_uses_defaults(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[general]\nlog_level = \"warning\"\n")
        cfg = load_config(config_path)
        assert cfg.log_level == "warning"
        # Everything else should be defaults
        assert cfg.embedding.backend == "onnx"
        assert cfg.search.rrf_k == 60

    def test_data_dir_expansion(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[general]\ndata_dir = "~/my-search-data"\n')
        cfg = load_config(config_path)
        assert "~" not in str(cfg.data_dir)
        assert str(cfg.data_dir).endswith("my-search-data")

    def test_empty_projects_list(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[general]\n")
        cfg = load_config(config_path)
        assert cfg.projects == ()
