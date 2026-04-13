"""MCP tool: hybrid_search — BM25 + Vector with RRF fusion."""

from __future__ import annotations

from hybrid_search.search.orchestrator import SearchOrchestrator


def handle_hybrid_search(
    orchestrator: SearchOrchestrator,
    query: str,
    project: str | None = None,
    limit: int = 10,
    file_pattern: str | None = None,
    node_types: list[str] | None = None,
    bm25_weight: float | None = None,
    cwd: str | None = None,
) -> dict:
    """Handle hybrid_search tool call."""
    response = orchestrator.hybrid_search(
        query=query,
        project=project,
        limit=limit,
        file_pattern=file_pattern,
        node_types=node_types,
        bm25_weight=bm25_weight,
        cwd=cwd,
    )

    return {
        "results": [
            {
                "chunk_id": r.chunk_id,
                "rrf_score": r.rrf_score,
                "bm25_rank": r.bm25_rank,
                "vector_rank": r.vector_rank,
                "file_path": r.file_path,
                "project": r.project,
                "name": r.name,
                "qualified_name": r.qualified_name,
                "node_type": r.node_type,
                "start_line": r.start_line,
                "end_line": r.end_line,
                "content": r.content,
                "snippet": r.snippet,
            }
            for r in response.results
        ],
        "query_type": response.query_type,
        "effective_bm25_weight": response.effective_bm25_weight,
        "query_time_ms": response.query_time_ms,
        "total_chunks_searched": response.total_chunks_searched,
        "skipped_projects": response.skipped_projects,
    }
