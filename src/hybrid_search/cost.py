"""Estimate what a full re-embedding will cost, before it runs.

A forced rebuild re-embeds every chunk in the project. That is the only
operation here that spends real money at scale, and it is invisible until
the bill arrives: one day's work re-ran a 15,800-chunk project three times
while chasing separate fixes and burned about 70M tokens, most of it
avoidable by batching the changes into a single rebuild.

The estimate exists so that decision can be made *before* the spend, not
after. It is deliberately rough — the point is the order of magnitude
("this is a $2 run, batch it with the next change"), not accounting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hybrid_search import providers

logger = logging.getLogger(__name__)

# Bytes of real content sampled to derive a chars-per-token ratio. The
# ratio varies enormously by content — measured on one corpus: 3.7
# chars/token for Python, 1.6 for Korean prose. Assuming a code-like
# ratio under-counted a Korean-heavy corpus by 47%, so it is measured
# rather than assumed.
_SAMPLE_BYTES = 512_000


@dataclass(frozen=True)
class CostEstimate:
    files: int
    chars: int
    tokens: int
    model: str
    usd: float | None  # None when the model has no published price here

    def render(self) -> str:
        # An upper bound: a rebuild reuses vectors for any chunk whose text
        # is unchanged, so the actual spend is usually a small fraction of
        # this. Quoting the ceiling is the useful direction to be wrong in.
        head = (
            f"{self.files:,} files / {self.chars / 1e6:.1f}M chars "
            f"≈ {self.tokens / 1e6:.1f}M tokens"
        )
        if self.usd is None:
            return f"{head} ({self.model}, price unknown)"
        return (
            f"{head} ≈ ${self.usd:.2f} at most ({self.model}; "
            f"unchanged chunks are reused, not re-embedded)"
        )


def _chars_per_token(paths: list[Path]) -> float:
    """Measure the ratio on a sample rather than assuming one."""
    try:
        import tiktoken
    except ImportError:  # pragma: no cover
        return 3.5
    enc = tiktoken.encoding_for_model("text-embedding-3-small")
    chars = tokens = 0
    for path in paths:
        if chars >= _SAMPLE_BYTES:
            break
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if not text:
            continue
        chars += len(text)
        tokens += len(enc.encode(text))
    if tokens <= 0:
        return 3.5
    return chars / tokens


def estimate_rebuild(project_path: Path, config) -> CostEstimate | None:
    """Estimate a full re-embed of ``project_path``. None if unmeasurable."""
    from hybrid_search.index.embedder import Embedder
    from hybrid_search.index.scanner import _build_ignore_spec, _walk_files

    try:
        root = Path(project_path).resolve()
        files = _walk_files(root, _build_ignore_spec(root, config.indexing), config.indexing)
    except Exception:
        logger.debug("cost estimate: scan failed", exc_info=True)
        return None
    if not files:
        return None

    try:
        total_chars = sum(f.stat().st_size for f in files)
    except OSError:
        return None

    # Sample from the middle so a project that leads with generated files
    # does not set the ratio for everything behind them.
    stride = max(1, len(files) // 40)
    ratio = _chars_per_token(files[::stride])
    tokens = int(total_chars / ratio) if ratio > 0 else 0

    model = Embedder(config.embedding)._model
    price = providers.input_price(model)
    usd = tokens / 1e6 * price if price is not None else None
    return CostEstimate(len(files), total_chars, tokens, model, usd)
