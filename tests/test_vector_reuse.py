"""A rebuild must not re-embed text it already has a vector for.

Most forced rebuilds are triggered by *our* code changing — chunking,
module discovery, naming — none of which alters the text being embedded.
Three rebuilds of one project in a single day re-embedded the same ~16k
chunks and bought nothing: about ₩9,000 of identical work.

Vectors are keyed by a hash of the exact text that produced them, so a
hit is safe by construction. Measured on this repo after the change:
2,920 of 2,936 embeddings served from the previous index, rebuild time
3 minutes to 11.6 seconds.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np

from hybrid_search.index.pipeline import IndexingPipeline, _VectorReuse


def _reuse(mapping: dict[str, np.ndarray]) -> _VectorReuse:
    index = {_VectorReuse._key(t): t for t in mapping}
    vectors = MagicMock()
    vectors.get_vector.side_effect = lambda cid: mapping.get(cid)
    return _VectorReuse(MagicMock(), vectors, index)


def _pipeline(reuse, embedder):
    pipe = IndexingPipeline.__new__(IndexingPipeline)
    pipe._embedder = embedder
    pipe._reuse = reuse
    return pipe


def _embedder(dim=3):
    e = MagicMock()
    e.embed_texts.side_effect = lambda ts: np.array(
        [[float(len(t))] * dim for t in ts], dtype=np.float32
    )
    return e


class TestReuse:
    def test_unchanged_text_is_not_re_embedded(self):
        vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        emb = _embedder()
        pipe = _pipeline(_reuse({"alpha": vec}), emb)
        out = pipe._embed_with_reuse(["alpha"])
        emb.embed_texts.assert_not_called()
        assert np.array_equal(out[0], vec)

    def test_new_text_is_embedded(self):
        emb = _embedder()
        pipe = _pipeline(_reuse({"alpha": np.zeros(3, dtype=np.float32)}), emb)
        pipe._embed_with_reuse(["beta"])
        emb.embed_texts.assert_called_once_with(["beta"])

    def test_order_is_preserved_across_a_mixed_batch(self):
        """Reused and freshly embedded vectors must land back in the
        caller's original positions — chunks are matched to rows by index."""
        a = np.array([9.0, 9.0, 9.0], dtype=np.float32)
        emb = _embedder()
        pipe = _pipeline(_reuse({"alpha": a}), emb)
        out = pipe._embed_with_reuse(["new1", "alpha", "new22"])
        assert np.array_equal(out[1], a)
        assert out[0][0] == 4.0   # len("new1")
        assert out[2][0] == 5.0   # len("new22")

    def test_only_the_misses_are_sent_to_the_api(self):
        emb = _embedder()
        pipe = _pipeline(_reuse({"alpha": np.zeros(3, dtype=np.float32)}), emb)
        pipe._embed_with_reuse(["alpha", "beta", "alpha"])
        assert emb.embed_texts.call_args[0][0] == ["beta"]

    def test_hits_and_misses_are_counted(self):
        r = _reuse({"alpha": np.zeros(3, dtype=np.float32)})
        pipe = _pipeline(r, _embedder())
        pipe._embed_with_reuse(["alpha", "beta"])
        assert (r.hits, r.misses) == (1, 1)

    def test_without_a_previous_index_everything_is_embedded(self):
        emb = _embedder()
        pipe = _pipeline(None, emb)
        pipe._embed_with_reuse(["a", "b"])
        emb.embed_texts.assert_called_once_with(["a", "b"])

    def test_a_vector_missing_from_the_index_counts_as_a_miss(self):
        """The hash table can outlive the vector it points at."""
        r = _reuse({"alpha": None})
        emb = _embedder()
        pipe = _pipeline(r, emb)
        pipe._embed_with_reuse(["alpha"])
        assert r.misses == 1
        emb.embed_texts.assert_called_once_with(["alpha"])
