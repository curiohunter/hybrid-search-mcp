"""Configuration loading from ~/.hybrid-search/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DATA_DIR = Path.home() / ".hybrid-search"

DEFAULT_EXCLUDE_PATTERNS = [
    "node_modules",
    ".git",
    "__pycache__",
    ".next",
    "dist",
    "build",
    ".venv",
    "*.lock",
]

DEFAULT_SUPPORTED_EXTENSIONS = [
    ".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go",
    ".rb", ".java", ".c", ".cpp", ".h", ".hpp",
    ".swift", ".kt", ".sql", ".css", ".scss",
    ".md", ".json", ".yaml", ".yml", ".toml",
]

# Known model token limits for auto-detection
MODEL_MAX_TOKENS: dict[str, int] = {
    "multilingual-e5-small": 512,
    "multilingual-e5-base": 512,
    "gte-multilingual-base": 8192,
    "bge-m3": 8192,
    "Qwen3-Embedding-0.6B": 8192,
    "Qwen3-Embedding": 8192,
}


@dataclass(frozen=True)
class EmbeddingConfig:
    ollama_model: str = "qwen3-embedding:0.6b"
    batch_size: int = 16
    # Legacy fields — kept for config.toml backwards compat, not used by Ollama backend
    model: str = ""
    model_revision: str = ""
    model_sha256: str = ""
    model_path: str = ""
    backend: str = "ollama"
    max_tokens: int = 0
    device: str = ""
    onnx_threads: int = 0
    quantized: bool = False


@dataclass(frozen=True)
class SearchConfig:
    default_limit: int = 10
    rrf_k: int = 60
    query_classifier: bool = True
    default_bm25_weight: float = 0.5


@dataclass(frozen=True)
class IndexingConfig:
    exclude_patterns: tuple[str, ...] = tuple(DEFAULT_EXCLUDE_PATTERNS)
    max_file_size_kb: int = 512
    supported_extensions: tuple[str, ...] = tuple(DEFAULT_SUPPORTED_EXTENSIONS)


@dataclass(frozen=True)
class ProjectEntry:
    name: str
    path: str


@dataclass(frozen=True)
class WikiConfig:
    max_pages_per_project: int = 100
    eviction_policy: str = "lru"  # "lru" only for now


@dataclass(frozen=True)
class Config:
    data_dir: Path = DEFAULT_DATA_DIR
    log_level: str = "info"
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    wiki: WikiConfig = field(default_factory=WikiConfig)
    projects: tuple[ProjectEntry, ...] = ()

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def global_dir(self) -> Path:
        return self.data_dir / "global"


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from TOML file. Creates default if missing."""
    if config_path is None:
        config_path = DEFAULT_DATA_DIR / "config.toml"

    if not config_path.exists():
        return _create_default_config(config_path)

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    general = raw.get("general", {})
    data_dir = Path(general.get("data_dir", str(DEFAULT_DATA_DIR))).expanduser()

    emb_raw = raw.get("embedding", {})
    embedding = EmbeddingConfig(
        model=emb_raw.get("model", ""),
        model_revision=emb_raw.get("model_revision", ""),
        model_sha256=emb_raw.get("model_sha256", ""),
        model_path=emb_raw.get("model_path", ""),
        backend=emb_raw.get("backend", "ollama"),
        ollama_model=emb_raw.get("ollama_model", ""),
        batch_size=emb_raw.get("batch_size", 32),
        max_tokens=emb_raw.get("max_tokens", 0),
        device=emb_raw.get("device", "cpu"),
        onnx_threads=emb_raw.get("onnx_threads", 6),
        quantized=emb_raw.get("quantized", True),
    )

    search_raw = raw.get("search", {})
    search = SearchConfig(
        default_limit=search_raw.get("default_limit", 10),
        rrf_k=search_raw.get("rrf_k", 60),
        query_classifier=search_raw.get("query_classifier", True),
        default_bm25_weight=search_raw.get("default_bm25_weight", 0.5),
    )

    idx_raw = raw.get("indexing", {})
    indexing = IndexingConfig(
        exclude_patterns=tuple(idx_raw.get("exclude_patterns", DEFAULT_EXCLUDE_PATTERNS)),
        max_file_size_kb=idx_raw.get("max_file_size_kb", 512),
        supported_extensions=tuple(
            idx_raw.get("supported_extensions", DEFAULT_SUPPORTED_EXTENSIONS)
        ),
    )

    wiki_raw = raw.get("wiki", {})
    wiki = WikiConfig(
        max_pages_per_project=wiki_raw.get("max_pages_per_project", 100),
        eviction_policy=wiki_raw.get("eviction_policy", "lru"),
    )

    projects = tuple(
        ProjectEntry(name=p["name"], path=p["path"])
        for p in raw.get("projects", [])
        if "name" in p and "path" in p
    )

    return Config(
        data_dir=data_dir,
        log_level=general.get("log_level", "info"),
        embedding=embedding,
        search=search,
        indexing=indexing,
        wiki=wiki,
        projects=projects,
    )


def _create_default_config(config_path: Path) -> Config:
    """Create default config file and return default Config."""
    config_path.parent.mkdir(parents=True, exist_ok=True)

    default_toml = """\
[general]
data_dir = "~/.hybrid-search"
log_level = "info"

[embedding]
ollama_model = "qwen3-embedding:0.6b"  # Ollama model (GPU accelerated)
batch_size = 16

[search]
default_limit = 10
rrf_k = 60
query_classifier = true
default_bm25_weight = 0.5

[indexing]
exclude_patterns = [
    "node_modules", ".git", "__pycache__", ".next",
    "dist", "build", ".venv", "*.lock"
]
max_file_size_kb = 512
supported_extensions = [
    ".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go",
    ".rb", ".java", ".c", ".cpp", ".h", ".hpp",
    ".swift", ".kt", ".sql", ".css", ".scss",
    ".md", ".json", ".yaml", ".yml", ".toml"
]

# [[projects]]
# name = "my-project"
# path = "/path/to/project"
"""
    config_path.write_text(default_toml)
    return Config()
