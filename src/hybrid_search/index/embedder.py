"""Embedding generation — supports ONNX Runtime, sentence-transformers, and Ollama backends.

Supports multilingual models (Qwen3-Embedding-0.6B, multilingual-e5-base, etc.).
Implements batch processing and truncation policy per §7.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

import numpy as np

from hybrid_search.config import EmbeddingConfig

logger = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"


class Embedder:
    """Generates embeddings. Backend auto-selected from config."""

    def __init__(self, config: EmbeddingConfig, models_dir: Path) -> None:
        self._config = config
        self._models_dir = models_dir
        self._model = None  # SentenceTransformer or ONNX session
        self._tokenizer = None  # Only for ONNX backend
        self._embedding_dim: int | None = None
        self._backend = config.backend  # "sentence-transformers" or "onnx"

    @property
    def embedding_dim(self) -> int:
        if self._embedding_dim is None:
            self._ensure_loaded()
        return self._embedding_dim  # type: ignore[return-value]

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """Embed a list of texts. Returns (N, dim) float32 array."""
        self._ensure_loaded()
        if not texts:
            return np.empty((0, self._embedding_dim), dtype=np.float32)

        if self._backend == "sentence-transformers":
            return self._embed_st(texts)
        if self._backend == "ollama":
            return self._embed_ollama_all(texts)
        return self._embed_onnx_all(texts)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query with 'query:' prefix. Returns (dim,) array."""
        prefixed = f"query: {query}"
        result = self.embed_texts([prefixed])
        return result[0]

    # ── sentence-transformers backend ──

    def _ensure_loaded_st(self) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        # Limit PyTorch CPU threads to avoid saturating all cores during indexing.
        # ONNX backend uses onnx_threads config; apply the same limit here.
        max_threads = self._config.onnx_threads or 4
        torch.set_num_threads(max_threads)
        torch.set_num_interop_threads(max(1, max_threads // 2))

        model_name = self._config.model
        device = self._config.device  # "cpu" or "mps"
        logger.info(
            "Loading model via sentence-transformers: %s (device=%s, threads=%d)",
            model_name, device, max_threads,
        )
        self._model = SentenceTransformer(model_name, trust_remote_code=True, device=device)
        dim = self._model.get_embedding_dimension()
        self._embedding_dim = dim
        logger.info("Model loaded: dim=%d, device=%s", dim, device)

    def _embed_st(self, texts: list[str]) -> np.ndarray:
        embeddings = self._model.encode(
            texts,
            batch_size=self._config.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        return np.asarray(embeddings, dtype=np.float32)

    # ── Ollama backend ──

    def _ensure_loaded_ollama(self) -> None:
        ollama_model = self._config.ollama_model
        if not ollama_model:
            raise ValueError(
                "No ollama_model configured. Set [embedding].ollama_model in config.toml "
                "(e.g., 'nomic-embed-text' or 'mxbai-embed-large')"
            )
        logger.info("Testing Ollama embedding model: %s", ollama_model)
        # Probe with a test embedding to get dimension
        test_result = self._ollama_embed_request(["dimension probe"])
        self._embedding_dim = len(test_result[0])
        logger.info("Ollama model ready: %s, dim=%d", ollama_model, self._embedding_dim)

    def _ollama_embed_request(self, texts: list[str]) -> list[list[float]]:
        """Call Ollama POST /api/embed endpoint."""
        url = f"{OLLAMA_DEFAULT_URL}/api/embed"
        payload = json.dumps({
            "model": self._config.ollama_model,
            "input": texts,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Ollama server not reachable at {OLLAMA_DEFAULT_URL}. "
                f"Is Ollama running? Error: {e}"
            ) from e

        if "embeddings" not in data:
            raise ValueError(f"Unexpected Ollama response: {list(data.keys())}")
        return data["embeddings"]

    def _embed_ollama_all(self, texts: list[str]) -> np.ndarray:
        """Embed texts via Ollama in batches."""
        all_embeddings: list[np.ndarray] = []
        batch_size = self._config.batch_size
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            raw = self._ollama_embed_request(batch)
            all_embeddings.append(np.array(raw, dtype=np.float32))
        embeddings = np.vstack(all_embeddings)
        # L2 normalize
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        return (embeddings / norms).astype(np.float32)

    # ── ONNX backend ──

    def _ensure_loaded_onnx(self) -> None:
        import os
        import onnxruntime as ort
        from transformers import AutoTokenizer

        max_threads = self._config.onnx_threads
        # Env-level thread caps — catches numpy/MKL/OpenMP that ONNX settings miss
        for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
            os.environ[var] = str(max_threads)

        model_path = self._resolve_model_path()
        q_tag = " (INT8 quantized)" if "quantized" in model_path.name else ""
        logger.info("Loading ONNX model from %s%s", model_path, q_tag)

        providers = ["CPUExecutionProvider"]
        if self._config.device == "mps":
            if "CoreMLExecutionProvider" in ort.get_available_providers():
                providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.intra_op_num_threads = max_threads
        sess_options.inter_op_num_threads = max(1, max_threads // 2)

        self._model = ort.InferenceSession(
            str(model_path), sess_options=sess_options, providers=providers,
        )

        model_name = self._config.model
        if self._config.model_path:
            tokenizer_path = Path(self._config.model_path).parent
            self._tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path))
        else:
            self._tokenizer = AutoTokenizer.from_pretrained(model_name)

        test_emb = self._embed_onnx_batch(["test"])
        self._embedding_dim = test_emb.shape[1]
        logger.info("ONNX model loaded: dim=%d", self._embedding_dim)

    def _embed_onnx_all(self, texts: list[str]) -> np.ndarray:
        all_embeddings: list[np.ndarray] = []
        batch_size = self._config.batch_size
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            all_embeddings.append(self._embed_onnx_batch(batch))
        return np.vstack(all_embeddings)

    # ── common ──

    def _ensure_loaded(self) -> None:
        if self._model is not None or (self._backend == "ollama" and self._embedding_dim is not None):
            return
        if self._backend == "sentence-transformers":
            self._ensure_loaded_st()
        elif self._backend == "ollama":
            self._ensure_loaded_ollama()
        else:
            self._ensure_loaded_onnx()

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

        # Prefer quantized model (INT8, ~3x faster, 4x smaller)
        onnx_filename = "model_quantized.onnx" if self._config.quantized else "model.onnx"
        onnx_path = model_dir / onnx_filename

        # Fallback: if quantized requested but only full model exists, use full
        if not onnx_path.exists() and self._config.quantized:
            full_path = model_dir / "model.onnx"
            if full_path.exists():
                logger.info("Quantized model not found, falling back to full model")
                onnx_path = full_path

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
        from huggingface_hub import hf_hub_download

        onnx_filename = onnx_path.name  # "model_quantized.onnx" or "model.onnx"

        # For quantized models, try downloading from Xenova's repo first (pre-quantized)
        if self._config.quantized and onnx_filename == "model_quantized.onnx":
            xenova_repo = f"Xenova/{self._config.model.split('/')[-1]}"
            try:
                logger.info("Downloading quantized model from %s...", xenova_repo)
                model_dir.mkdir(parents=True, exist_ok=True)
                downloaded = hf_hub_download(
                    repo_id=xenova_repo,
                    filename="onnx/model_quantized.onnx",
                    local_dir=str(model_dir),
                )
                # Move from onnx/ subdirectory to model_dir root
                src = Path(downloaded)
                dst = model_dir / "model_quantized.onnx"
                if src != dst:
                    src.rename(dst)
                    # Clean up empty onnx/ dir
                    try:
                        src.parent.rmdir()
                    except OSError:
                        pass

                # Download tokenizer from original repo
                self._download_tokenizer(model_dir)
                logger.info("INT8 quantized model ready: %s (%.1f MB)", dst, dst.stat().st_size / 1e6)
                return dst
            except Exception as e:
                logger.warning("Xenova quantized model not available (%s), falling back to full model", e)
                onnx_filename = "model.onnx"
                onnx_path = model_dir / onnx_filename

        # Standard download from original repo
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

        logger.info("Downloading model %s (revision: %s)...", self._config.model, self._config.model_revision)
        model_dir.mkdir(parents=True, exist_ok=True)

        downloaded = hf_hub_download(
            repo_id=self._config.model,
            filename=onnx_filename,
            revision=self._config.model_revision,
            local_dir=str(model_dir),
        )

        self._download_tokenizer(model_dir)

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

    def _download_tokenizer(self, model_dir: Path) -> None:
        """Download tokenizer files from the original HuggingFace model repo."""
        from huggingface_hub import hf_hub_download

        for fname in ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt"]:
            try:
                hf_hub_download(
                    repo_id=self._config.model,
                    filename=fname,
                    revision=self._config.model_revision or None,
                    local_dir=str(model_dir),
                )
            except Exception:
                pass  # Not all models have all tokenizer files

    def _embed_onnx_batch(self, texts: list[str]) -> np.ndarray:
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
        input_names = {inp.name for inp in self._model.get_inputs()}
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        outputs = self._model.run(None, feeds)

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
