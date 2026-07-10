"""Commit-message indexing (feature genesis, gap B).

Commits become node_type='commit' chunks in the unified store: delta by
hash, changed-file anchors in the embedding text, commit date in
frontmatter for per-commit recency decay, and never wiki material.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from hybrid_search.config import Config, EmbeddingConfig
from hybrid_search.index.commit_indexer import (
    COMMIT_NODE_TYPE,
    CommitIndexer,
    collect_commits,
)
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


class _FakeEmbedder:
    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.embedded = 0

    @property
    def embedding_dim(self) -> int:
        return self._dim

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        self.embedded += len(texts)
        return np.ones((len(texts), self._dim), dtype=np.float32)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init")
    (repo / "auth.py").write_text("def login(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat(auth): add login entry point\n\nWhy: users need to sign in.")
    (repo / "billing.py").write_text("def charge(): pass\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat(billing): charge flow")
    return repo


def _setup(tmp_path: Path) -> tuple[CommitIndexer, Config, Path]:
    repo = _make_repo(tmp_path)
    config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig(batch_size=8))
    registry = ProjectRegistry(config.global_dir)
    indexer = CommitIndexer(config, registry, _FakeEmbedder())
    return indexer, config, repo


class TestCollectCommits:
    def test_parses_subject_body_files_date(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        entries = collect_commits(repo)
        assert len(entries) == 2
        newest, oldest = entries  # git log is newest-first
        assert newest.subject == "feat(billing): charge flow"
        assert newest.files == ("billing.py",)
        assert oldest.subject == "feat(auth): add login entry point"
        assert "users need to sign in" in oldest.body
        assert oldest.files == ("auth.py",)
        assert oldest.date.startswith("20")  # ISO date

    def test_non_git_dir_is_empty(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert collect_commits(plain) == []

    def test_entry_text_and_content(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        oldest = collect_commits(repo)[-1]
        assert "files: auth.py" in oldest.text
        assert oldest.content.startswith("---\ndate: ")
        assert "---" not in oldest.text  # embedding input has no frontmatter


class TestCommitIndexer:
    def test_indexes_commits_into_all_stores(self, tmp_path: Path) -> None:
        indexer, config, repo = _setup(tmp_path)
        result = indexer.index_commits(str(repo))
        assert result.commits_indexed == 2

        pid = project_hash(str(repo.resolve()))
        paths = IndexPaths(get_project_dir(config.projects_dir, pid))
        db = StoreDB(paths.store_db)
        try:
            chunks = [c for c in db.get_chunks_by_project(pid) if c.node_type == COMMIT_NODE_TYPE]
            assert len(chunks) == 2
            assert all(c.content.startswith("---\ndate: ") for c in chunks)
            bm25 = BM25Engine(paths.tantivy_dir, read_only=True)
            hits = [r.chunk_id for r in bm25.search("charge flow", limit=5)]
            assert any(h.startswith("commit:") for h in hits)
            vec = VectorEngine(paths.vectors_dir, 8)
            assert vec.count == 2
        finally:
            db.close()

    def test_delta_run_embeds_only_new_commits(self, tmp_path: Path) -> None:
        indexer, config, repo = _setup(tmp_path)
        indexer.index_commits(str(repo))
        embedder = indexer._embedder
        before = embedder.embedded

        # No new commits → nothing embedded.
        again = indexer.index_commits(str(repo))
        assert again.commits_indexed == 0
        assert embedder.embedded == before

        # One new commit → exactly one embedding.
        (repo / "refund.py").write_text("def refund(): pass\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "feat(refund): withdrawal settlement")
        third = indexer.index_commits(str(repo))
        assert third.commits_indexed == 1
        assert embedder.embedded == before + 1

    def test_rescan_does_not_delete_virtual_commit_file(self, tmp_path: Path) -> None:
        # .git-history/ has no on-disk counterpart — a full project rescan
        # must not flag it as deleted (same contract as .conversations/).
        from hybrid_search.config import IndexingConfig
        from hybrid_search.index.scanner import scan_project

        indexer, config, repo = _setup(tmp_path)
        indexer.index_commits(str(repo))

        pid = project_hash(str(repo.resolve()))
        paths = IndexPaths(get_project_dir(config.projects_dir, pid))
        db = StoreDB(paths.store_db)
        try:
            result = scan_project(repo, pid, db, IndexingConfig())
            assert all(not d.startswith(".git-history/") for d in result.deleted)
        finally:
            db.close()

    def test_non_git_project_is_noop(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig(batch_size=8))
        indexer = CommitIndexer(config, ProjectRegistry(config.global_dir), _FakeEmbedder())
        result = indexer.index_commits(str(plain))
        assert result.commits_indexed == 0
