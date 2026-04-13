"""Call Graph Resolution — resolves raw call edges to chunk IDs with confidence levels.

Implements the 3-tier resolution strategy from design.md §12:
  High:   import path + symbol name → exact chunk match
  Medium: qualified_name match within the same project
  Low:    name-only match (common names filtered)
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from hybrid_search.storage.db import StoreDB

logger = logging.getLogger(__name__)

# Common function names that are too generic for reliable name-only matching.
# These remain at 'low' confidence even if a single match is found.
COMMON_NAMES = frozenset({
    "run", "init", "get", "set", "render", "handle", "process",
    "validate", "update", "create", "delete", "remove", "add",
    "start", "stop", "close", "open", "read", "write", "send",
    "fetch", "load", "save", "parse", "format", "log", "print",
    "map", "filter", "reduce", "sort", "find", "each", "every",
    "some", "includes", "push", "pop", "shift", "then", "catch",
    "next", "done", "callback", "resolve", "reject", "emit",
    "on", "off", "once", "listen", "dispatch", "subscribe",
    "toString", "valueOf", "apply", "call", "bind",
    "__init__", "__str__", "__repr__", "setUp", "tearDown",
})


def resolve_call_edges(db: StoreDB, project_id: str) -> dict:
    """Resolve all unresolved call edges for a project.

    Returns stats dict with counts of resolved edges per confidence level.
    """
    edges = db.get_all_call_edges(project_id)
    if not edges:
        return {"total": 0, "high": 0, "medium": 0, "low": 0, "unresolved": 0}

    # Pre-build lookup indexes for the project
    all_chunks = db.get_chunks_by_project(project_id)

    # qualified_name → chunk_id (exact match)
    qname_index: dict[str, str] = {}
    # name → list of chunk_ids (for name-only matching)
    name_index: dict[str, list[tuple[str, str]]] = {}  # name → [(chunk_id, qualified_name)]
    # chunk_id → file_id (O(1) lookup instead of linear scan)
    file_index: dict[str, str] = {}

    for chunk in all_chunks:
        if chunk.qualified_name:
            qname_index[chunk.qualified_name] = chunk.id
        if chunk.name:
            name_index.setdefault(chunk.name, []).append((chunk.id, chunk.qualified_name or ""))
        file_index[chunk.id] = chunk.file_id

    stats = {"total": len(edges), "high": 0, "medium": 0, "low": 0, "unresolved": 0}
    updates: list[tuple[int, str, str | None, str]] = []  # (rowid, chunk_id, qname, confidence)

    for edge in edges:
        callee_name = edge["callee_name"]
        callee_module = edge.get("callee_module")
        rowid = edge["rowid"]

        # Skip edges already resolved at medium/high confidence.
        # Re-resolve low-confidence edges — they may upgrade after more chunks are indexed.
        if edge.get("callee_chunk_id") and edge.get("confidence") != "low":
            continue

        resolved_id, resolved_qname, confidence = _resolve_single(
            callee_name, callee_module, edge.get("caller_chunk_id"),
            qname_index, name_index, file_index, project_id,
        )

        if resolved_id:
            updates.append((rowid, resolved_id, resolved_qname, confidence))
            stats[confidence] += 1
        else:
            stats["unresolved"] += 1

    # Batch update within a transaction
    if updates:
        with db.transaction() as conn:
            for rowid, chunk_id, qname, confidence in updates:
                db.update_call_edge_resolution(conn, rowid, chunk_id, qname, confidence)

    logger.info(
        "Call edge resolution for %s: %d total, %d high, %d medium, %d low, %d unresolved",
        project_id, stats["total"], stats["high"], stats["medium"],
        stats["low"], stats["unresolved"],
    )
    return stats


def _resolve_single(
    callee_name: str,
    callee_module: str | None,
    caller_chunk_id: str | None,
    qname_index: dict[str, str],
    name_index: dict[str, list[tuple[str, str]]],
    file_index: dict[str, str],
    project_id: str,
) -> tuple[str | None, str | None, str]:
    """Try to resolve a single call edge. Returns (chunk_id, qualified_name, confidence)."""

    # Strategy 1 (High): import path + symbol name → qualified_name match
    if callee_module:
        for qname, chunk_id in qname_index.items():
            if callee_name in qname and _module_matches(callee_module, qname):
                return chunk_id, qname, "high"

    # Strategy 2 (Medium): qualified_name contains the callee name
    if "." in callee_name:
        if callee_name in qname_index:
            return qname_index[callee_name], callee_name, "medium"
        for qname, chunk_id in qname_index.items():
            if qname.endswith(f".{callee_name}") or qname.endswith(f"::{callee_name}"):
                return chunk_id, qname, "medium"

    # Strategy 2b: exact name match with single candidate
    candidates = name_index.get(callee_name, [])
    if len(candidates) == 1:
        chunk_id, qname = candidates[0]
        if callee_name.lower() in COMMON_NAMES:
            return chunk_id, qname, "low"
        return chunk_id, qname, "medium"

    # Strategy 3 (Low): name-only match with multiple candidates
    if candidates and callee_name.lower() not in COMMON_NAMES:
        # Pick the candidate in the same file as the caller if possible
        if caller_chunk_id:
            caller_file = file_index.get(caller_chunk_id)
            for chunk_id, qname in candidates:
                if file_index.get(chunk_id) == caller_file:
                    return chunk_id, qname, "medium"
        chunk_id, qname = candidates[0]
        return chunk_id, qname, "low"

    return None, None, "low"


def _module_matches(import_path: str, qualified_name: str) -> bool:
    """Check if an import path plausibly matches a qualified name's file path."""
    # Strip leading "./" or "@/" from import path (removeprefix, not lstrip)
    clean = import_path.removeprefix("./").removeprefix("@/")
    # qualified_name format: "path/to/file.ts::functionName"
    file_part = qualified_name.split("::")[0] if "::" in qualified_name else ""
    # Check if the import path is a suffix of the file path (without extension)
    file_stem = str(PurePosixPath(file_part).with_suffix("")) if file_part else ""
    return file_stem.endswith(clean) or clean.endswith(file_stem)

