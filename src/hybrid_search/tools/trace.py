"""MCP tools: trace_callers / trace_callees — call graph traversal.

Implements §10.3 and §10.4 of the design doc:
  - visited set for cycle prevention
  - 100 node cap with truncated flag
  - partial results when unresolved edges remain
  - chunk_id takes precedence over symbol
"""

from __future__ import annotations

from hybrid_search.config import Config
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

MAX_NODES = 100


class TraceError(Exception):
    """Raised when trace setup fails (project not found, no index, etc.)."""


def handle_trace_callers(
    config: Config,
    registry: ProjectRegistry,
    symbol: str | None = None,
    chunk_id: str | None = None,
    project: str | None = None,
    depth: int = 2,
    min_confidence: str = "medium",
) -> dict:
    """Trace all functions that call the given function (reverse call graph)."""
    if not symbol and not chunk_id:
        return {"error": "Either 'symbol' or 'chunk_id' is required"}

    try:
        stores = _open_stores(config, registry, project)
    except TraceError as e:
        return {"error": str(e)}

    visited: set[str] = set()
    nodes: list[dict] = []
    truncated = False

    try:
        start_ids = _resolve_start(stores, chunk_id, symbol, project)
        if not start_ids:
            return {"error": f"No matching chunk found for {'chunk_id=' + chunk_id if chunk_id else 'symbol=' + (symbol or '')}"}

        for start_id, start_project, db in start_ids:
            _trace_callers_recursive(
                db, start_id, start_project, min_confidence,
                depth, 0, visited, nodes,
            )
            if len(nodes) >= MAX_NODES:
                truncated = True
                break
    finally:
        for _, _, db in stores:
            db.close()

    return {
        "root": chunk_id or symbol,
        "direction": "callers",
        "nodes": nodes[:MAX_NODES],
        "total": len(nodes),
        "depth_requested": depth,
        "truncated": truncated,
        "partial": any(n.get("unresolved") for n in nodes),
    }


def handle_trace_callees(
    config: Config,
    registry: ProjectRegistry,
    symbol: str | None = None,
    chunk_id: str | None = None,
    project: str | None = None,
    depth: int = 2,
    min_confidence: str = "medium",
) -> dict:
    """Trace all functions called by the given function (forward call graph)."""
    if not symbol and not chunk_id:
        return {"error": "Either 'symbol' or 'chunk_id' is required"}

    try:
        stores = _open_stores(config, registry, project)
    except TraceError as e:
        return {"error": str(e)}

    visited: set[str] = set()
    nodes: list[dict] = []
    truncated = False

    try:
        start_ids = _resolve_start(stores, chunk_id, symbol, project)
        if not start_ids:
            return {"error": f"No matching chunk found for {'chunk_id=' + chunk_id if chunk_id else 'symbol=' + (symbol or '')}"}

        for start_id, start_project, db in start_ids:
            _trace_callees_recursive(
                db, start_id, start_project, min_confidence,
                depth, 0, visited, nodes,
            )
            if len(nodes) >= MAX_NODES:
                truncated = True
                break
    finally:
        for _, _, db in stores:
            db.close()

    return {
        "root": chunk_id or symbol,
        "direction": "callees",
        "nodes": nodes[:MAX_NODES],
        "total": len(nodes),
        "depth_requested": depth,
        "truncated": truncated,
        "partial": any(n.get("unresolved") for n in nodes),
    }


def _trace_callers_recursive(
    db: StoreDB,
    chunk_id: str,
    project_id: str,
    min_confidence: str,
    max_depth: int,
    current_depth: int,
    visited: set[str],
    nodes: list[dict],
) -> None:
    """Recursively trace callers up to max_depth."""
    if current_depth >= max_depth or len(nodes) >= MAX_NODES:
        return
    if chunk_id in visited:
        return
    visited.add(chunk_id)

    callers = db.get_callers(chunk_id, project_id, min_confidence)
    for caller in callers:
        caller_id = caller["caller_chunk_id"]
        if caller_id in visited or len(nodes) >= MAX_NODES:
            continue

        nodes.append({
            "chunk_id": caller_id,
            "name": caller.get("name"),
            "qualified_name": caller.get("qualified_name"),
            "node_type": caller.get("node_type"),
            "file_path": caller.get("relative_path"),
            "start_line": caller.get("start_line"),
            "end_line": caller.get("end_line"),
            "confidence": caller.get("confidence"),
            "depth": current_depth + 1,
            "calls": chunk_id,
        })

        _trace_callers_recursive(
            db, caller_id, project_id, min_confidence,
            max_depth, current_depth + 1, visited, nodes,
        )


def _trace_callees_recursive(
    db: StoreDB,
    chunk_id: str,
    project_id: str,
    min_confidence: str,
    max_depth: int,
    current_depth: int,
    visited: set[str],
    nodes: list[dict],
) -> None:
    """Recursively trace callees up to max_depth."""
    if current_depth >= max_depth or len(nodes) >= MAX_NODES:
        return
    if chunk_id in visited:
        return
    visited.add(chunk_id)

    callees = db.get_callees(chunk_id, project_id, min_confidence)
    for callee in callees:
        callee_id = callee.get("callee_chunk_id")
        if not callee_id:
            # Unresolved edge — include as partial result
            nodes.append({
                "callee_name": callee.get("callee_name"),
                "callee_qualified_name": callee.get("callee_qualified_name"),
                "confidence": callee.get("confidence"),
                "depth": current_depth + 1,
                "called_by": chunk_id,
                "unresolved": True,
            })
            continue

        if callee_id in visited or len(nodes) >= MAX_NODES:
            continue

        nodes.append({
            "chunk_id": callee_id,
            "name": callee.get("name"),
            "qualified_name": callee.get("qualified_name"),
            "node_type": callee.get("node_type"),
            "file_path": callee.get("relative_path"),
            "start_line": callee.get("start_line"),
            "end_line": callee.get("end_line"),
            "confidence": callee.get("confidence"),
            "depth": current_depth + 1,
            "called_by": chunk_id,
        })

        _trace_callees_recursive(
            db, callee_id, project_id, min_confidence,
            max_depth, current_depth + 1, visited, nodes,
        )


def _open_stores(
    config: Config,
    registry: ProjectRegistry,
    project: str | None,
) -> list[tuple[str, str, StoreDB]]:
    """Open StoreDB instances for the target project(s).

    Returns list of (project_id, project_name, db).
    Raises TraceError if no valid stores found.
    """
    if project:
        info = registry.get_by_name(project)
        if info is None:
            raise TraceError(f"Project '{project}' not found")
        project_infos = [info]
    else:
        project_infos = registry.list_all()

    stores: list[tuple[str, str, StoreDB]] = []
    for pinfo in project_infos:
        project_dir = get_project_dir(config.projects_dir, pinfo.id)
        idx_paths = IndexPaths(project_dir)
        if idx_paths.store_db.exists():
            stores.append((pinfo.id, pinfo.name, StoreDB(idx_paths.store_db)))

    if not stores:
        raise TraceError("No indexed projects found")

    return stores


def _resolve_start(
    stores: list[tuple[str, str, StoreDB]],
    chunk_id: str | None,
    symbol: str | None,
    project: str | None,
) -> list[tuple[str, str, StoreDB]]:
    """Resolve the starting chunk for traversal.

    Returns list of (chunk_id, project_id, db) tuples.

    For symbol-based resolution, uses deterministic ordering:
    exact qualified_name > exact name > fuzzy LIKE match.
    When multiple fuzzy matches exist, returns ALL matches (not just the first)
    so the caller can trace from each, producing complete results.
    """
    results: list[tuple[str, str, StoreDB]] = []

    if chunk_id:
        # chunk_id takes precedence — find which store has it
        for pid, pname, db in stores:
            chunk = db.get_chunk(chunk_id)
            if chunk:
                results.append((chunk_id, pid, db))
                return results
    elif symbol:
        for pid, pname, db in stores:
            # Priority 1: exact qualified_name match (deterministic)
            chunk = db.find_chunk_by_qualified_name(symbol, pid)
            if chunk:
                results.append((chunk.id, pid, db))
                continue

            # Priority 2: exact name match
            chunks = db.find_chunks_by_name(symbol, pid)
            if chunks:
                # Include all exact matches (not just first) for deterministic results
                for c in chunks:
                    results.append((c.id, pid, db))
                continue

            # Priority 3: fuzzy LIKE match — include all matches
            chunks = db.search_chunks_by_name(symbol, pid)
            if chunks:
                for c in chunks:
                    results.append((c.id, pid, db))

    return results
