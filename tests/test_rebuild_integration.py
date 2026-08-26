"""Round-3 integration: revalidation projection across a REAL store rebuild.

The round-2 unit tests proved the projection is a pure function of
(HEAD, qa corpus); this file proves the property holds through the
actual pipeline machinery: index → qa written with result-carried
evidence → reindex → flag; then ``index_project(force=True)`` swaps the
store atomically (fresh qa_revalidation table) and the next pass
restores exactly the flag that still holds.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from hybrid_search.config import Config, EmbeddingConfig, IndexingConfig
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.memory import qa_log
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


class _DetEmbedder:
    @property
    def embedding_dim(self) -> int:
        return 16

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.embedding_dim, dtype=np.float32)
        for tok in (text or "").lower().split():
            v[sum(ord(c) for c in tok) % self.embedding_dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def embed_texts(self, texts):
        if not texts:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


def _git(root: Path, *argv: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", *argv], cwd=root, capture_output=True, text=True,
        env=env, check=True,
    ).stdout.strip()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(".hybrid-search/\n")
    (repo / "src").mkdir()
    (repo / "src/auth.py").write_text(
        "def verify_token():\n    return 'v1'\n", encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init", date="2026-07-01T00:00:00+00:00")

    config = Config(
        data_dir=tmp_path / "data",
        embedding=EmbeddingConfig(batch_size=8),
        indexing=IndexingConfig(index_qa_logs=True),
    )
    registry = ProjectRegistry(config.global_dir)
    pipeline = IndexingPipeline(config, registry, _DetEmbedder())
    pipeline.index_project(str(repo))
    return repo, config, registry, pipeline


def _store(config: Config, registry: ProjectRegistry, name: str) -> StoreDB:
    pinfo = registry.get_by_name(name)
    idx = IndexPaths(get_project_dir(config.projects_dir, pinfo.id))
    return StoreDB(idx.store_db)


class _FakeResult:
    def __init__(self, path: str, ihash: str):
        self.chunk_id = "c1"
        self.file_path = path
        self.project = None
        self.name = "n"
        self.qualified_name = "q"
        self.node_type = "function"
        self.start_line = 1
        self.end_line = 2
        self.snippet = "s"
        self.indexed_file_hash = ihash


class _FakeResponse:
    def __init__(self, results):
        self.results = results
        self.query_type = "KOREAN_NL"
        self.effective_bm25_weight = 0.15
        self.query_time_ms = 1.0
        self.total_chunks_searched = 10


def test_flags_survive_atomic_force_rebuild(env) -> None:
    repo, config, registry, pipeline = env
    name = repo.name
    pinfo = registry.get_by_name(name)

    # 1. qa written with the hash the index actually served for v1.
    db = _store(config, registry, name)
    try:
        file_rec = db.get_file_by_path(pinfo.id, "src/auth.py")
        assert file_rec is not None and file_rec.file_hash
        v1_hash = file_rec.file_hash
    finally:
        db.close()
    written = qa_log.record(
        query="auth 어디서 처리해?",
        response=_FakeResponse([_FakeResult("src/auth.py", v1_hash)]),
        cwd=str(repo),
        async_write=False,
    )
    assert written is not None

    # 2. reindex picks the qa up as a qa_log chunk.
    pipeline.index_project(str(repo))

    # 3. the anchored file changes at HEAD.
    (repo / "src/auth.py").write_text(
        "def verify_token():\n    return 'v2'\n", encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v2", date="2026-07-10T00:00:00+00:00")

    from hybrid_search.cli import _run_qa_revalidation

    def _flags() -> dict:
        db = _store(config, registry, name)
        try:
            qa_ids = [
                c.id for c in db.get_chunks_by_node_type(pinfo.id, "qa_log")
            ]
            assert qa_ids, "qa chunk must be indexed"
            return db.get_qa_revalidations(qa_ids)
        finally:
            db.close()

    _run_qa_revalidation(config, registry, name, repo)
    before = _flags()
    assert len(before) == 1
    assert next(iter(before.values()))[1] == "src/auth.py"

    # 4. FORCE REBUILD — the store (and qa_revalidation table) is
    # replaced wholesale by the atomic rebuild path.
    pipeline.index_project(str(repo), force=True)
    assert _flags() == {}, "fresh store starts with no flags"

    # 5. the next pass restores exactly the flag that still holds.
    _run_qa_revalidation(config, registry, name, repo)
    after = _flags()
    assert len(after) == 1
    assert next(iter(after.values())) == next(iter(before.values()))
