"""Project registry — global SQLite DB for managing multiple projects."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REGISTRY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    last_indexed_at TEXT,
    file_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    index_version INTEGER DEFAULT 1
);
"""


@dataclass
class ProjectInfo:
    id: str
    name: str
    path: str
    last_indexed_at: str | None = None
    file_count: int = 0
    chunk_count: int = 0
    index_version: int = 1


def project_hash(project_path: str) -> str:
    """Deterministic project ID from absolute path."""
    return hashlib.sha256(project_path.encode()).hexdigest()[:16]


class ProjectRegistry:
    """Global registry at ~/.hybrid-search/global/project_registry.db."""

    def __init__(self, global_dir: Path) -> None:
        global_dir.mkdir(parents=True, exist_ok=True)
        db_path = global_dir / "project_registry.db"
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(REGISTRY_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def register(self, name: str, path: str) -> ProjectInfo:
        """Register or update a project. Returns ProjectInfo."""
        pid = project_hash(path)
        self._conn.execute(
            """INSERT INTO projects (id, name, path)
               VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name, path = excluded.path""",
            (pid, name, path),
        )
        self._conn.commit()
        return self.get(pid)  # type: ignore[return-value]

    def get(self, project_id: str) -> ProjectInfo | None:
        cur = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        return self._row_to_info(row) if row else None

    def get_by_name(self, name: str) -> ProjectInfo | None:
        cur = self._conn.execute("SELECT * FROM projects WHERE name = ?", (name,))
        row = cur.fetchone()
        return self._row_to_info(row) if row else None

    def get_by_path(self, path: str) -> ProjectInfo | None:
        cur = self._conn.execute("SELECT * FROM projects WHERE path = ?", (path,))
        row = cur.fetchone()
        return self._row_to_info(row) if row else None

    def list_all(self) -> list[ProjectInfo]:
        cur = self._conn.execute("SELECT * FROM projects ORDER BY name")
        return [self._row_to_info(row) for row in cur.fetchall()]

    def update_stats(
        self,
        project_id: str,
        file_count: int,
        chunk_count: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE projects
               SET file_count = ?, chunk_count = ?, last_indexed_at = ?
               WHERE id = ?""",
            (file_count, chunk_count, now, project_id),
        )
        self._conn.commit()

    def remove(self, project_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def _row_to_info(self, row: sqlite3.Row) -> ProjectInfo:
        return ProjectInfo(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            last_indexed_at=row["last_indexed_at"],
            file_count=row["file_count"],
            chunk_count=row["chunk_count"],
            index_version=row["index_version"],
        )
