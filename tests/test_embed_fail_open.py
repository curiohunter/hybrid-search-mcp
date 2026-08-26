"""Regression: a dead embedding provider must not kill the whole search.

Before this, ``hybrid_search`` called ``embed_query`` unguarded, so a revoked
API key (or any network fault) raised out of the search entry point and took
the BM25 lane down with it — plain symbol lookup died on a failed HTTP call
to a service it never needed. The vector lane is an enhancement; losing it
degrades results, it does not end them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hybrid_search.project import ProjectInfo
from hybrid_search.search.orchestrator import SearchOrchestrator


def _make_orchestrator(*, embed_fails: bool) -> SearchOrchestrator:
    config = MagicMock()
    config.search.rrf_k = 60
    config.search.reranking.enabled = False
    config.search.reranking.max_candidates = 20
    config.search.reranking.lexical = False
    config.search.reranking.lexical_weight = 0.6
    config.router.confidence.as_dict.return_value = {
        "strong_score": 0.0,      # everything clears the strong bar, so any
        "strong_gap": 0.0,        # "weak" in the assertions is caused by the
        "weak_score": 0.0,        # degraded path and nothing else.
        "cosine_anchor": 0.0,
    }

    pinfo = ProjectInfo(
        id="proj1", name="test", path="/tmp/test",
        last_indexed_at=None, file_count=1, chunk_count=2,
    )
    registry = MagicMock()
    registry.list_all.return_value = [pinfo]
    registry.get_by_name.return_value = pinfo

    embedder = MagicMock()
    if embed_fails:
        embedder.embed_query.side_effect = ConnectionError(
            "OpenAI API error 401: account_deactivated"
        )
    else:
        embedder.embed_query.return_value = [0.1, 0.2]

    orch = SearchOrchestrator(config, registry, embedder)
    # BM25 lane still returns hits; the vector lane returns nothing when the
    # query could not be embedded (mirrors the real guard's behaviour).
    orch._search_single = MagicMock(
        return_value=(["a", "b"], [] if embed_fails else ["b", "a"], 2, [], {},
                      {} if embed_fails else {"b": 0.7, "a": 0.6})
    )
    # Enrichment hits a real StoreDB; the lanes under test are upstream of it.
    orch._enrich_results = MagicMock(return_value=[])
    orch._module_results_for_query = MagicMock(return_value=([], []))
    orch._first_corpus_absent_term = MagicMock(return_value=None)
    return orch


class TestEmbeddingOutageFailsOpen:
    def test_search_does_not_raise(self):
        """The outage must surface as a degraded response, not an exception."""
        orch = _make_orchestrator(embed_fails=True)
        resp = orch.hybrid_search("compute_file_hash")
        assert resp is not None

    def test_bm25_lane_still_runs(self):
        """BM25 needs no embedding — it must be queried anyway."""
        orch = _make_orchestrator(embed_fails=True)
        orch.hybrid_search("compute_file_hash")
        assert orch._search_single.called

    def test_none_vector_is_passed_down(self):
        """Downstream lanes get None and skip the vector search themselves."""
        orch = _make_orchestrator(embed_fails=True)
        orch.hybrid_search("compute_file_hash")
        # _search_single(pinfo, query, query_vector, depth, ...)
        assert orch._search_single.call_args[0][2] is None

    def test_confidence_is_weak_and_hint_says_degraded(self):
        """Two-lane thresholds cannot classify one-lane scores — never claim
        more than weak, and say why."""
        orch = _make_orchestrator(embed_fails=True)
        resp = orch.hybrid_search("compute_file_hash")
        assert resp.confidence == "weak"
        assert resp.fallback_hint is not None
        assert "DEGRADED" in resp.fallback_hint

    def test_healthy_path_is_untouched(self):
        """The guard must not demote a normal two-lane search."""
        orch = _make_orchestrator(embed_fails=False)
        resp = orch.hybrid_search("compute_file_hash")
        assert orch._search_single.call_args[0][2] is not None
        assert "DEGRADED" not in (resp.fallback_hint or "")


class TestSearchSingleArity:
    def test_missing_store_db_returns_six_tuple(self, tmp_path):
        """The un-indexed early return unpacked into six names but yielded
        five — any caller hitting it raised ValueError."""
        config = MagicMock()
        config.projects_dir = tmp_path
        embedder = MagicMock()
        embedder.embedding_dim = 2
        orch = SearchOrchestrator(config, MagicMock(), embedder)
        pinfo = ProjectInfo(
            id="never-indexed", name="x", path=str(tmp_path),
            last_indexed_at=None, file_count=0, chunk_count=0,
        )
        result = orch._search_single(pinfo, "q", None, 10, None, None)
        assert len(result) == 6
        bm25_ids, vector_ids, total, skipped, authority, vector_scores = result
        assert (bm25_ids, vector_ids, total, skipped) == ([], [], 0, [])
        assert authority == {} and vector_scores == {}
