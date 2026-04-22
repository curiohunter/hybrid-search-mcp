"""Module card synthesis — Phase 5 Step 3.

For each module discovered by ``discover_modules``, compose a card that makes
the module a first-class retrieval unit:

  - ``summary``: 2-3 sentence description drawn from the longest available
    docstring among member chunks, falling back to a name + file-list blurb
  - ``entry_points``: JSON list of top ``_TOP_ENTRY_POINTS`` chunk ids,
    ranked by docstring length (proxy for "publicly documented, likely entry")
  - ``depends_on``: JSON list of other module_ids this module calls into,
    resolved via ``call_edges``
  - ``rationale``: deduplicated NOTE/WHY/TODO/FIXME lines extracted from
    docstrings (Phase 3 M10 already seeded them into the docstring field)

Synthesis is deterministic — no LLM involvement — which keeps the step
idempotent and offline-safe. If ``module.member_hash`` + the serialized
chunk-docstring hash are unchanged, synthesis is skipped, preserving a cheap
delta pass on re-index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from hybrid_search.storage.db import ModuleRecord, StoreDB

logger = logging.getLogger(__name__)

_TOP_ENTRY_POINTS = 5
_SUMMARY_MAX_CHARS = 480
_RATIONALE_TAG_RE = re.compile(
    r"(?mi)^\s*(?:#|//|/\*|\*)?\s*(NOTE|WHY|TODO|FIXME|HACK|XXX)\s*[:\-]\s*(.+?)\s*$"
)


def synthesize_module(db: StoreDB, module: ModuleRecord) -> tuple[ModuleRecord, bool]:
    """Populate summary/entry_points/depends_on/rationale for a module.

    Returns (updated_record, was_changed). Skips (returns unchanged) if the
    synthesis input hash already matches what's stored.
    """
    file_ids = db.get_files_by_module(module.id)
    if not file_ids:
        return module, False

    chunks = []
    for fid in file_ids:
        chunks.extend(db.get_chunks_by_file(fid))

    # Synthesis input hash — changes iff any member or chunk doc changes.
    inp_hash = _input_hash(module.member_hash, chunks)
    existing_tag = f"v1:{inp_hash}"
    if module.summary and module.summary.startswith(f"[hash:{existing_tag[:12]}]"):
        return module, False

    summary = _compose_summary(module.name, file_ids, chunks, db)
    entry_points = _pick_entry_points(chunks)
    depends_on = _resolve_depends(db, module.id, chunks)
    rationale = _extract_rationale(chunks)

    # Prefix summary with a hash marker so the skip check is cheap and
    # tolerates older rows that never saw Step 3.
    tagged_summary = f"[hash:{existing_tag[:12]}] {summary}" if summary else summary

    updated = ModuleRecord(
        id=module.id,
        project_id=module.project_id,
        name=module.name,
        summary=tagged_summary,
        entry_points=json.dumps(entry_points),
        depends_on=json.dumps(sorted(depends_on)),
        related_docs=module.related_docs,
        rationale=rationale,
        signals=module.signals,
        member_hash=module.member_hash,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    return updated, True


def synthesize_modules(db: StoreDB, project_id: str) -> dict:
    mods = db.get_modules(project_id)
    if not mods:
        return {"modules": 0, "synthesized": 0, "skipped": 0}

    synthesized = 0
    skipped = 0
    with db.transaction() as conn:
        for m in mods:
            updated, changed = synthesize_module(db, m)
            if changed:
                db.upsert_module(conn, updated)
                synthesized += 1
            else:
                skipped += 1
    return {
        "modules": len(mods),
        "synthesized": synthesized,
        "skipped": skipped,
    }


def _input_hash(member_hash: str, chunks: list) -> str:
    h = hashlib.sha256()
    h.update(member_hash.encode("utf-8"))
    h.update(b"\n")
    for c in chunks:
        h.update(c.id.encode("utf-8"))
        h.update(b"\n")
        if c.docstring:
            h.update(c.docstring.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def _compose_summary(name: str, file_ids: list[str], chunks: list, db: StoreDB) -> str:
    """Pick best docstring; fallback to name + filenames."""
    best_doc = ""
    for c in chunks:
        if c.docstring and len(c.docstring) > len(best_doc):
            best_doc = c.docstring
    file_names = []
    for fid in file_ids[:8]:
        fr = db.get_file(fid)
        if fr:
            file_names.append(fr.relative_path.rsplit("/", 1)[-1])
    files_blurb = ", ".join(file_names)
    if best_doc:
        head = best_doc.strip().split("\n\n", 1)[0][:_SUMMARY_MAX_CHARS]
        return f"Module `{name}` — {head}\n\nMembers: {files_blurb}"
    return f"Module `{name}` with {len(file_ids)} files: {files_blurb}"


def _pick_entry_points(chunks: list) -> list[str]:
    scored = sorted(
        chunks,
        key=lambda c: (
            -len(c.docstring or ""),
            -1 if c.node_type in ("function", "class", "method", "export") else 0,
            c.qualified_name or "",
        ),
    )
    return [c.id for c in scored[:_TOP_ENTRY_POINTS]]


def _resolve_depends(db: StoreDB, self_module_id: str, chunks: list) -> set[str]:
    """Other modules reached via call_edges from any chunk in this module."""
    deps: set[str] = set()
    for c in chunks:
        callees = db.get_callees(c.id, min_confidence="inferred")
        for row in callees:
            callee_id = row.get("callee_chunk_id")
            if not callee_id:
                continue
            callee_chunk = db.get_chunk(callee_id)
            if callee_chunk is None:
                continue
            for mid in db.get_modules_by_file(callee_chunk.file_id):
                if mid != self_module_id:
                    deps.add(mid)
    return deps


def _extract_rationale(chunks: list) -> str:
    seen: list[str] = []
    for c in chunks:
        if not c.docstring:
            continue
        for match in _RATIONALE_TAG_RE.finditer(c.docstring):
            tag = match.group(1).upper()
            body = match.group(2).strip()
            if not body:
                continue
            line = f"{tag}: {body}"
            if line not in seen:
                seen.append(line)
    return "\n".join(seen[:20]) if seen else ""
