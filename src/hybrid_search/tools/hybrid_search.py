"""MCP tool: hybrid_search — BM25 + Vector with RRF fusion.

When reranking is enabled, returns expanded candidates (top-20) for
Claude Code to rerank by query intent — no external API calls needed.

Trust boundary: every parameter that crosses the MCP JSON-RPC surface is
sanitized by :mod:`hybrid_search.security` before reaching the orchestrator,
and every code chunk returned to the caller is stripped of control chars.
"""

from __future__ import annotations

from hybrid_search.memory import qa_log
from hybrid_search.search.orchestrator import SearchOrchestrator
from hybrid_search.security import (
    clamp_float,
    clamp_int,
    sanitize_cwd,
    sanitize_file_pattern,
    sanitize_node_types,
    sanitize_query,
    sanitize_snippet,
    validate_project_name,
)


_RERANK_HINT = (
    "[RERANK MODE] {n} candidates returned (more than requested limit {limit}). "
    "These are ranked by RRF score (BM25 + vector fusion), but RRF cannot judge "
    "query intent. You should reorder these results by relevance to the query "
    '"{query}" — prioritize results that best match the user\'s intent, not just '
    "keyword overlap. Present only the top {limit} most relevant results."
)

# Bounds — mirror the MCP inputSchema but enforced server-side as well.
_LIMIT_LO, _LIMIT_HI = 1, 50
_BM25_LO, _BM25_HI = 0.0, 1.0

# Progressive disclosure (compact mode). Code/doc hits ship snippet-only —
# the agent can Read file_path:start_line for depth. Memory-lane hits
# (conversations, commits, qa) have no readable file behind them, so they
# keep their content up to a cap instead.
_MEMORY_CONTENT_CAP = 900
_UNREADABLE_NODE_TYPES = {
    "qa_log", "memory_card", "domain_term", "episodic_example",
    "conv_turn", "commit", "module_card", "module_member", "graph_card",
}


def _compact_content(node_type: str | None, content: str | None) -> str | None:
    if content is None:
        return None
    if node_type in _UNREADABLE_NODE_TYPES:
        if len(content) <= _MEMORY_CONTENT_CAP:
            return content
        return content[:_MEMORY_CONTENT_CAP] + " …[truncated — full text via detail=\"full\"]"
    return None  # readable on disk — snippet + file_path:line is enough


def handle_hybrid_search(
    orchestrator: SearchOrchestrator,
    query: str,
    project: str | None = None,
    limit: int = 10,
    file_pattern: str | None = None,
    node_types: list[str] | None = None,
    bm25_weight: float | None = None,
    cwd: str | None = None,
    exclude_pattern: str | None = None,
    detail: str = "compact",
) -> dict:
    """Handle hybrid_search tool call — sanitizes all inputs and outputs."""
    safe_query = sanitize_query(query)
    safe_project = validate_project_name(project)
    safe_limit = clamp_int(limit, _LIMIT_LO, _LIMIT_HI, name="limit")
    safe_file_pattern = sanitize_file_pattern(file_pattern)
    safe_exclude_pattern = sanitize_file_pattern(exclude_pattern)
    safe_node_types = sanitize_node_types(node_types)
    safe_weight = (
        clamp_float(bm25_weight, _BM25_LO, _BM25_HI, name="bm25_weight")
        if bm25_weight is not None else None
    )
    safe_cwd = sanitize_cwd(cwd)

    response = orchestrator.hybrid_search(
        query=safe_query,
        project=safe_project,
        limit=safe_limit,
        file_pattern=safe_file_pattern,
        node_types=safe_node_types,
        bm25_weight=safe_weight,
        cwd=safe_cwd,
        exclude_pattern=safe_exclude_pattern,
    )

    full_detail = detail == "full"
    result: dict = {
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
                "content": (
                    sanitize_snippet(c)
                    if (c := (r.content if full_detail else _compact_content(r.node_type, r.content)))
                    is not None
                    else None
                ),
                "snippet": sanitize_snippet(r.snippet),
                "trust_meta": sanitize_snippet(getattr(r, "trust_meta", None)),
            }
            for r in response.results
        ],
        "query_type": response.query_type,
        "effective_bm25_weight": response.effective_bm25_weight,
        "query_time_ms": response.query_time_ms,
        "total_chunks_searched": response.total_chunks_searched,
        "top_score": getattr(response, "top_score", 0.0),
        "score_gap": getattr(response, "score_gap", None),
        "confidence": getattr(response, "confidence", "weak"),
        "skipped_projects": response.skipped_projects,
        "generated_ratio": getattr(response, "generated_ratio", 0.0),
        "top_cosine": getattr(response, "top_cosine", None),
    }
    if getattr(response, "fallback_hint", None):
        result["fallback_hint"] = sanitize_snippet(response.fallback_hint)

    if response.reranked and len(response.results) > safe_limit:
        result["rerank_hint"] = _RERANK_HINT.format(
            n=len(response.results),
            limit=safe_limit,
            query=safe_query,
        )

    # Memory layer — persist the exchange for cross-session recall (opt-in).
    # Fire-and-forget: disabled unless HYBRID_SEARCH_QA_LOG=1, never blocks
    # the response, swallows its own errors.
    try:
        project_infos = None
        registry = getattr(orchestrator, "_registry", None)
        if registry is not None:
            try:
                project_infos = registry.list_all()
            except Exception:
                project_infos = None
        qa_log.record(
            query=safe_query,
            response=response,
            cwd=safe_cwd,
            project_infos=project_infos,
        )
    except Exception:  # pragma: no cover — belt-and-suspenders
        pass

    return result
