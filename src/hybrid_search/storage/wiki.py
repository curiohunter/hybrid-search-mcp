"""Wiki page storage with dependency tracking and staleness detection."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def normalize_query(query: str) -> str:
    """Normalize query for deterministic lookup: lowercase, collapse whitespace, sort words."""
    words = query.strip().lower().split()
    sorted_words = sorted(words)
    result = " ".join(sorted_words)
    return result[:200]


def _page_id(project_id: str, query_key: str) -> str:
    """Generate deterministic page ID from project + query."""
    raw = f"{project_id}:{query_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WikiPage:
    id: str
    project_id: str
    query_key: str
    title: str
    content: str
    tags: list[str]
    created_at: str
    updated_at: str
    accessed_at: str
    access_count: int
    version: int
    stale: bool | None = None
    changed_files: list[str] | None = None


class WikiStore:
    """Wiki page CRUD + dependency-based staleness detection.

    Wraps a StoreDB's connection. Caller is responsible for
    opening/closing the StoreDB.
    """

    def __init__(self, conn: sqlite3.Connection, max_pages: int = 100) -> None:
        self._conn = conn
        self._max_pages = max_pages

    def compile_page(
        self,
        project_id: str,
        query: str,
        title: str,
        content: str,
        tags: list[str] | None,
        file_dependencies: list[dict],
    ) -> dict:
        """Store a wiki page with file dependency snapshots.

        file_dependencies: [{"file_id": str, "file_hash": str, "chunk_ids": [str]}]
        Returns: {"page_id", "query_key", "evicted_count"}
        """
        query_key = normalize_query(query)
        page_id = _page_id(project_id, query_key)
        now = _now_iso()
        tags_json = json.dumps(tags or [], ensure_ascii=False)

        existing = self._conn.execute(
            "SELECT id FROM wiki_pages WHERE id = ?", (page_id,)
        ).fetchone()

        if existing:
            self._conn.execute(
                """UPDATE wiki_pages
                   SET title = ?, content = ?, tags = ?, updated_at = ?,
                       accessed_at = ?, version = version + 1
                   WHERE id = ?""",
                (title, content, tags_json, now, now, page_id),
            )
            self._conn.execute(
                "DELETE FROM wiki_dependencies WHERE wiki_page_id = ?", (page_id,)
            )
        else:
            self._conn.execute(
                """INSERT INTO wiki_pages
                   (id, project_id, query_key, title, content, tags,
                    created_at, updated_at, accessed_at, access_count, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)""",
                (page_id, project_id, query_key, title, content, tags_json,
                 now, now, now),
            )

        for dep in file_dependencies:
            chunk_ids_json = json.dumps(dep.get("chunk_ids", []))
            self._conn.execute(
                """INSERT OR REPLACE INTO wiki_dependencies
                   (wiki_page_id, file_id, file_hash_at_compile, chunk_ids)
                   VALUES (?, ?, ?, ?)""",
                (page_id, dep["file_id"], dep["file_hash"], chunk_ids_json),
            )

        evicted = self._evict_lru(project_id)

        return {"page_id": page_id, "query_key": query_key, "evicted_count": evicted}

    def lookup_page(
        self, project_id: str, query: str | None = None, tag: str | None = None
    ) -> WikiPage | None:
        """Find a wiki page by normalized query or tag. Updates access tracking."""
        if query:
            query_key = normalize_query(query)
            row = self._conn.execute(
                "SELECT * FROM wiki_pages WHERE project_id = ? AND query_key = ?",
                (project_id, query_key),
            ).fetchone()
        elif tag:
            row = self._conn.execute(
                """SELECT * FROM wiki_pages
                   WHERE project_id = ? AND tags LIKE ?
                   ORDER BY accessed_at DESC LIMIT 1""",
                (project_id, f'%"{tag}"%'),
            ).fetchone()
        else:
            return None

        if row is None:
            return None

        now = _now_iso()
        self._conn.execute(
            "UPDATE wiki_pages SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
            (now, row["id"]),
        )

        staleness = self._check_page_staleness(row["id"])

        return WikiPage(
            id=row["id"],
            project_id=row["project_id"],
            query_key=row["query_key"],
            title=row["title"],
            content=row["content"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            accessed_at=now,
            access_count=row["access_count"] + 1,
            version=row["version"],
            stale=staleness["stale"],
            changed_files=staleness["changed_files"],
        )

    def check_staleness(
        self, project_id: str, page_id: str | None = None
    ) -> list[dict]:
        """Check staleness for one page or all pages in a project."""
        if page_id:
            rows = self._conn.execute(
                "SELECT id, title FROM wiki_pages WHERE id = ?", (page_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, title FROM wiki_pages WHERE project_id = ?",
                (project_id,),
            ).fetchall()

        results = []
        for row in rows:
            staleness = self._check_page_staleness(row["id"])
            results.append({
                "page_id": row["id"],
                "title": row["title"],
                **staleness,
            })
        return results

    def refresh_page(
        self,
        page_id: str,
        content: str,
        file_dependencies: list[dict] | None = None,
    ) -> dict | None:
        """Update page content and re-snapshot file hashes."""
        row = self._conn.execute(
            "SELECT * FROM wiki_pages WHERE id = ?", (page_id,)
        ).fetchone()
        if row is None:
            return None

        now = _now_iso()
        self._conn.execute(
            """UPDATE wiki_pages
               SET content = ?, updated_at = ?, accessed_at = ?,
                   version = version + 1
               WHERE id = ?""",
            (content, now, now, page_id),
        )

        if file_dependencies is not None:
            self._conn.execute(
                "DELETE FROM wiki_dependencies WHERE wiki_page_id = ?", (page_id,)
            )
            for dep in file_dependencies:
                chunk_ids_json = json.dumps(dep.get("chunk_ids", []))
                self._conn.execute(
                    """INSERT OR REPLACE INTO wiki_dependencies
                       (wiki_page_id, file_id, file_hash_at_compile, chunk_ids)
                       VALUES (?, ?, ?, ?)""",
                    (page_id, dep["file_id"], dep["file_hash"], chunk_ids_json),
                )
        else:
            # Re-snapshot current file hashes for existing dependencies
            deps = self._conn.execute(
                "SELECT file_id FROM wiki_dependencies WHERE wiki_page_id = ?",
                (page_id,),
            ).fetchall()
            for dep in deps:
                file_row = self._conn.execute(
                    "SELECT file_hash FROM files WHERE id = ?", (dep["file_id"],)
                ).fetchone()
                if file_row:
                    self._conn.execute(
                        """UPDATE wiki_dependencies
                           SET file_hash_at_compile = ?
                           WHERE wiki_page_id = ? AND file_id = ?""",
                        (file_row["file_hash"], page_id, dep["file_id"]),
                    )

        new_row = self._conn.execute(
            "SELECT version FROM wiki_pages WHERE id = ?", (page_id,)
        ).fetchone()

        return {"page_id": page_id, "version": new_row["version"]}

    def delete_page(self, page_id: str) -> bool:
        """Delete a wiki page (dependencies cascade)."""
        cur = self._conn.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
        return cur.rowcount > 0

    def list_pages(
        self, project_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List wiki pages for a project, newest first."""
        rows = self._conn.execute(
            """SELECT id, title, query_key, tags, updated_at, access_count, version
               FROM wiki_pages WHERE project_id = ?
               ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
            (project_id, limit, offset),
        ).fetchall()
        return [
            {
                "page_id": row["id"],
                "title": row["title"],
                "query_key": row["query_key"],
                "tags": json.loads(row["tags"]) if row["tags"] else [],
                "updated_at": row["updated_at"],
                "access_count": row["access_count"],
                "version": row["version"],
            }
            for row in rows
        ]

    def _check_page_staleness(self, page_id: str) -> dict:
        """Compare stored hashes against current file hashes."""
        deps = self._conn.execute(
            """SELECT wd.file_id, wd.file_hash_at_compile, f.file_hash, f.relative_path
               FROM wiki_dependencies wd
               LEFT JOIN files f ON wd.file_id = f.id
               WHERE wd.wiki_page_id = ?""",
            (page_id,),
        ).fetchall()

        changed_files = []
        for dep in deps:
            if dep["file_hash"] is None:
                # File was deleted
                changed_files.append(dep["file_id"])
            elif dep["file_hash"] != dep["file_hash_at_compile"]:
                changed_files.append(dep["relative_path"] or dep["file_id"])

        return {
            "stale": len(changed_files) > 0,
            "changed_files": changed_files,
            "total_dependencies": len(deps),
        }

    def _evict_lru(self, project_id: str) -> int:
        """Evict oldest-accessed pages when over the limit."""
        count_row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM wiki_pages WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        count = count_row["cnt"]

        if count <= self._max_pages:
            return 0

        excess = count - self._max_pages
        to_evict = self._conn.execute(
            """SELECT id FROM wiki_pages
               WHERE project_id = ?
               ORDER BY accessed_at ASC LIMIT ?""",
            (project_id, excess),
        ).fetchall()

        for row in to_evict:
            self._conn.execute("DELETE FROM wiki_pages WHERE id = ?", (row["id"],))

        return len(to_evict)
