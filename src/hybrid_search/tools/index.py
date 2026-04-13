"""MCP tools: index_project, index_status."""

from __future__ import annotations

from hybrid_search.index.pipeline import IndexingPipeline


def handle_index_project(
    pipeline: IndexingPipeline,
    project_path: str,
    project_name: str | None = None,
    force: bool = False,
) -> dict:
    """Handle index_project tool call."""
    result = pipeline.index_project(project_path, project_name, force)
    return {
        "project_id": result.project_id,
        "project_name": result.project_name,
        "files_added": result.files_added,
        "files_changed": result.files_changed,
        "files_deleted": result.files_deleted,
        "chunks_total": result.chunks_total,
        "elapsed_seconds": round(result.elapsed_seconds, 1),
        "errors": result.errors,
    }


def handle_index_status(
    pipeline: IndexingPipeline,
    project: str | None = None,
) -> dict:
    """Handle index_status tool call."""
    registry = pipeline._registry

    if project:
        info = registry.get_by_name(project)
        if info is None:
            return {"error": f"Project '{project}' not found"}
        projects = [info]
    else:
        projects = registry.list_all()

    return {
        "projects": [
            {
                "name": p.name,
                "path": p.path,
                "last_indexed_at": p.last_indexed_at,
                "file_count": p.file_count,
                "chunk_count": p.chunk_count,
                "index_version": p.index_version,
            }
            for p in projects
        ]
    }
