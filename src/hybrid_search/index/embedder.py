"""Embedding generation using ONNX Runtime with HuggingFace tokenizer.

Supports multilingual models (gte-multilingual-base, bge-m3, etc.).
Implements batch processing and truncation policy per §7.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import numpy as np

from hybrid_search.config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Lazy-loaded globals
_session = None
_tokenizer = None
_embedding_dim: int | None = None


class Embedder:
    """Generates embeddings using ONNX Runtime."""

    def __init__(self, config: EmbeddingConfig, models_dir: Path) -> None:
        self._config = config
        self._models_dir = models_dir
        self._session = None
        self._tokenizer = None
        self._embedding_dim: int | None = None

    @property
    def embedding_dim(self) -> int:
        """Return embedding dimension (initializes model if needed)."""
        if self._embedding_dim is None:
            self._ensure_loaded()
        return self._embedding_dim  # type: ignore[return-value]

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Embed a list of texts. Returns (N, dim) float32 array."""
        self._ensure_loaded()
        if not texts:
            return np.empty((0, self._embedding_dim), dtype=np.float32)

        all_embeddings: list[np.ndarray] = []
        batch_size = self._config.batch_size

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            embeddings = self._embed_batch(batch)
            all_embeddings.append(embeddings)

        return np.vstack(all_embeddings)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query with 'query:' prefix. Returns (dim,) array."""
        prefixed = f"query: {query}"
        result = self.embed_texts([prefixed])
        return result[0]

    def _ensure_loaded(self) -> None:
        """Lazy-load ONNX model and tokenizer."""
        if self._session is not None:
            return

        import onnxruntime as ort
        from transformers import AutoTokenizer

        model_path = self._resolve_model_path()
        logger.info("Loading embedding model from %s", model_path)

        # Configure ONNX Runtime
        providers = ["CPUExecutionProvider"]
        if self._config.device == "mps":
            # CoreML on Apple Silicon
            if "CoreMLExecutionProvider" in ort.get_available_providers():
                providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = 4

        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=providers,
        )

        # Load tokenizer
        model_name = self._config.model
        if self._config.model_path:
            # Local model — tokenizer might be alongside
            tokenizer_path = Path(self._config.model_path).parent
            self._tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path))
        else:
            self._tokenizer = AutoTokenizer.from_pretrained(model_name)

        # Detect embedding dimension from a test inference
        test_emb = self._embed_batch(["test"])
        self._embedding_dim = test_emb.shape[1]
        logger.info("Embedding model loaded: dim=%d", self._embedding_dim)

    def _resolve_model_path(self) -> Path:
        """Find or download the ONNX model file."""
        # Option 1: explicit local path
        if self._config.model_path:
            p = Path(self._config.model_path).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"Model not found at {p}")
            return p

        # Option 2: cached download
        if not self._config.model:
            raise ValueError(
                "No embedding model configured. Set [embedding].model in config.toml"
            )

        model_dir = self._models_dir / self._config.model.replace("/", "_")
        onnx_path = model_dir / "model.onnx"

        if onnx_path.exists():
            # Verify checksum if configured
            if self._config.model_sha256:
                actual_hash = _file_sha256(onnx_path)
                if actual_hash != self._config.model_sha256:
                    raise ValueError(
                        f"Model checksum mismatch: expected {self._config.model_sha256}, "
                        f"got {actual_hash}. Delete {onnx_path} and re-download."
                    )
            return onnx_path

        # Download from HuggingFace
        return self._download_model(model_dir, onnx_path)

    def _download_model(self, model_dir: Path, onnx_path: Path) -> Path:
        """Download ONNX model from HuggingFace Hub."""
        if not self._config.model_revision:
            raise ValueError(
                "model_revision is required for download. "
                "Set [embedding].model_revision in config.toml"
            )
        if not self._config.model_sha256:
            raise ValueError(
                "model_sha256 is required for download. "
                "Set [embedding].model_sha256 in config.toml"
            )

        from huggingface_hub import hf_hub_download

        logger.info("Downloading model %s (revision: %s)...", self._config.model, self._config.model_revision)
        model_dir.mkdir(parents=True, exist_ok=True)

        downloaded = hf_hub_download(
            repo_id=self._config.model,
            filename="model.onnx",
            revision=self._config.model_revision,
            local_dir=str(model_dir),
        )

        # Also download tokenizer files
        for fname in ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"]:
            try:
                hf_hub_download(
                    repo_id=self._config.model,
                    filename=fname,
                    revision=self._config.model_revision,
                    local_dir=str(model_dir),
                )
            except Exception:
                pass  # Not all models have all tokenizer files

        # Verify checksum
        actual_hash = _file_sha256(Path(downloaded))
        if actual_hash != self._config.model_sha256:
            Path(downloaded).unlink(missing_ok=True)
            raise ValueError(
                f"Downloaded model checksum mismatch: expected {self._config.model_sha256}, "
                f"got {actual_hash}"
            )

        logger.info("Model downloaded and verified: %s", onnx_path)
        return onnx_path

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        """Embed a batch of texts using ONNX Runtime."""
        max_tokens = self._config.effective_max_tokens

        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=max_tokens,
            return_tensors="np",
        )

        input_ids = encoded["input_ids"].astype(np.int64)
        attention_mask = encoded["attention_mask"].astype(np.int64)

        feeds = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }

        # Some models also expect token_type_ids
        input_names = {inp.name for inp in self._session.get_inputs()}
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        outputs = self._session.run(None, feeds)

        # Usually last_hidden_state is the first output
        hidden_states = outputs[0]

        # Mean pooling with attention mask
        mask_expanded = np.expand_dims(attention_mask, -1).astype(np.float32)
        sum_embeddings = np.sum(hidden_states * mask_expanded, axis=1)
        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        embeddings = sum_embeddings / sum_mask

        # L2 normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        embeddings = embeddings / norms

        return embeddings.astype(np.float32)


def _file_sha256(path: Path) -> str:
    """Compute SHA256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
