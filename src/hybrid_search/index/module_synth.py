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
from typing import TYPE_CHECKING

import numpy as np

from hybrid_search.storage.db import ModuleRecord, StoreDB

if TYPE_CHECKING:
    from hybrid_search.index.embedder import Embedder

logger = logging.getLogger(__name__)

_TOP_ENTRY_POINTS = 5
_SUMMARY_MAX_CHARS = 480
# Per-doc excerpt cap. Two-ish short doc excerpts + a docstring should fit in
# ~1.2 KB of card text — still well under what a single chunk snippet would
# cost the agent to read.
_DOC_EXCERPT_MAX_CHARS = 320
_DOC_EXCERPT_TOP_N = 2
_RATIONALE_TAG_RE = re.compile(
    r"(?mi)^\s*(?:#|//|/\*|\*)?\s*(NOTE|WHY|TODO|FIXME|HACK|XXX)\s*[:\-]\s*(.+?)\s*$"
)
# Markdown node_types produced by doc_chunker. Kept as a module-level constant
# so _compose_summary and tests share the definition.
_DOC_NODE_TYPES = frozenset({"section", "block", "qa_log"})


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


def synthesize_modules(
    db: StoreDB,
    project_id: str,
    embedder: "Embedder | None" = None,
) -> dict:
    """Synthesize text cards, then optionally embed them (Phase 5 Step C).

    The embedding pass is separate from the text-synth pass and uses its own
    per-module hash (``vector_input_hash``) so an existing card whose text
    didn't change this run still gets a vector on first Step-C rollout.
    ``embedder=None`` preserves the old behavior for tests/offline callers.
    """
    mods = db.get_modules(project_id)
    if not mods:
        return {"modules": 0, "synthesized": 0, "skipped": 0, "embedded": 0}

    synthesized = 0
    skipped = 0
    post: dict[str, ModuleRecord] = {}
    with db.transaction() as conn:
        for m in mods:
            updated, changed = synthesize_module(db, m)
            if changed:
                db.upsert_module(conn, updated)
                synthesized += 1
                post[m.id] = updated
            else:
                skipped += 1
                post[m.id] = m

    embedded = _embed_modules(db, post, embedder) if embedder is not None else 0
    return {
        "modules": len(mods),
        "synthesized": synthesized,
        "skipped": skipped,
        "embedded": embedded,
    }


def _embed_modules(
    db: StoreDB,
    records: dict[str, ModuleRecord],
    embedder: "Embedder",
) -> int:
    """Batch-embed cards whose text differs from the stored vector fingerprint.

    Returns the count of modules (re-)embedded this pass. Non-fatal on
    embedder errors — synthesis already succeeded; vectors are additive.
    """
    pending: list[tuple[str, str, str]] = []  # (module_id, text, input_hash)
    for mid, m in records.items():
        text = vector_input_text(m)
        if not text:
            continue
        inp_hash = _vector_input_hash(text)
        if m.vector_input_hash == inp_hash and m.summary_vector:
            continue
        pending.append((mid, text, inp_hash))

    if not pending:
        return 0

    try:
        vectors = embedder.embed_texts([t for _, t, _ in pending])
    except Exception as e:  # noqa: BLE001 — embedding is non-fatal
        logger.warning("Module embedding failed (non-fatal): %s", e)
        return 0

    written = 0
    with db.transaction() as conn:
        for (mid, _text, inp_hash), vec in zip(pending, vectors):
            blob = np.asarray(vec, dtype=np.float32).tobytes()
            db.update_module_vector(conn, mid, blob, inp_hash)
            written += 1
    return written


def vector_input_text(m: ModuleRecord) -> str:
    """Canonical text the embedding is computed over. Drops the [hash:…]
    summary prefix so embedding input is stable across synth re-runs."""
    parts: list[str] = []
    if m.name:
        parts.append(m.name)
    if m.summary:
        summ = m.summary
        if summ.startswith("[hash:"):
            end = summ.find("]")
            if end != -1:
                summ = summ[end + 1:].strip()
        if summ:
            parts.append(summ)
    if m.rationale:
        parts.append(m.rationale)
    return "\n".join(parts)


def _vector_input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
    """Compose a module summary from two independent sources:

    1. **Related-doc excerpts** — the leading paragraph of the top-N largest
       markdown section chunks that belong to this module. This is what
       Step F adds: S2/S5 failure modes traced back to module cards that
       talked only about code identifiers ("portal shell rendering") while
       the feature doc said "학부모/parent" — now both live in the card.
    2. **Best code docstring** — longest non-doc docstring among member
       chunks, the previous sole summary source.

    Both streams are trimmed and joined so downstream search (token overlap
    *and* vector cosine) sees domain language alongside implementation terms.
    """
    doc_excerpts = _collect_doc_excerpts(chunks)
    best_doc = ""
    for c in chunks:
        if c.node_type in _DOC_NODE_TYPES:
            continue
        if c.docstring and len(c.docstring) > len(best_doc):
            best_doc = c.docstring
    file_names = []
    for fid in file_ids[:8]:
        fr = db.get_file(fid)
        if fr:
            file_names.append(fr.relative_path.rsplit("/", 1)[-1])
    files_blurb = ", ".join(file_names)

    body_parts: list[str] = []
    if doc_excerpts:
        body_parts.append("Docs:\n" + "\n---\n".join(doc_excerpts))
    if best_doc:
        head = best_doc.strip().split("\n\n", 1)[0][:_SUMMARY_MAX_CHARS]
        body_parts.append(f"Code: {head}")

    if body_parts:
        body = "\n\n".join(body_parts)
        return f"Module `{name}` — {body}\n\nMembers: {files_blurb}"
    return f"Module `{name}` with {len(file_ids)} files: {files_blurb}"


def _collect_doc_excerpts(chunks: list) -> list[str]:
    """Pull the leading paragraph from up to ``_DOC_EXCERPT_TOP_N`` doc
    sections, picked by content length as a proxy for "substantive section".

    Strips trailing whitespace and caps each excerpt at
    ``_DOC_EXCERPT_MAX_CHARS`` so the composite card stays compact. Skips
    qa_log chunks — those are search artifacts, not feature documentation.
    """
    doc_chunks = [
        c for c in chunks
        if c.node_type in _DOC_NODE_TYPES
        and c.node_type != "qa_log"
        and (c.content or "").strip()
    ]
    if not doc_chunks:
        return []
    doc_chunks.sort(key=lambda c: -len(c.content or ""))
    excerpts: list[str] = []
    seen_heads: set[str] = set()
    for c in doc_chunks[: _DOC_EXCERPT_TOP_N * 3]:  # scan a small window
        head = (c.content or "").strip().split("\n\n", 1)[0]
        head = head[:_DOC_EXCERPT_MAX_CHARS].strip()
        if not head or head in seen_heads:
            continue
        seen_heads.add(head)
        excerpts.append(head)
        if len(excerpts) >= _DOC_EXCERPT_TOP_N:
            break
    return excerpts


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
