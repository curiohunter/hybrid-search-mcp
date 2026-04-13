"""Wiki page storage with dependency tracking, staleness detection, and graph traversal."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# [[link_text]] pattern — matches wikilinks in wiki page content
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _extract_snippet(content: str, max_len: int = 200) -> str:
    """Extract first meaningful line from wiki content as a snippet."""
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(">") or stripped.startswith("|"):
            continue
        if len(stripped) > max_len:
            return stripped[:max_len] + "…"
        return stripped
    return content[:max_len] + "…" if len(content) > max_len else content


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
class LinkedPage:
    """Summary of a linked wiki page for graph expansion."""
    page_id: str
    title: str
    link_text: str
    snippet: str
    hop: int


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
    linked_pages: list[LinkedPage] = field(default_factory=list)


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

        # Extract and store wikilinks
        self._sync_wikilinks(page_id, project_id, content)

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
        linked = self._expand_graph(row["id"], row["project_id"], max_hops=2)

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
            linked_pages=linked,
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

        # Re-sync wikilinks from updated content
        self._sync_wikilinks(page_id, row["project_id"], content)

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

    # -- wikilink graph --

    def _sync_wikilinks(
        self, page_id: str, project_id: str, content: str
    ) -> None:
        """Parse [[link_text]] from content and upsert wiki_links rows."""
        self._conn.execute(
            "DELETE FROM wiki_links WHERE source_page_id = ?", (page_id,)
        )

        link_texts = _WIKILINK_RE.findall(content)
        if not link_texts:
            return

        for text in dict.fromkeys(link_texts):  # dedupe, preserve order
            # Resolve link_text → target page by title match (case-insensitive)
            target = self._conn.execute(
                """SELECT id FROM wiki_pages
                   WHERE project_id = ? AND LOWER(title) = LOWER(?)""",
                (project_id, text),
            ).fetchone()
            if target is None:
                # Try matching by query_key
                query_key = normalize_query(text)
                target_id = _page_id(project_id, query_key)
                exists = self._conn.execute(
                    "SELECT id FROM wiki_pages WHERE id = ?", (target_id,)
                ).fetchone()
                if exists is None:
                    continue
                target_page_id = target_id
            else:
                target_page_id = target["id"]

            if target_page_id == page_id:
                continue  # skip self-links

            self._conn.execute(
                """INSERT OR IGNORE INTO wiki_links
                   (source_page_id, target_page_id, link_text)
                   VALUES (?, ?, ?)""",
                (page_id, target_page_id, text),
            )

    def _expand_graph(
        self,
        start_page_id: str,
        project_id: str,
        max_hops: int = 2,
        max_pages: int = 10,
    ) -> list[LinkedPage]:
        """BFS from start page, following wikilinks up to max_hops.

        Returns linked pages with their hop distance and a content snippet.
        """
        visited: set[str] = {start_page_id}
        queue: deque[tuple[str, int]] = deque()  # (page_id, hop)
        result: list[LinkedPage] = []

        # Seed: outgoing links from start page
        outgoing = self._conn.execute(
            "SELECT target_page_id, link_text FROM wiki_links WHERE source_page_id = ?",
            (start_page_id,),
        ).fetchall()
        for row in outgoing:
            if row["target_page_id"] not in visited:
                queue.append((row["target_page_id"], 1))
                visited.add(row["target_page_id"])

        # Also include incoming links (pages that link TO this page)
        incoming = self._conn.execute(
            "SELECT source_page_id, link_text FROM wiki_links WHERE target_page_id = ?",
            (start_page_id,),
        ).fetchall()
        for row in incoming:
            if row["source_page_id"] not in visited:
                queue.append((row["source_page_id"], 1))
                visited.add(row["source_page_id"])

        while queue and len(result) < max_pages:
            page_id, hop = queue.popleft()

            page_row = self._conn.execute(
                "SELECT id, title, content FROM wiki_pages WHERE id = ? AND project_id = ?",
                (page_id, project_id),
            ).fetchone()
            if page_row is None:
                continue

            # Build snippet: first non-empty, non-heading line, truncated
            snippet = _extract_snippet(page_row["content"])

            # Find link_text used to reach this page
            link_row = self._conn.execute(
                """SELECT link_text FROM wiki_links
                   WHERE (source_page_id = ? AND target_page_id = ?)
                      OR (source_page_id = ? AND target_page_id = ?)
                   LIMIT 1""",
                (start_page_id, page_id, page_id, start_page_id),
            ).fetchone()
            link_text = link_row["link_text"] if link_row else page_row["title"]

            result.append(LinkedPage(
                page_id=page_id,
                title=page_row["title"],
                link_text=link_text,
                snippet=snippet,
                hop=hop,
            ))

            # Expand further if within hop limit
            if hop < max_hops:
                neighbors = self._conn.execute(
                    """SELECT target_page_id AS neighbor_id FROM wiki_links
                       WHERE source_page_id = ?
                       UNION
                       SELECT source_page_id AS neighbor_id FROM wiki_links
                       WHERE target_page_id = ?""",
                    (page_id, page_id),
                ).fetchall()
                for n in neighbors:
                    neighbor_id = n["neighbor_id"]
                    if neighbor_id not in visited:
                        queue.append((neighbor_id, hop + 1))
                        visited.add(neighbor_id)

        return result

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
