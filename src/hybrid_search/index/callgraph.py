"""Call Graph Resolution — resolves raw call edges to chunk IDs with confidence levels.

Implements the 3-tier resolution strategy from design.md §12:
  extracted: import path + symbol name → exact chunk match
  inferred:  qualified_name match within the same project
  ambiguous: name-only match (common names filtered)
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from hybrid_search.storage.db import CONFIDENCE_SCORES, StoreDB

logger = logging.getLogger(__name__)

# Common function names that are too generic for reliable name-only matching.
# These remain at 'ambiguous' confidence even if a single match is found.
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


def _build_module_index(all_files: list, all_chunks: list) -> dict[str, list[tuple[str, str]]]:
    """Build module path → [(chunk_id, chunk_name)] index for High confidence resolution.

    Maps various forms of a file's import path to the chunks in that file.
    e.g. "src/auth" → chunks from src/auth.ts, src/auth.py, src/auth/index.ts, etc.
    """
    # file_id → list of (chunk_id, chunk_name)
    file_chunks: dict[str, list[tuple[str, str]]] = {}
    for chunk in all_chunks:
        file_chunks.setdefault(chunk.file_id, []).append((chunk.id, chunk.name or ""))

    module_index: dict[str, list[tuple[str, str]]] = {}

    for file_rec in all_files:
        rel = file_rec.relative_path
        chunks_in_file = file_chunks.get(file_rec.id, [])
        if not chunks_in_file:
            continue

        # Generate possible import path forms
        p = PurePosixPath(rel)
        stem = str(p.with_suffix(""))  # "src/auth/login.ts" → "src/auth/login"

        # Direct stem: "src/auth/login"
        module_index.setdefault(stem, []).extend(chunks_in_file)

        # With "./" prefix: "./auth/login"
        if not stem.startswith("./"):
            module_index.setdefault(f"./{stem}", []).extend(chunks_in_file)

        # index file convention: "src/auth/index.ts" → "src/auth"
        if p.stem == "index":
            parent = str(p.parent)
            module_index.setdefault(parent, []).extend(chunks_in_file)
            if not parent.startswith("./"):
                module_index.setdefault(f"./{parent}", []).extend(chunks_in_file)

        # Python dotted path: "src/auth/login.py" → "src.auth.login"
        dotted = stem.replace("/", ".")
        module_index.setdefault(dotted, []).extend(chunks_in_file)

    return module_index


def resolve_call_edges(db: StoreDB, project_id: str) -> dict:
    """Resolve all unresolved call edges for a project.

    Returns stats dict with counts of resolved edges per confidence level.
    """
    edges = db.get_all_call_edges(project_id)
    if not edges:
        return {"total": 0, "extracted": 0, "inferred": 0, "ambiguous": 0, "unresolved": 0}

    # Pre-build lookup indexes for the project
    all_chunks = db.get_chunks_by_project(project_id)
    all_files = db.get_all_files(project_id)

    # qualified_name → chunk_id (exact match)
    qname_index: dict[str, str] = {}
    # name → list of chunk_ids (for name-only matching)
    name_index: dict[str, list[tuple[str, str]]] = {}  # name → [(chunk_id, qualified_name)]
    # chunk_id → file_id (O(1) lookup instead of linear scan)
    file_index: dict[str, str] = {}

    # parent_name → {name → [(chunk_id, qname)]} for self/this resolution (Step 3)
    class_members: dict[str, dict[str, list[tuple[str, str]]]] = {}

    for chunk in all_chunks:
        if chunk.qualified_name:
            qname_index[chunk.qualified_name] = chunk.id
        if chunk.name:
            name_index.setdefault(chunk.name, []).append((chunk.id, chunk.qualified_name or ""))
        file_index[chunk.id] = chunk.file_id
        if chunk.parent_name and chunk.name:
            class_members.setdefault(chunk.parent_name, {}).setdefault(
                chunk.name, [],
            ).append((chunk.id, chunk.qualified_name or ""))

    # Module path → chunks index (Step 2: import path resolution)
    module_index = _build_module_index(all_files, all_chunks)

    stats = {"total": len(edges), "extracted": 0, "inferred": 0, "ambiguous": 0, "unresolved": 0}
    # (rowid, chunk_id, qname, confidence, score)
    updates: list[tuple[int, str, str | None, str, float]] = []

    for edge in edges:
        callee_name = edge["callee_name"]
        callee_module = edge.get("callee_module")
        rowid = edge["rowid"]

        # Skip edges already resolved at inferred/extracted confidence.
        # Re-resolve ambiguous edges — they may upgrade after more chunks are indexed.
        if edge.get("callee_chunk_id") and edge.get("confidence") != "ambiguous":
            continue

        resolved_id, resolved_qname, confidence = _resolve_single(
            callee_name, callee_module, edge.get("caller_chunk_id"),
            qname_index, name_index, file_index, project_id, module_index,
            class_members,
        )

        if resolved_id:
            score = CONFIDENCE_SCORES.get(confidence, 0.0)
            updates.append((rowid, resolved_id, resolved_qname, confidence, score))
            stats[confidence] += 1
        else:
            stats["unresolved"] += 1

    # Batch update within a transaction
    if updates:
        with db.transaction() as conn:
            for rowid, chunk_id, qname, confidence, score in updates:
                db.update_call_edge_resolution(
                    conn, rowid, chunk_id, qname, confidence, score,
                )

    logger.info(
        "Call edge resolution for %s: %d total, %d extracted, %d inferred, %d ambiguous, %d unresolved",
        project_id, stats["total"], stats["extracted"], stats["inferred"],
        stats["ambiguous"], stats["unresolved"],
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
    module_index: dict[str, list[tuple[str, str]]] | None = None,
    class_members: dict[str, dict[str, list[tuple[str, str]]]] | None = None,
) -> tuple[str | None, str | None, str]:
    """Try to resolve a single call edge. Returns (chunk_id, qualified_name, confidence)."""

    # Strategy 0 (extracted): this/self method call → class member lookup
    if callee_module and callee_module.startswith("__self__::") and class_members:
        class_name = callee_module.removeprefix("__self__::")
        members = class_members.get(class_name, {})
        matches = members.get(callee_name, [])
        if len(matches) == 1:
            chunk_id, qname = matches[0]
            return chunk_id, qname, "extracted"
        elif len(matches) > 1:
            # Multiple matches (e.g. overloaded) — pick first, inferred confidence
            chunk_id, qname = matches[0]
            return chunk_id, qname, "inferred"

    # Strategy 1 (extracted): module index lookup — direct match via import path
    if callee_module and not callee_module.startswith("__self__::") and module_index:
        candidates = module_index.get(callee_module, [])
        for chunk_id, chunk_name in candidates:
            if chunk_name == callee_name:
                qname = next(
                    (q for q, cid in qname_index.items() if cid == chunk_id), None,
                )
                return chunk_id, qname, "extracted"

    # Strategy 1b (extracted): fallback — scan qname_index for module match
    if callee_module and not callee_module.startswith("__self__::"):
        for qname, chunk_id in qname_index.items():
            if callee_name in qname and _module_matches(callee_module, qname):
                return chunk_id, qname, "extracted"

    # Strategy 2 (inferred): qualified_name contains the callee name
    if "." in callee_name:
        if callee_name in qname_index:
            return qname_index[callee_name], callee_name, "inferred"
        for qname, chunk_id in qname_index.items():
            if qname.endswith(f".{callee_name}") or qname.endswith(f"::{callee_name}"):
                return chunk_id, qname, "inferred"

    # Strategy 2b: exact name match with single candidate
    candidates = name_index.get(callee_name, [])
    has_context = callee_module is not None  # Step 4: module info upgrades confidence
    if len(candidates) == 1:
        chunk_id, qname = candidates[0]
        if callee_name.lower() in COMMON_NAMES and not has_context:
            return chunk_id, qname, "ambiguous"
        return chunk_id, qname, "inferred"

    # Strategy 3 (ambiguous): name-only match with multiple candidates
    if candidates and (callee_name.lower() not in COMMON_NAMES or has_context):
        # Pick the candidate in the same file as the caller if possible
        if caller_chunk_id:
            caller_file = file_index.get(caller_chunk_id)
            for chunk_id, qname in candidates:
                if file_index.get(chunk_id) == caller_file:
                    return chunk_id, qname, "inferred"
        chunk_id, qname = candidates[0]
        return chunk_id, qname, "ambiguous"

    return None, None, "ambiguous"


def _module_matches(import_path: str, qualified_name: str) -> bool:
    """Check if an import path plausibly matches a qualified name's file path."""
    # Strip leading "./" or "@/" from import path (removeprefix, not lstrip)
    clean = import_path.removeprefix("./").removeprefix("@/")
    # qualified_name format: "path/to/file.ts::functionName"
    file_part = qualified_name.split("::")[0] if "::" in qualified_name else ""
    # Check if the import path is a suffix of the file path (without extension)
    file_stem = str(PurePosixPath(file_part).with_suffix("")) if file_part else ""
    return file_stem.endswith(clean) or clean.endswith(file_stem)

