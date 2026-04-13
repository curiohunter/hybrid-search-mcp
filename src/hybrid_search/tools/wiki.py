"""MCP tools: compile_to_wiki, lookup_wiki, check_wiki_staleness, refresh_wiki_page.

Reactive Wiki Layer — Claude compiles search results into wiki pages,
the server tracks file dependencies and detects staleness.
"""

from __future__ import annotations

import json
import logging

from hybrid_search.config import Config
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger(__name__)


class WikiError(Exception):
    """Raised when wiki operation setup fails."""


def _open_store(
    config: Config,
    registry: ProjectRegistry,
    project: str,
) -> tuple[str, StoreDB]:
    """Open StoreDB for a single project. Returns (project_id, db)."""
    info = registry.get_by_name(project)
    if info is None:
        raise WikiError(f"Project '{project}' not found")

    project_dir = get_project_dir(config.projects_dir, info.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        raise WikiError(f"Project '{project}' has no index. Run index_project first.")

    return info.id, StoreDB(idx_paths.store_db)


def _resolve_file_deps(
    db: StoreDB, source_chunk_ids: list[str]
) -> list[dict]:
    """Resolve chunk_ids to file dependencies with current hashes."""
    file_map: dict[str, dict] = {}

    for chunk_id in source_chunk_ids:
        chunk = db.get_chunk(chunk_id)
        if chunk is None:
            continue
        file_rec = db.get_file(chunk.file_id)
        if file_rec is None:
            continue

        if file_rec.id not in file_map:
            file_map[file_rec.id] = {
                "file_id": file_rec.id,
                "file_hash": file_rec.file_hash,
                "chunk_ids": [],
            }
        file_map[file_rec.id]["chunk_ids"].append(chunk_id)

    return list(file_map.values())


def handle_compile_to_wiki(
    config: Config,
    registry: ProjectRegistry,
    project: str,
    query: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_chunk_ids: list[str] | None = None,
) -> dict:
    """Compile search results into a wiki page with dependency tracking."""
    try:
        project_id, db = _open_store(config, registry, project)
    except WikiError as e:
        return {"error": str(e)}

    try:
        file_deps = _resolve_file_deps(db, source_chunk_ids or [])

        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        with db.transaction():
            result = wiki.compile_page(
                project_id=project_id,
                query=query,
                title=title,
                content=content,
                tags=tags,
                file_dependencies=file_deps,
            )

        return {
            **result,
            "title": title,
            "dependencies_count": len(file_deps),
        }
    finally:
        db.close()


def handle_lookup_wiki(
    config: Config,
    registry: ProjectRegistry,
    project: str,
    query: str | None = None,
    tag: str | None = None,
) -> dict:
    """Look up a cached wiki page by query or tag."""
    if not query and not tag:
        return {"error": "Either 'query' or 'tag' is required"}

    try:
        project_id, db = _open_store(config, registry, project)
    except WikiError as e:
        return {"error": str(e)}

    try:
        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        page = wiki.lookup_page(project_id, query=query, tag=tag)

        if page is None:
            return {"found": False}

        return {
            "found": True,
            "page_id": page.id,
            "title": page.title,
            "content": page.content,
            "tags": page.tags,
            "stale": page.stale,
            "changed_files": page.changed_files,
            "version": page.version,
            "access_count": page.access_count,
        }
    finally:
        db.close()


def handle_check_wiki_staleness(
    config: Config,
    registry: ProjectRegistry,
    project: str,
    page_id: str | None = None,
) -> dict:
    """Check staleness of wiki pages."""
    try:
        project_id, db = _open_store(config, registry, project)
    except WikiError as e:
        return {"error": str(e)}

    try:
        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        pages = wiki.check_staleness(project_id, page_id=page_id)
        return {"pages": pages}
    finally:
        db.close()


def handle_refresh_wiki_page(
    config: Config,
    registry: ProjectRegistry,
    project: str,
    page_id: str,
    content: str,
    source_chunk_ids: list[str] | None = None,
) -> dict:
    """Refresh a stale wiki page with updated content."""
    try:
        project_id, db = _open_store(config, registry, project)
    except WikiError as e:
        return {"error": str(e)}

    try:
        file_deps = None
        if source_chunk_ids is not None:
            file_deps = _resolve_file_deps(db, source_chunk_ids)

        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        with db.transaction():
            result = wiki.refresh_page(
                page_id=page_id,
                content=content,
                file_dependencies=file_deps,
            )

        if result is None:
            return {"error": f"Wiki page '{page_id}' not found"}

        return {
            **result,
            "dependencies_updated": file_deps is not None,
        }
    finally:
        db.close()
