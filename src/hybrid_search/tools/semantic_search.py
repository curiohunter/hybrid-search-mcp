"""MCP tool: semantic_search — pure vector search."""

from __future__ import annotations

import time

from hybrid_search.config import Config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


def handle_semantic_search(
    config: Config,
    registry: ProjectRegistry,
    embedder: Embedder,
    query: str,
    project: str | None = None,
    limit: int = 10,
    file_pattern: str | None = None,
    node_types: list[str] | None = None,
    similarity_threshold: float = 0.5,
) -> dict:
    """Handle semantic_search tool call."""
    start = time.monotonic()

    # Determine which projects to search
    if project:
        info = registry.get_by_name(project)
        if info is None:
            return {"error": f"Project '{project}' not found"}
        project_infos = [info]
    else:
        project_infos = registry.list_all()

    if not project_infos:
        return {"results": [], "query_time_ms": 0, "total_chunks_searched": 0}

    # Embed query
    query_vector = embedder.embed_query(query)

    all_results: list[dict] = []
    total_chunks = 0

    for pinfo in project_infos:
        pid = pinfo.id
        project_dir = get_project_dir(config.projects_dir, pid)
        idx_paths = IndexPaths(project_dir)

        if not idx_paths.store_db.exists():
            continue

        db = StoreDB(idx_paths.store_db)
        vector_engine = VectorEngine(idx_paths.vectors_dir, embedder.embedding_dim)

        try:
            total_chunks += vector_engine.count

            # Build chunk ID filter if file_pattern or node_types specified
            chunk_filter = _build_filter(db, pid, file_pattern, node_types)

            # Search vectors
            vec_results = vector_engine.search(
                query_vector,
                limit=limit * 3,
                chunk_ids_filter=chunk_filter,
            )

            for vr in vec_results:
                if vr.score < similarity_threshold:
                    continue

                chunk = db.get_chunk(vr.chunk_id)
                if chunk is None:
                    continue

                file_rec = db.get_file(chunk.file_id)
                file_path = file_rec.relative_path if file_rec else chunk.file_id

                all_results.append({
                    "chunk_id": chunk.id,
                    "similarity": round(vr.score, 4),
                    "file_path": file_path,
                    "project": pinfo.name,
                    "name": chunk.name,
                    "qualified_name": chunk.qualified_name,
                    "node_type": chunk.node_type,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "content": chunk.content,
                    "snippet": _make_snippet(chunk.docstring, chunk.content),
                })
        finally:
            db.close()

    # Sort by similarity descending, take top limit
    all_results.sort(key=lambda r: r["similarity"], reverse=True)
    all_results = all_results[:limit]

    elapsed_ms = (time.monotonic() - start) * 1000

    return {
        "results": all_results,
        "query_time_ms": round(elapsed_ms, 1),
        "total_chunks_searched": total_chunks,
    }


def _build_filter(
    db: StoreDB,
    project_id: str,
    file_pattern: str | None,
    node_types: list[str] | None,
) -> set[str] | None:
    """Build a set of chunk IDs matching the filter criteria."""
    if not file_pattern and not node_types:
        return None

    import fnmatch

    chunks = db.get_chunks_by_project(project_id)
    filtered_ids: set[str] = set()

    for chunk in chunks:
        if file_pattern:
            # Get file path from chunk's file_id
            file_rec = db.get_file(chunk.file_id)
            if file_rec and not fnmatch.fnmatch(file_rec.relative_path, file_pattern):
                continue

        if node_types and chunk.node_type not in node_types:
            continue

        filtered_ids.add(chunk.id)

    return filtered_ids if (file_pattern or node_types) else None


def _make_snippet(docstring: str | None, content: str | None) -> str:
    """Create a short snippet for display."""
    if docstring:
        return docstring[:200]
    if content:
        lines = content.strip().split("\n")
        return "\n".join(lines[:5])[:200]
    return ""
