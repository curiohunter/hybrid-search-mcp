from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hybrid_search.config import Config, EmbeddingConfig
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import get_project_dir


class _FakeEmbedder:
    def __init__(self, dim: int = 8, fail: bool = False) -> None:
        self._dim = dim
        self._fail = fail

    @property
    def embedding_dim(self) -> int:
        return self._dim

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if self._fail:
            raise RuntimeError("embedding failed")
        return np.ones((len(texts), self._dim), dtype=np.float32)


def _make_pipeline(tmp_path: Path, fail: bool = False) -> tuple[IndexingPipeline, Config]:
    config = Config(
        data_dir=tmp_path / "data",
        embedding=EmbeddingConfig(batch_size=8),
    )
    registry = ProjectRegistry(config.global_dir)
    pipeline = IndexingPipeline(config, registry, _FakeEmbedder(fail=fail))
    return pipeline, config


def _read_counts(config: Config, project_root: Path) -> tuple[int, int]:
    project_dir = get_project_dir(config.projects_dir, project_hash(str(project_root.resolve())))
    db = StoreDB(project_dir / "store.db")
    try:
        pid = project_hash(str(project_root.resolve()))
        return db.get_file_count(pid), db.get_chunk_count(pid)
    finally:
        db.close()


def test_force_rebuild_failure_preserves_existing_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def alpha():\n    return 1\n", encoding="utf-8")

    pipeline, config = _make_pipeline(tmp_path)
    pipeline.index_project(str(repo))
    original_counts = _read_counts(config, repo)

    source.write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )

    failing_pipeline, _ = _make_pipeline(tmp_path, fail=True)
    with pytest.raises(RuntimeError, match="embedding failed"):
        failing_pipeline.index_project(str(repo), force=True)

    assert _read_counts(config, repo) == original_counts

    project_dir = get_project_dir(config.projects_dir, project_hash(str(repo.resolve())))
    assert project_dir.exists()
    assert not project_dir.parent.joinpath(f"{project_dir.name}.rebuilding").exists()
    assert not project_dir.parent.joinpath(f"{project_dir.name}.backup").exists()


def test_recover_backup_directory_before_indexing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")

    pipeline, config = _make_pipeline(tmp_path)
    pipeline.index_project(str(repo))

    project_dir = get_project_dir(config.projects_dir, project_hash(str(repo.resolve())))
    backup_dir = project_dir.parent / f"{project_dir.name}.backup"
    project_dir.rename(backup_dir)

    result = pipeline.index_project(str(repo))

    assert result.chunks_total > 0
    assert project_dir.exists()
    assert not backup_dir.exists()
