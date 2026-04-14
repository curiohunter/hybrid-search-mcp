"""LLM-grounded wiki synthesis — Phase 9a.

Two-phase architecture for token-efficient synthesis:
  1. CLI `synthesize-wiki --prepare` → collects context from DB into files (zero tokens)
  2. Claude Code reads prepared context, writes synthesis (uses own LLM)
  3. CLI `synthesize-wiki --finalize` → verifies refs, merges, saves to DB (zero tokens)

No external API key needed — Claude Code IS the LLM.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from hybrid_search.storage.db import StoreDB

logger = logging.getLogger(__name__)

# Reference patterns: `file.py:L23`, `file.py:23`, `(file.py:L23)`
_REF_RE = re.compile(
    r"[`(]?"
    r"([\w./+-]+\.\w{1,10})"  # file path
    r":(?:L|line\s*)?(\d+)"   # line number
    r"[`)]?"
)

SYNTHESIS_INSTRUCTIONS = """\
You are a codebase documentation expert.
Based on the deterministic analysis (structural data) and actual source code below,
write documentation that helps developers understand this module.

Rules:
1. Never contradict the facts in the structural data
2. Never speculate about functionality not present in the code
3. Attach file:line citations to all claims (format: `path/to/file.py:L42`)
4. "Overview" explains why this module exists in one paragraph
5. "Key Design Decisions" covers only non-obvious choices (skip the obvious)
6. "Caveats" covers bug potential, edge cases, implicit dependencies
7. Write in the same language as the existing wiki page (Korean if Korean, English if English)
8. Use [[module-name]] wikilinks when referencing other modules

Output ONLY these sections (no title, no metadata):

## Overview
(one paragraph)

## Key Design Decisions
- **decision**: reason (`file:L##`)

## Data Flow
```
(ASCII diagram)
```

## Caveats
- issue description (`file:L##`)

## Related Modules
- [[module-name]] -- relationship description
"""


@dataclass
class SourceChunk:
    file_path: str
    name: str | None
    content: str
    start_line: int | None


@dataclass
class ModuleContext:
    module_name: str
    deterministic_wiki: str
    source_chunks: list[SourceChunk]
    related_summaries: list[str]
    file_paths: list[str]
    file_hashes: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    verified: list[str]
    failed: list[str]
    cleaned_content: str


def compute_synthesis_hash(deterministic_wiki: str, source_hashes: list[str]) -> str:
    """Hash of (deterministic wiki + sorted source file hashes) for change detection."""
    combined = deterministic_wiki + "\n" + "\n".join(sorted(source_hashes))
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def collect_module_context(
    db: StoreDB,
    project_id: str,
    module_name: str,
    project_path: str,
) -> ModuleContext | None:
    """Gather all context needed to synthesize a wiki page for a module."""
    from hybrid_search.storage.wiki import normalize_query, _page_id

    query_key = normalize_query(module_name.replace("-", " "))
    page_id = _page_id(project_id, query_key)

    row = db._conn.execute(
        "SELECT * FROM wiki_pages WHERE id = ?", (page_id,)
    ).fetchone()
    if row is None:
        row = db._conn.execute(
            "SELECT * FROM wiki_pages WHERE project_id = ? AND LOWER(title) LIKE ?",
            (project_id, f"%{module_name.lower()}%"),
        ).fetchone()
    if row is None:
        logger.warning("No wiki page found for module: %s", module_name)
        return None

    actual_page_id = row["id"]
    deterministic_wiki = row["content"]

    deps = db._conn.execute(
        """SELECT wd.file_id, wd.chunk_ids, f.relative_path, f.file_hash
           FROM wiki_dependencies wd
           JOIN files f ON f.id = wd.file_id
           WHERE wd.wiki_page_id = ?""",
        (actual_page_id,),
    ).fetchall()

    source_chunks: list[SourceChunk] = []
    file_paths: list[str] = []
    file_hashes: list[str] = []

    for dep in deps:
        file_paths.append(dep["relative_path"])
        file_hashes.append(dep["file_hash"])

        chunk_ids = json.loads(dep["chunk_ids"]) if dep["chunk_ids"] else []
        if chunk_ids:
            for cid in chunk_ids:
                chunk = db.get_chunk(cid)
                if chunk and chunk.content:
                    source_chunks.append(SourceChunk(
                        file_path=dep["relative_path"],
                        name=chunk.name,
                        content=chunk.content,
                        start_line=chunk.start_line,
                    ))
        else:
            all_chunks = db._conn.execute(
                "SELECT * FROM chunks WHERE file_id = ? ORDER BY start_line",
                (dep["file_id"],),
            ).fetchall()
            for c in all_chunks:
                if c["content"]:
                    source_chunks.append(SourceChunk(
                        file_path=dep["relative_path"],
                        name=c["name"],
                        content=c["content"],
                        start_line=c["start_line"],
                    ))

    related_summaries: list[str] = []
    links = db._conn.execute(
        """SELECT target_page_id FROM wiki_links WHERE source_page_id = ?
           UNION
           SELECT source_page_id FROM wiki_links WHERE target_page_id = ?""",
        (actual_page_id, actual_page_id),
    ).fetchall()

    for link in links[:5]:
        linked_row = db._conn.execute(
            "SELECT title, content FROM wiki_pages WHERE id = ?",
            (link[0],),
        ).fetchone()
        if linked_row:
            title = linked_row["title"]
            snippet = _extract_summary(linked_row["content"])
            related_summaries.append(f"[[{title}]]: {snippet}")

    return ModuleContext(
        module_name=module_name,
        deterministic_wiki=deterministic_wiki,
        source_chunks=source_chunks,
        related_summaries=related_summaries,
        file_paths=file_paths,
        file_hashes=file_hashes,
    )


def _extract_summary(content: str, max_len: int = 300) -> str:
    """Extract first meaningful paragraph from wiki content."""
    lines: list[str] = []
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(">"):
            if lines:
                break
            continue
        lines.append(stripped)
    text = " ".join(lines)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


# -- Phase 1: Prepare --

def prepare_context_file(ctx: ModuleContext, output_path: Path) -> Path:
    """Write module context to a markdown file for Claude Code to read.

    The file contains:
    1. Synthesis instructions
    2. Deterministic wiki (structural data)
    3. Source code chunks
    4. Related module summaries

    Claude Code reads this file, writes synthesis, saves to _synthesis_output/.
    """
    source_text = _format_source_chunks(ctx.source_chunks)
    related_text = "\n".join(ctx.related_summaries) if ctx.related_summaries else "(none)"

    content = f"""\
# Synthesis Context: {ctx.module_name}

> **Instructions**: Read the context below and write a wiki synthesis.
> Save your output to the corresponding `_synthesis_output/{output_path.stem}.md` file.
> Write ONLY the synthesis sections (## Overview, ## Key Design Decisions, etc.)
> Do NOT include the title or metadata line.

{SYNTHESIS_INSTRUCTIONS}

---

## Deterministic Wiki (structural data — DO NOT contradict)

{ctx.deterministic_wiki}

---

## Source Code

{source_text}

---

## Related Module Summaries

{related_text}

---

## Metadata (do not include in output)

- input_hash: {compute_synthesis_hash(ctx.deterministic_wiki, ctx.file_hashes)}
- files: {len(ctx.file_paths)}
- chunks: {len(ctx.source_chunks)}
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


# -- Phase 2: Finalize --

def verify_references(content: str, project_path: str) -> VerificationResult:
    """Verify that file:line references in synthesized content actually exist."""
    verified: list[str] = []
    failed: list[str] = []
    project = Path(project_path)

    for match in _REF_RE.finditer(content):
        file_path = match.group(1)
        line_num = int(match.group(2))
        ref_str = f"{file_path}:L{line_num}"

        full_path = project / file_path
        if full_path.exists():
            try:
                line_count = sum(1 for _ in full_path.open())
                if line_num <= line_count:
                    verified.append(ref_str)
                else:
                    failed.append(ref_str)
            except (OSError, UnicodeDecodeError):
                failed.append(ref_str)
        else:
            failed.append(ref_str)

    cleaned_lines: list[str] = []
    for line in content.split("\n"):
        has_failed = False
        for ref in failed:
            file_part = ref.split(":")[0]
            if file_part in line and f":{ref.split(':')[1]}" in line:
                has_failed = True
                break
        if not has_failed:
            cleaned_lines.append(line)

    return VerificationResult(
        verified=verified,
        failed=failed,
        cleaned_content="\n".join(cleaned_lines),
    )


def merge_synthesis_with_structure(
    synthesis_content: str,
    deterministic_wiki: str,
    module_name: str,
) -> str:
    """Merge LLM synthesis (top) with deterministic structure (<details> bottom)."""
    title = module_name
    for line in deterministic_wiki.split("\n"):
        if line.startswith("# "):
            title = line[2:].strip()
            break

    meta_line = ""
    for line in deterministic_wiki.split("\n"):
        if line.startswith("> "):
            meta_line = line
            break

    from datetime import datetime, timezone
    synth_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Strip any leading # title from synthesis_content
    synth_lines = synthesis_content.split("\n")
    if synth_lines and synth_lines[0].startswith("# "):
        synth_lines = synth_lines[1:]
    synthesis_body = "\n".join(synth_lines).strip()

    # Extract structural content (skip title and meta lines)
    det_lines = deterministic_wiki.split("\n")
    struct_start = 0
    for i, line in enumerate(det_lines):
        if line.startswith("## "):
            struct_start = i
            break
    structural_content = "\n".join(det_lines[struct_start:]) if struct_start > 0 else deterministic_wiki

    parts = [
        f"# {title}",
        f"{meta_line} | synthesized: {synth_date}" if meta_line else f"> synthesized: {synth_date}",
        "",
        synthesis_body,
        "",
        "<details>",
        "<summary>Structure (auto-generated)</summary>",
        "",
        structural_content,
        "</details>",
    ]

    return "\n".join(parts)


def finalize_module(
    db: StoreDB,
    project_id: str,
    module_name: str,
    synthesis_content: str,
    project_path: str,
    wiki_dir: Path,
) -> dict:
    """Finalize a synthesized module: verify refs, merge, backup raw, save to DB.

    Returns summary dict with stats.
    """
    from hybrid_search.storage.wiki import normalize_query, _page_id

    query_key = normalize_query(module_name.replace("-", " "))
    page_id = _page_id(project_id, query_key)

    # Get current deterministic wiki
    row = db._conn.execute(
        "SELECT content FROM wiki_pages WHERE id = ?", (page_id,)
    ).fetchone()
    if row is None:
        # Try title match
        row = db._conn.execute(
            "SELECT id, content FROM wiki_pages WHERE project_id = ? AND LOWER(title) LIKE ?",
            (project_id, f"%{module_name.lower()}%"),
        ).fetchone()
    if row is None:
        return {"error": f"No wiki page found for {module_name}"}

    deterministic_wiki = row["content"]

    # Get file hashes for synthesis_hash
    deps = db._conn.execute(
        """SELECT f.file_hash FROM wiki_dependencies wd
           JOIN files f ON f.id = wd.file_id
           WHERE wd.wiki_page_id = ?""",
        (page_id,),
    ).fetchall()
    file_hashes = [d["file_hash"] for d in deps]
    input_hash = compute_synthesis_hash(deterministic_wiki, file_hashes)

    # Verify references
    verification = verify_references(synthesis_content, project_path)

    # Merge
    merged = merge_synthesis_with_structure(
        verification.cleaned_content, deterministic_wiki, module_name,
    )

    # Backup raw
    raw_dir = wiki_dir / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    slug = module_name.lower().replace(" ", "-")
    raw_path = raw_dir / f"{slug}.raw.md"
    raw_path.write_text(deterministic_wiki, encoding="utf-8")

    # Write merged wiki
    wiki_path = wiki_dir / f"{slug}.md"
    wiki_path.write_text(merged, encoding="utf-8")

    # Resolve file deps from merged content for DB
    file_deps = _resolve_file_deps(db, project_id, merged)

    # Save to DB
    wiki_store = db.wiki_store()
    with db.transaction():
        wiki_store.compile_page(
            project_id=project_id,
            query=module_name.replace("-", " "),
            title=module_name,
            content=merged,
            tags=[slug, "synthesized"],
            file_dependencies=file_deps,
            synthesis_model="claude-code",
            synthesis_hash=input_hash,
        )

    return {
        "module": module_name,
        "verified_refs": len(verification.verified),
        "failed_refs": len(verification.failed),
        "wiki_path": str(wiki_path),
        "raw_path": str(raw_path),
    }


def _resolve_file_deps(db: StoreDB, project_id: str, content: str) -> list[dict]:
    """Find files referenced in content (backtick paths) and snapshot hashes."""
    path_pattern = re.compile(r"`([a-zA-Z0-9_./-]+\.[a-zA-Z]{1,10})`")
    referenced_paths = set(path_pattern.findall(content))

    file_deps: list[dict] = []
    seen_ids: set[str] = set()
    for ref_path in referenced_paths:
        file_rec = db.get_file_by_path(project_id, ref_path)
        if file_rec and file_rec.id not in seen_ids:
            file_deps.append({
                "file_id": file_rec.id,
                "file_hash": file_rec.file_hash,
                "chunk_ids": [],
            })
            seen_ids.add(file_rec.id)
    return file_deps


# -- Utilities --

def _format_source_chunks(
    chunks: list[SourceChunk], max_chars: int = 30000
) -> str:
    """Format source chunks into a string, respecting character budget."""
    max_chars_budget = max_chars * 4
    parts: list[str] = []
    total = 0

    for chunk in chunks:
        header = f"--- {chunk.file_path}"
        if chunk.name:
            header += f" :: {chunk.name}"
        if chunk.start_line:
            header += f" (L{chunk.start_line})"
        header += " ---"

        entry = f"{header}\n{chunk.content}\n"
        if total + len(entry) > max_chars_budget:
            parts.append(f"\n... ({len(chunks) - len(parts)} more chunks truncated)")
            break
        parts.append(entry)
        total += len(entry)

    return "\n".join(parts) if parts else "(no source code available)"


def estimate_tokens(ctx: ModuleContext) -> dict:
    """Estimate context size for a module (dry-run info)."""
    source_text = _format_source_chunks(ctx.source_chunks)
    total_chars = (
        len(SYNTHESIS_INSTRUCTIONS)
        + len(ctx.deterministic_wiki)
        + len(source_text)
        + sum(len(s) for s in ctx.related_summaries)
    )
    input_tokens = total_chars // 4

    return {
        "module": ctx.module_name,
        "input_tokens": input_tokens,
        "source_chunks": len(ctx.source_chunks),
        "files": len(ctx.file_paths),
    }
