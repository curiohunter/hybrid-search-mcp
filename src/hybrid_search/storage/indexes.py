"""Index file management — handles per-project Tantivy + USearch index paths."""

from __future__ import annotations

import shutil
from pathlib import Path


class IndexPaths:
    """Manages index file paths for a single project."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        project_dir.mkdir(parents=True, exist_ok=True)

    @property
    def store_db(self) -> Path:
        return self._project_dir / "store.db"

    @property
    def tantivy_dir(self) -> Path:
        return self._project_dir / "tantivy"

    @property
    def vectors_dir(self) -> Path:
        return self._project_dir / "vectors"

    @property
    def lock_file(self) -> Path:
        return self._project_dir / "store.db.lock"

    def ensure_dirs(self) -> None:
        """Create all index directories."""
        self.tantivy_dir.mkdir(parents=True, exist_ok=True)
        self.vectors_dir.mkdir(parents=True, exist_ok=True)

    def delete_all(self) -> None:
        """Remove all index data for this project."""
        if self._project_dir.exists():
            shutil.rmtree(self._project_dir)


def get_project_dir(projects_dir: Path, project_id: str) -> Path:
    """Get the directory for a specific project's index data."""
    return projects_dir / project_id
