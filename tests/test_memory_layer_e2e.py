"""End-to-end proof that the Memory Layer loop closes.

Scenario (the killer-product demo as code):
    1. A project is indexed normally.
    2. A user searches — the MCP tool writes the exchange to
       ``.hybrid-search/qa/.../*.md``.
    3. The project is reindexed. The qa file is picked up as a
       ``qa_log`` chunk.
    4. A *later* search surfaces that qa_log chunk among the
       top results — the first question has literally become
       searchable context for the next one.

This is the "search quality improves from usage" loop. Unit tests
cover the boost math in isolation (``test_memory_boost.py``); this
file proves the loop actually closes end-to-end when every layer
is wired together.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from hybrid_search.config import Config, EmbeddingConfig, IndexingConfig
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.memory import qa_log
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator


class _DetEmbedder:
    """Deterministic embedder that gives each text a unit vector whose
    value depends on a simple token hash — lets the vector engine
    discriminate similar queries without needing OpenAI."""

    @property
    def embedding_dim(self) -> int:
        return 16

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.embedding_dim, dtype=np.float32)
        for tok in (text or "").lower().split():
            idx = sum(ord(c) for c in tok) % self.embedding_dim
            v[idx] += 1.0
        n = np.linalg.norm(v)
        if n > 0:
            v /= n
        return v

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def _mk_project(tmp_path: Path) -> tuple[Path, Config]:
    repo = tmp_path / "repo"
    repo.mkdir()
    # A tiny real-looking codebase so BM25 has something to chew on.
    (repo / "app.py").write_text(
        "def sign_in(user):\n"
        "    '''Handle user login.'''\n"
        "    return True\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# MyApp\n\nAuthentication flows live in `app.py`.\n",
        encoding="utf-8",
    )
    config = Config(
        data_dir=tmp_path / "data",
        embedding=EmbeddingConfig(batch_size=8),
        # Default-on now, but pin explicitly so the test doesn't depend
        # on class defaults if someone flips them.
        indexing=IndexingConfig(index_qa_logs=True),
    )
    return repo, config


def test_qa_log_becomes_searchable_after_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The killer-product loop: ask → log → reindex → future search
    returns the past exchange as a first-class result."""

    # 1. Build a project + index it.
    repo, config = _mk_project(tmp_path)
    registry = ProjectRegistry(config.global_dir)
    embedder = _DetEmbedder()
    pipeline = IndexingPipeline(config, registry, embedder)
    pipeline.index_project(str(repo))

    # 2. Search — trigger qa_log persistence synchronously.
    # The MCP tool uses async writes; we call the lower-level record()
    # path with async_write=False so the write completes before we
    # reindex.
    monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
    first_response = orch.hybrid_search(
        query="how does sign_in work",
        cwd=str(repo),
        limit=5,
    )
    written = qa_log.record(
        query="how does sign_in work",
        response=first_response,
        cwd=str(repo),
        project_infos=registry.list_all(),
        async_write=False,
    )
    assert written is not None and written.exists()

    # 3. Reindex — the qa file should now be picked up as a qa_log chunk.
    pipeline.index_project(str(repo))

    # 4. A later search. Use a query whose tokens overlap the stored
    # question ("sign_in") but phrased as explicit recall so the
    # memory-intent boost amplifies the match.
    second_response = orch.hybrid_search(
        query="지난번에 sign_in 관련 뭐 물어봤지",
        cwd=str(repo),
        limit=10,
    )
    node_types = [r.node_type for r in second_response.results]
    paths = [r.file_path for r in second_response.results]

    # The qa file's relative path starts with .hybrid-search/qa/
    qa_hit_paths = [
        p for p in paths if p.startswith(".hybrid-search/qa/")
    ]

    assert "qa_log" in node_types, (
        f"qa_log chunk was not in top-10. node_types={node_types}, "
        f"paths={paths}"
    )
    assert qa_hit_paths, (
        f"no qa file path in results. paths={paths}"
    )


def test_qa_log_skipped_when_scanner_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-out path: user disables memory indexing → qa files don't
    participate in search even after being written."""

    repo, _ = _mk_project(tmp_path)
    # Opt out at the indexing level.
    config = Config(
        data_dir=tmp_path / "data",
        embedding=EmbeddingConfig(batch_size=8),
        indexing=IndexingConfig(index_qa_logs=False),
    )
    registry = ProjectRegistry(config.global_dir)
    embedder = _DetEmbedder()
    pipeline = IndexingPipeline(config, registry, embedder)
    pipeline.index_project(str(repo))

    monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
    response = orch.hybrid_search(query="sign_in", cwd=str(repo), limit=5)
    qa_log.record(
        query="sign_in flow",
        response=response,
        cwd=str(repo),
        project_infos=registry.list_all(),
        async_write=False,
    )

    pipeline.index_project(str(repo))

    response2 = orch.hybrid_search(
        query="지난번에 sign_in 관련", cwd=str(repo), limit=10,
    )
    for r in response2.results:
        assert r.node_type != "qa_log", (
            "qa_log surfaced despite index_qa_logs=False"
        )
