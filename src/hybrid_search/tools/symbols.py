"""MCP tool: search_symbols — fuzzy symbol name search."""

from __future__ import annotations

from hybrid_search.config import Config
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


def handle_search_symbols(
    config: Config,
    registry: ProjectRegistry,
    name: str,
    project: str | None = None,
    node_types: list[str] | None = None,
) -> dict:
    """Handle search_symbols tool call."""
    if project:
        info = registry.get_by_name(project)
        if info is None:
            return {"error": f"Project '{project}' not found"}
        project_infos = [info]
    else:
        project_infos = registry.list_all()

    results: list[dict] = []

    for pinfo in project_infos:
        project_dir = get_project_dir(config.projects_dir, pinfo.id)
        idx_paths = IndexPaths(project_dir)

        if not idx_paths.store_db.exists():
            continue

        db = StoreDB(idx_paths.store_db)
        try:
            chunks = db.search_chunks_by_name(name, pinfo.id)
            for chunk in chunks:
                if node_types and chunk.node_type not in node_types:
                    continue

                file_rec = db.get_file(chunk.file_id)
                file_path = file_rec.relative_path if file_rec else chunk.file_id

                results.append({
                    "chunk_id": chunk.id,
                    "project": pinfo.name,
                    "name": chunk.name,
                    "qualified_name": chunk.qualified_name,
                    "node_type": chunk.node_type,
                    "file_path": file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content": chunk.content[:500] if chunk.content else None,
                })
        finally:
            db.close()

    return {"results": results, "total": len(results)}
