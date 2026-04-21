"""Configuration loading from ~/.hybrid-search/config.toml."""

from __future__ import annotations

import os
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
    openai_model: str = "text-embedding-3-small"
    batch_size: int = 100  # OpenAI supports up to 2048 inputs per request
    # Legacy fields — kept for config.toml backwards compat
    ollama_model: str = ""
    model: str = ""
    model_revision: str = ""
    model_sha256: str = ""
    model_path: str = ""
    backend: str = "openai"
    max_tokens: int = 0
    device: str = ""
    onnx_threads: int = 0
    quantized: bool = False


@dataclass(frozen=True)
class RerankingConfig:
    enabled: bool = False
    max_candidates: int = 20


@dataclass(frozen=True)
class SearchConfig:
    default_limit: int = 10
    rrf_k: int = 60
    query_classifier: bool = True
    default_bm25_weight: float = 0.5
    # M1.v2 boost ceiling. L6 n=60 (2026-04-21): α=0.3 is best for
    # self-contained projects; α=0.5 is stronger on external-weighted
    # workloads (+0.094 vs +0.065 NDCG). Override per-project via config.
    authority_alpha: float = 0.3
    reranking: RerankingConfig = field(default_factory=RerankingConfig)


@dataclass(frozen=True)
class IndexingConfig:
    exclude_patterns: tuple[str, ...] = tuple(DEFAULT_EXCLUDE_PATTERNS)
    max_file_size_kb: int = 512
    supported_extensions: tuple[str, ...] = tuple(DEFAULT_SUPPORTED_EXTENSIONS)
    # Sprint 3: opt-in self-reference for the Memory Layer. When True, the
    # scanner walks into ``.hybrid-search/qa/`` and the resulting chunks are
    # tagged ``node_type="qa_log"`` so hybrid_search can surface past queries
    # alongside code. Default off — qa logs may contain user data that
    # shouldn't leak into general-purpose search results without consent.
    index_qa_logs: bool = False


@dataclass(frozen=True)
class ProjectEntry:
    name: str
    path: str


@dataclass(frozen=True)
class SynthesisConfig:
    enabled: bool = False


@dataclass(frozen=True)
class WikiConfig:
    max_pages_per_project: int = 100
    eviction_policy: str = "lru"  # "lru" only for now
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)


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
        openai_model=emb_raw.get("openai_model", "text-embedding-3-small"),
        batch_size=emb_raw.get("batch_size", 100),
        ollama_model=emb_raw.get("ollama_model", ""),
        model=emb_raw.get("model", ""),
        model_revision=emb_raw.get("model_revision", ""),
        model_sha256=emb_raw.get("model_sha256", ""),
        model_path=emb_raw.get("model_path", ""),
        backend=emb_raw.get("backend", "openai"),
        max_tokens=emb_raw.get("max_tokens", 0),
        device=emb_raw.get("device", ""),
        onnx_threads=emb_raw.get("onnx_threads", 0),
        quantized=emb_raw.get("quantized", False),
    )

    search_raw = raw.get("search", {})
    rerank_raw = search_raw.get("reranking", {})
    reranking = RerankingConfig(
        enabled=rerank_raw.get("enabled", False),
        max_candidates=rerank_raw.get("max_candidates", 20),
    )
    search = SearchConfig(
        default_limit=search_raw.get("default_limit", 10),
        rrf_k=search_raw.get("rrf_k", 60),
        query_classifier=search_raw.get("query_classifier", True),
        default_bm25_weight=search_raw.get("default_bm25_weight", 0.5),
        authority_alpha=float(search_raw.get("authority_alpha", 0.3)),
        reranking=reranking,
    )

    idx_raw = raw.get("indexing", {})
    env_index_qa = os.environ.get("HYBRID_SEARCH_INDEX_QA", "").strip().lower()
    indexing = IndexingConfig(
        exclude_patterns=tuple(idx_raw.get("exclude_patterns", DEFAULT_EXCLUDE_PATTERNS)),
        max_file_size_kb=idx_raw.get("max_file_size_kb", 512),
        supported_extensions=tuple(
            idx_raw.get("supported_extensions", DEFAULT_SUPPORTED_EXTENSIONS)
        ),
        # Env var wins over config when set, so users can toggle per-shell.
        index_qa_logs=(
            env_index_qa in {"1", "true", "yes", "on"}
            if env_index_qa
            else bool(idx_raw.get("index_qa_logs", False))
        ),
    )

    wiki_raw = raw.get("wiki", {})
    synth_raw = wiki_raw.get("synthesis", {})
    synthesis = SynthesisConfig(
        enabled=synth_raw.get("enabled", False),
    )
    wiki = WikiConfig(
        max_pages_per_project=wiki_raw.get("max_pages_per_project", 100),
        eviction_policy=wiki_raw.get("eviction_policy", "lru"),
        synthesis=synthesis,
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
backend = "openai"
openai_model = "text-embedding-3-small"
batch_size = 100

[search]
default_limit = 10
rrf_k = 60
query_classifier = true
default_bm25_weight = 0.5
# authority_alpha: call-graph boost ceiling (M1.v2). α=0.3 = default.
# Try 0.5 for external-weighted workloads (+0.094 vs +0.065 NDCG at L6 n=15).
authority_alpha = 0.3

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
