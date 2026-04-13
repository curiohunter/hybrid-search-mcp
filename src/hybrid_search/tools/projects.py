"""MCP tools: list_projects, remove_project."""

from __future__ import annotations

from hybrid_search.config import Config
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


def handle_list_projects(registry: ProjectRegistry) -> dict:
    """Handle list_projects tool call."""
    projects = registry.list_all()
    return {
        "projects": [
            {
                "name": p.name,
                "path": p.path,
                "last_indexed_at": p.last_indexed_at,
                "file_count": p.file_count,
                "chunk_count": p.chunk_count,
            }
            for p in projects
        ]
    }


def handle_remove_project(
    config: Config,
    registry: ProjectRegistry,
    project: str,
    keep_index: bool = False,
) -> dict:
    """Handle remove_project tool call."""
    info = registry.get_by_name(project)
    if info is None:
        return {"error": f"Project '{project}' not found"}

    # Remove from registry
    registry.remove(info.id)

    # Optionally delete index data
    if not keep_index:
        project_dir = get_project_dir(config.projects_dir, info.id)
        idx_paths = IndexPaths(project_dir)
        idx_paths.delete_all()

    return {
        "removed": project,
        "index_deleted": not keep_index,
    }
