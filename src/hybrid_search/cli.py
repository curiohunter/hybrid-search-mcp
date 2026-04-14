"""CLI entrypoint for background indexing (git hook, no MCP overhead).

Usage:
    python -m hybrid_search.cli reindex [--cwd PATH] [--force]
    python -m hybrid_search.cli status
    python -m hybrid_search.cli stale [--cwd PATH]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from hybrid_search.config import load_config
from hybrid_search.index.dag import generate_all_wiki_pages
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.index.scanner import get_changed_files_from_git
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger("hybrid_search.cli")


def _detect_project(registry: ProjectRegistry, cwd: str) -> tuple[str, str] | None:
    """Find registered project matching cwd. Returns (name, path) or None.

    Picks the most specific (longest path) match to avoid matching a parent
    project (e.g. home dir) when a deeper subdirectory is the real target.
    Skips home directory registrations.
    """
    cwd_path = Path(cwd).resolve()
    home = Path.home().resolve()
    best: tuple[str, str] | None = None
    best_depth = -1

    for pinfo in registry.list_all():
        project_path = Path(pinfo.path).resolve()
        if project_path == home:
            continue  # skip home directory
        try:
            cwd_path.relative_to(project_path)
            depth = len(project_path.parts)
            if depth > best_depth:
                best = (pinfo.name, pinfo.path)
                best_depth = depth
        except ValueError:
            pass
    return best


_CLAUDE_MD_MARKER = "<!-- hybrid-search -->"

_CLAUDE_MD_SECTION = """<!-- hybrid-search -->
## 검색 전략 — 의도 기반 라우팅

고정 순서가 아니라 **질문 유형에 따라 1차 도구를 선택**하고, 부족하면 fallback으로 보충한다.

| 질문 유형 | 신호 | 1차 | fallback |
|-----------|------|-----|----------|
| 구조/관계 | "누가 호출", 의존, 모듈 구조, 전체 그림 | Wiki | hybrid_search |
| 기능 탐색 | 자연어, 한국어, 넓은 기능 질문 | hybrid_search | Wiki |
| 정밀 조회 | 정확한 심볼명, 파일명, 에러 문자열 | Grep | Read |
| 설계/맥락 | "왜 이렇게", QA 히스토리, 계획 문서 | hybrid_search | Wiki |
| 스키마/DB | 마이그레이션, DDL, 테이블 구조 변화 | hybrid_search (node_types/file_pattern 활용) | Grep |

**운영 규칙**:
- 1차에서 답이 부족하면 도구를 **바꾸지 말고 보충**한다 (hybrid→wiki, wiki→hybrid, grep→read)
- Wiki는 `.hybrid-search/wiki/index.md`에서 시작. `[[링크]]`가 있으면 따라갈 것
- hybrid_search는 한국어 자연어 질의 + 코드/문서/계획 문서 크로스 도메인 검색이 강점
"""


def _ensure_claude_md(project_path: str) -> None:
    """Add hybrid-search section to CLAUDE.md. Inserts at TOP for visibility."""
    claude_md = Path(project_path) / "CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        if _CLAUDE_MD_MARKER in content:
            return  # already patched
        # Insert at TOP (after first heading if exists)
        lines = content.split("\n")
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith("# "):
                insert_at = i + 1
                # Skip blank lines after heading
                while insert_at < len(lines) and not lines[insert_at].strip():
                    insert_at += 1
                break
        lines.insert(insert_at, _CLAUDE_MD_SECTION)
        claude_md.write_text("\n".join(lines), encoding="utf-8")
        print(f"CLAUDE.md: hybrid-search section added (top)")
    else:
        # Create new CLAUDE.md
        claude_md.write_text(_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")
        print(f"CLAUDE.md: created with hybrid-search instructions")


def _write_gap_flag(cwd: str, files_added: int) -> None:
    """Write wiki-gaps flag file when new files were indexed."""
    if files_added <= 0:
        return
    gap_file = Path(cwd) / ".hybrid-search" / "wiki-gaps.txt"
    gap_file.parent.mkdir(parents=True, exist_ok=True)
    # Append timestamp + count (lightweight flag, not file list)
    import datetime
    ts = datetime.datetime.now().isoformat()
    with open(gap_file, "a") as f:
        f.write(f"{ts}: {files_added} new files indexed\n")
    print(f"Wiki gaps: {files_added} new files flagged → {gap_file}")


def cmd_reindex(args: argparse.Namespace) -> None:
    """Delta reindex the project at cwd."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)

    if match:
        name, path = match
        project_path = path
        project_name = name
    else:
        # Not registered yet — register using cwd
        project_path = cwd
        project_name = Path(cwd).name

    embedder = Embedder(config.embedding, config.models_dir)
    pipeline = IndexingPipeline(config, registry, embedder)
    changed_paths: list[str] | None = None
    deleted_paths: list[str] | None = None

    if getattr(args, "git_delta", False) and not args.force:
        diff = get_changed_files_from_git(Path(project_path))
        if diff is not None:
            changed_paths = list(dict.fromkeys(diff.added + diff.modified))
            deleted_paths = list(dict.fromkeys(diff.deleted))
            if not changed_paths and not deleted_paths:
                print("Git delta: no changed files detected, skipping reindex.")
                return
            print(
                "Git delta:"
                f" {len(diff.added)} added,"
                f" {len(diff.modified)} modified,"
                f" {len(diff.deleted)} deleted"
            )
        else:
            print("Git delta unavailable, falling back to full scan.")

    start = time.monotonic()
    print(f"Reindexing: {project_name} ({project_path})")

    def progress(current: int, total: int, path: str) -> None:
        if total > 0 and current % 50 == 0:
            print(f"  [{current}/{total}] {path}")

    result = pipeline.index_project(
        project_path,
        project_name,
        force=args.force,
        changed_paths=changed_paths,
        deleted_paths=deleted_paths,
        on_progress=progress,
    )

    elapsed = time.monotonic() - start
    print(
        f"Done: +{result.files_added} added, "
        f"~{result.files_changed} changed, "
        f"-{result.files_deleted} deleted, "
        f"{result.chunks_total} chunks, "
        f"{elapsed:.1f}s"
    )

    # Stale wiki marking
    if result.files_changed > 0 or result.files_deleted > 0:
        _mark_stale_wikis(config, registry, project_name)

    # Call graph re-resolution after reindex
    pinfo = registry.get_by_name(project_name)
    if pinfo:
        from hybrid_search.index.callgraph import resolve_call_edges
        p_dir = get_project_dir(config.projects_dir, pinfo.id)
        p_idx = IndexPaths(p_dir)
        if p_idx.store_db.exists():
            db = StoreDB(p_idx.store_db)
            try:
                stats = resolve_call_edges(db, pinfo.id)
                resolved = stats["high"] + stats["medium"]
                print(f"Call graph: {resolved} resolved ({stats['high']}H + {stats['medium']}M), {stats['unresolved']} unresolved")
            finally:
                db.close()

    # --synthesize implies --wiki (wiki must exist before synthesis)
    do_wiki = getattr(args, "wiki", False) or getattr(args, "synthesize", False)

    wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
    wiki_exists = wiki_dir.exists() and any(wiki_dir.glob("*.md"))

    if getattr(args, "wiki_scope", "full") == "affected" and wiki_exists:
        if changed_paths or deleted_paths:
            regen_count = _regenerate_affected_wiki_pages(
                config,
                registry,
                project_name,
                project_path,
                changed_paths or [],
                deleted_paths or [],
            )
            if regen_count == 0:
                print("Wiki: no affected pages found, syncing existing wiki to DB...")
                import argparse as _ap
                _sync_args = _ap.Namespace(cwd=project_path)
                cmd_sync_wiki(_sync_args)
    elif do_wiki or not wiki_exists:
        # Generate/regenerate wiki from module tree
        print("Generating wiki from module tree...")
        import argparse as _ap
        _wiki_args = _ap.Namespace(cwd=project_path)
        cmd_generate_wiki(_wiki_args)
    elif wiki_exists:
        # Just sync existing wiki to DB
        print("Auto-syncing wiki to DB...")
        import argparse as _ap
        _sync_args = _ap.Namespace(cwd=project_path)
        cmd_sync_wiki(_sync_args)

    # Re-check staleness after sync (cleans up STALE.md when all pages are fresh)
    _mark_stale_wikis(config, registry, project_name)

    # Auto-prepare synthesis for stale modules
    if getattr(args, "synthesize", False):
        _auto_prepare_synthesis(config, registry, project_name, project_path)

    # Auto-patch CLAUDE.md with search instructions (once per project)
    _ensure_claude_md(project_path)

    # Auto-install post-commit hook (once per project)
    hook_path = Path(project_path) / ".git" / "hooks" / "post-commit"
    if not hook_path.exists() or "hybrid_search.cli" not in hook_path.read_text():
        import argparse as _ap
        _hook_args = _ap.Namespace(cwd=project_path)
        cmd_install_hook(_hook_args)

    # Gap detection for new files
    _write_gap_flag(cwd, result.files_added)

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"  {err}")


def _mark_stale_wikis(config, registry: ProjectRegistry, project_name: str) -> None:
    """Mark wiki pages as stale and write STALE.md for Claude auto-refresh."""
    pinfo = registry.get_by_name(project_name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        stale_pages = wiki.check_staleness(pinfo.id)
        stale_items = [p for p in stale_pages if p["stale"]]

        # Find project root for writing STALE.md
        project_path = pinfo.path if hasattr(pinfo, "path") else None
        if not project_path:
            for p in registry.list_all():
                if p.name == project_name:
                    project_path = p.path
                    break

        if stale_items and project_path:
            stale_md_path = Path(project_path) / ".hybrid-search" / "wiki" / "STALE.md"
            lines = [
                "# Stale Wiki Pages",
                "",
                "> 이 파일은 자동 생성됩니다. 아래 페이지들의 소스 코드가 변경되었습니다.",
                "> 각 페이지를 읽고, 변경된 소스 파일을 확인한 후, wiki 내용을 갱신하세요.",
                "> 모든 페이지를 갱신하면 이 파일을 삭제하세요.",
                "",
            ]
            for p in stale_items:
                changed = ", ".join(p["changed_files"][:5]) if p["changed_files"] else "unknown"
                lines.append(f"- **{p['title']}** (page_id: `{p['page_id']}`)")
                lines.append(f"  - 변경된 파일: {changed}")
            lines.append("")

            stale_md_path.parent.mkdir(parents=True, exist_ok=True)
            stale_md_path.write_text("\n".join(lines))
            print(f"Wiki: {len(stale_items)} stale page(s) → STALE.md written")
        elif not stale_items and project_path:
            # Remove STALE.md if no stale pages
            stale_md_path = Path(project_path) / ".hybrid-search" / "wiki" / "STALE.md"
            if stale_md_path.exists():
                stale_md_path.unlink()
                print("Wiki: all pages fresh, STALE.md removed")
        else:
            stale_count = len(stale_items)
            if stale_count > 0:
                print(f"Wiki: {stale_count} stale page(s) detected")
    finally:
        db.close()


def _regenerate_affected_wiki_pages(
    config,
    registry: ProjectRegistry,
    project_name: str,
    project_path: str,
    changed_paths: list[str],
    deleted_paths: list[str],
) -> int:
    """Regenerate only wiki pages affected by changed/deleted files."""
    pinfo = registry.get_by_name(project_name)
    if not pinfo:
        return 0

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        return 0

    db = StoreDB(idx_paths.store_db)
    try:
        plan, pages = generate_all_wiki_pages(db, pinfo.id)
        if not pages:
            return 0

        changed_set = set(changed_paths) | set(deleted_paths)
        selected_pages = []
        selected_names: set[str] = {"index"}

        for page in pages:
            page_paths = {
                db.get_file(fid).relative_path
                for fid in page.file_ids
                if db.get_file(fid) is not None
            }
            if page_paths & changed_set:
                selected_pages.append(page)
                selected_names.add(page.name)

        if not selected_pages:
            return 0

        # Also refresh pages linked from directly affected pages to keep module map coherent.
        extra_pages = [
            page for page in pages
            if page.name not in selected_names and any(f"[[{page.name}]]" in p.content for p in selected_pages)
        ]
        selected_pages.extend(extra_pages)

        wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)

        page_map = {page.name: page for page in pages}
        written = 0

        index_page = page_map.get("index")
        if index_page is not None:
            (wiki_dir / index_page.filename).write_text(index_page.content, encoding="utf-8")
            written += 1

        wiki_store = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        for page in selected_pages:
            page_path = wiki_dir / page.filename
            page_path.write_text(page.content, encoding="utf-8")

            file_deps = []
            seen_file_ids: set[str] = set()
            for fid in page.file_ids:
                if fid in seen_file_ids:
                    continue
                seen_file_ids.add(fid)
                file_rec = db.get_file(fid)
                if file_rec:
                    file_deps.append({
                        "file_id": fid,
                        "file_hash": file_rec.file_hash,
                        "chunk_ids": [cid for cid in page.chunk_ids],
                    })

            with db.transaction():
                wiki_store.compile_page(
                    project_id=pinfo.id,
                    query=page.name.replace("-", " "),
                    title=page.title,
                    content=page.content,
                    tags=page.tags,
                    file_dependencies=file_deps,
                )
            written += 1

        print(
            f"Wiki: regenerated {written} page(s) "
            f"for {len(changed_set)} changed/deleted file(s), "
            f"coverage {plan.covered_chunks}/{plan.total_chunks}"
        )
        return written
    finally:
        db.close()


def _auto_prepare_synthesis(
    config, registry: ProjectRegistry, project_name: str, project_path: str
) -> None:
    """Auto-prepare synthesis context for stale modules after reindex.

    Only prepares modules whose synthesis_hash has actually changed
    (skips modules where file changes didn't affect the deterministic wiki).
    """
    from hybrid_search.index.synthesizer import (
        collect_module_context,
        prepare_context_file,
        should_skip_synthesis,
    )

    pinfo = registry.get_by_name(project_name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki_store = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        staleness = wiki_store.check_staleness(pinfo.id)
        stale_pages = [p for p in staleness if p["stale"]]

        if not stale_pages:
            return

        wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
        input_dir = wiki_dir / "_synthesis_input"
        output_dir = wiki_dir / "_synthesis_output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        prepared = 0
        skipped = 0
        for page in stale_pages:
            mod_name = page["title"]
            skip, reason = should_skip_synthesis(
                db, pinfo.id, mod_name, project_path,
            )
            if skip:
                skipped += 1
                continue

            ctx = collect_module_context(db, pinfo.id, mod_name, project_path)
            if not ctx:
                continue

            slug = mod_name.lower().replace(" ", "-")
            out_path = input_dir / f"{slug}.md"
            prepare_context_file(ctx, out_path)
            prepared += 1

        # Indirect impact propagation: find linked modules that need
        # "Related Modules" section refresh due to stale neighbors
        stale_ids = [p["page_id"] for p in stale_pages]
        indirect = wiki_store.find_indirectly_affected(pinfo.id, stale_ids)
        indirect_prepared = 0
        for page_info in indirect:
            mod_name = page_info["title"]
            skip, reason = should_skip_synthesis(
                db, pinfo.id, mod_name, project_path,
            )
            if skip:
                continue

            ctx = collect_module_context(db, pinfo.id, mod_name, project_path)
            if not ctx:
                continue

            slug = mod_name.lower().replace(" ", "-")
            out_path = input_dir / f"{slug}.md"
            if not out_path.exists():  # don't overwrite direct stale prepare
                prepare_context_file(ctx, out_path)
                indirect_prepared += 1

        if prepared > 0 or indirect_prepared > 0:
            total = prepared + indirect_prepared
            print(
                f"Synthesis: {total} module(s) prepared in {input_dir}"
                f" ({prepared} stale + {indirect_prepared} indirect)"
            )
            if skipped > 0:
                print(f"  ({skipped} skipped — inputs unchanged)")
            print(
                f"  Next: Read context files → write synthesis to {output_dir}/ "
                f"→ run synthesize-wiki --finalize --cwd {project_path}"
            )
        elif skipped > 0:
            print(f"Synthesis: all {skipped} stale module(s) have unchanged inputs — nothing to re-synthesize")
    finally:
        db.close()


def cmd_status(args: argparse.Namespace) -> None:
    """Show index status for all projects."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    projects = registry.list_all()
    if not projects:
        print("No indexed projects.")
        return

    for p in projects:
        print(f"  {p.name}: {p.file_count} files, {p.chunk_count} chunks")
        if p.last_indexed_at:
            print(f"    Last indexed: {p.last_indexed_at}")
        print(f"    Path: {p.path}")


def cmd_stale(args: argparse.Namespace) -> None:
    """Check wiki staleness for a project."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, _ = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        pages = wiki.check_staleness(pinfo.id)
        if not pages:
            print("No wiki pages.")
            return
        for p in pages:
            status = "STALE" if p["stale"] else "OK"
            print(f"  [{status}] {p['title']} ({p['total_dependencies']} deps)")
            if p["stale"] and p["changed_files"]:
                for f in p["changed_files"][:3]:
                    print(f"    Changed: {f}")
    finally:
        db.close()


def cmd_sync_wiki(args: argparse.Namespace) -> None:
    """Sync disk wiki files to DB for staleness tracking."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, project_path = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    # Find wiki files on disk
    wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
    if not wiki_dir.exists():
        print(f"No wiki directory found: {wiki_dir}")
        return

    wiki_files = sorted(wiki_dir.glob("*.md"))
    wiki_files = [f for f in wiki_files if f.name != "index.md"]

    if not wiki_files:
        print("No wiki pages found (only index.md).")
        return

    # Open DB
    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki_store = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        synced = 0

        for wiki_file in wiki_files:
            content = wiki_file.read_text()
            title = _extract_title(content)
            tags = _extract_tags(wiki_file.stem, content)

            # Extract referenced file paths from wiki content
            file_deps = _resolve_wiki_deps(db, pinfo.id, content)

            with db.transaction():
                wiki_store.compile_page(
                    project_id=pinfo.id,
                    query=wiki_file.stem.replace("-", " "),
                    title=title,
                    content=content,
                    tags=tags,
                    file_dependencies=file_deps,
                )
            synced += 1
            dep_count = len(file_deps)
            print(f"  Synced: {wiki_file.name} ({title}, {dep_count} deps)")

        print(f"Done: {synced} wiki pages synced to DB.")
    finally:
        db.close()


def _extract_title(content: str) -> str:
    """Extract title from first # heading."""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
    return "Untitled"


def _extract_tags(stem: str, content: str) -> list[str]:
    """Generate tags from filename stem and content headings."""
    tags = [stem]
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("## ") and line != "## 개요":
            tag = line[3:].strip().lower().replace(" ", "-")
            if len(tag) < 30:
                tags.append(tag)
    return tags[:10]


def _resolve_wiki_deps(db: StoreDB, project_id: str, content: str) -> list[dict]:
    """Find files referenced in wiki content (backtick paths) and snapshot their hashes."""
    import re

    # Match paths in backticks like `path/to/file.ts`
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


def cmd_call_graph_stats(args: argparse.Namespace) -> None:
    """Show call graph resolution statistics for a project."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, _ = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        edges = db.get_all_call_edges(pinfo.id)
        total = len(edges)
        if total == 0:
            print(f"Project: {name} — no call edges found.")
            return

        high = sum(1 for e in edges if e["confidence"] == "high")
        medium = sum(1 for e in edges if e["confidence"] == "medium")
        resolved = sum(1 for e in edges if e["callee_chunk_id"] is not None)
        unresolved = total - resolved

        # Module-linked edges: import-call binding success (project-internal candidates)
        with_mod = [e for e in edges if e.get("callee_module")]
        mod_resolved = sum(1 for e in with_mod if e["callee_chunk_id"])
        without_mod = [e for e in edges if not e.get("callee_module")]
        nomod_resolved = sum(1 for e in without_mod if e["callee_chunk_id"])

        # "Project dependency edges" = High + Medium (useful for CodeWiki / topo sort)
        project_deps = high + medium

        print(f"Project: {name}")
        print(f"  Total edges:       {total}")
        print(f"  Project deps:      {project_deps} (High {high} + Medium {medium})")
        print(f"  All resolved:      {resolved}/{total} ({resolved/total*100:.1f}%)")
        print(f"  With module:       {len(with_mod)} → resolved {mod_resolved} ({mod_resolved/max(len(with_mod),1)*100:.1f}%)")
        print(f"  Without module:    {len(without_mod)} → resolved {nomod_resolved} ({nomod_resolved/max(len(without_mod),1)*100:.1f}%)")
    finally:
        db.close()


def cmd_generate_wiki(args: argparse.Namespace) -> None:
    """Generate wiki pages from module tree and sync to DB."""
    from hybrid_search.index.dag import generate_all_wiki_pages

    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, project_path = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        print(f"Generating wiki for: {name}")
        plan, pages = generate_all_wiki_pages(db, pinfo.id)

        if not pages:
            print("No modules found. Run reindex first.")
            return

        # Write to disk
        wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for page in pages:
            page_path = wiki_dir / page.filename
            page_path.write_text(page.content, encoding="utf-8")
            written += 1

        print(f"  Wrote {written} pages to {wiki_dir}")

        # Sync to DB
        wiki_store = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        synced = 0
        for page in pages:
            if page.name == "index":
                continue  # index.md is disk-only

            # Build file dependencies for staleness tracking
            file_deps = []
            seen_file_ids: set[str] = set()
            for fid in page.file_ids:
                if fid in seen_file_ids:
                    continue
                seen_file_ids.add(fid)
                file_rec = db.get_file(fid)
                if file_rec:
                    file_deps.append({
                        "file_id": fid,
                        "file_hash": file_rec.file_hash,
                        "chunk_ids": [cid for cid in page.chunk_ids],
                    })

            with db.transaction():
                wiki_store.compile_page(
                    project_id=pinfo.id,
                    query=page.name.replace("-", " "),
                    title=page.title,
                    content=page.content,
                    tags=page.tags,
                    file_dependencies=file_deps,
                )
            synced += 1

        print(f"  Synced {synced} pages to DB")
        print(f"  Coverage: {plan.covered_chunks}/{plan.total_chunks} chunks ({plan.coverage*100:.1f}%)")
        print(f"  Modules: {len(plan.modules)} graph-based + {len(plan.isolated_modules)} isolated")

    finally:
        db.close()


def cmd_generate_wiki_plan(args: argparse.Namespace) -> None:
    """Generate module tree from call graph for CodeWiki auto wiki generation."""
    from hybrid_search.index.dag import generate_wiki_plan

    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, _ = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        plan = generate_wiki_plan(db, pinfo.id)

        print(f"Project: {name}")
        print(f"Coverage: {plan.covered_chunks}/{plan.total_chunks} chunks ({plan.coverage*100:.1f}%)")
        print()

        if plan.modules:
            print(f"Module Tree ({len(plan.modules)} modules):")
            for i, mod in enumerate(plan.modules, 1):
                entry_hint = ""
                if mod.entry_points:
                    entry_hint = f" — entry: {mod.entry_points[0]}"
                rep = ", ".join(mod.representative_paths)
                print(f"  {i:2d}. {mod.name} ({mod.file_count} files, {mod.chunk_count} chunks) — {rep}{entry_hint}")

        if plan.isolated_modules:
            print(f"\nIsolated ({len(plan.isolated_modules)} groups, directory-based fallback):")
            for i, mod in enumerate(plan.isolated_modules, 1):
                rep = ", ".join(mod.representative_paths)
                print(f"  {i:2d}. {mod.name} ({mod.file_count} files, {mod.chunk_count} chunks) — {rep}")

        # Write plan to .hybrid-search/wiki-plan.json for downstream use
        if not args.dry_run:
            import json
            plan_dir = Path(cwd) / ".hybrid-search"
            plan_dir.mkdir(parents=True, exist_ok=True)
            plan_file = plan_dir / "wiki-plan.json"
            plan_data = {
                "project": name,
                "total_chunks": plan.total_chunks,
                "covered_chunks": plan.covered_chunks,
                "coverage": round(plan.coverage, 4),
                "modules": [
                    {
                        "name": m.name,
                        "files": m.files,
                        "chunk_count": m.chunk_count,
                        "entry_points": m.entry_points,
                        "representative_paths": m.representative_paths,
                    }
                    for m in plan.modules
                ],
                "isolated_modules": [
                    {
                        "name": m.name,
                        "files": m.files,
                        "chunk_count": m.chunk_count,
                        "representative_paths": m.representative_paths,
                    }
                    for m in plan.isolated_modules
                ],
            }
            plan_file.write_text(json.dumps(plan_data, indent=2, ensure_ascii=False))
            print(f"\nPlan saved: {plan_file}")

    finally:
        db.close()


def cmd_verify_wiki(args: argparse.Namespace) -> None:
    """Verify wiki coverage against the module tree."""
    import json as json_mod
    from hybrid_search.index.dag import generate_wiki_plan
    from hybrid_search.storage.wiki import normalize_query

    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, _ = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        plan = generate_wiki_plan(db, pinfo.id)
        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        existing_pages = wiki.list_pages(pinfo.id, limit=200)
        existing_keys = {p["query_key"] for p in existing_pages}

        all_modules = plan.modules + plan.isolated_modules

        # Match modules to wiki pages by normalized query_key
        matched: list[dict] = []
        missing: list[dict] = []
        for m in all_modules:
            query_key = normalize_query(m.name.replace("-", " "))
            if query_key in existing_keys:
                matched.append({"name": m.name, "files": m.file_count, "chunks": m.chunk_count})
            else:
                missing.append({"name": m.name, "files": m.files, "chunks": m.chunk_count})

        # File coverage: files in modules vs total
        files_in_modules = {f for m in all_modules for f in m.files}
        all_files = db.get_all_files(pinfo.id)
        all_file_paths = {f.relative_path for f in all_files}
        uncovered_files = sorted(all_file_paths - files_in_modules)

        # Staleness
        staleness = wiki.check_staleness(pinfo.id)
        stale_pages = [s for s in staleness if s.get("stale")]
        fresh_pages = [s for s in staleness if not s.get("stale")]

        # --- Output ---
        if args.json:
            report = {
                "project": name,
                "modules": {"graph": len(plan.modules), "isolated": len(plan.isolated_modules)},
                "wiki_pages": {"matched": len(matched), "missing": len(missing), "total": len(all_modules)},
                "coverage": {
                    "chunks": {"covered": plan.covered_chunks, "total": plan.total_chunks, "pct": round(plan.coverage * 100, 1)},
                    "files": {"in_modules": len(files_in_modules), "total": len(all_file_paths), "uncovered": len(uncovered_files)},
                },
                "staleness": {"fresh": len(fresh_pages), "stale": len(stale_pages)},
                "missing_pages": [{"name": m["name"], "file_count": len(m["files"])} for m in missing],
                "stale_pages": [{"title": s["title"], "changed_files": s.get("changed_files", [])} for s in stale_pages],
                "uncovered_files": uncovered_files[:50],
            }
            print(json_mod.dumps(report, indent=2, ensure_ascii=False))
            return

        print(f"Project: {name}")
        print(f"  Module Tree:    {len(plan.modules)} graph + {len(plan.isolated_modules)} isolated")
        print(f"  Wiki pages:     {len(matched)}/{len(all_modules)} matched ({len(matched)/max(len(all_modules),1)*100:.0f}%)")
        print(f"  File coverage:  {len(files_in_modules)}/{len(all_file_paths)} files in modules ({len(files_in_modules)/max(len(all_file_paths),1)*100:.0f}%)")
        print(f"  Chunk coverage: {plan.covered_chunks}/{plan.total_chunks} ({plan.coverage*100:.1f}%)")
        print(f"  Staleness:      {len(fresh_pages)} fresh, {len(stale_pages)} stale")

        if missing:
            print(f"\n  Missing wiki pages ({len(missing)}):")
            for m in missing[:20]:
                print(f"    - {m['name']} ({len(m['files'])} files, {m['chunks']} chunks)")

        if stale_pages:
            print(f"\n  Stale pages ({len(stale_pages)}):")
            for s in stale_pages:
                changed = ", ".join(s.get("changed_files", [])[:3])
                extra = f" +{len(s.get('changed_files', [])) - 3} more" if len(s.get("changed_files", [])) > 3 else ""
                print(f"    - {s['title']}: {changed}{extra}")

        if uncovered_files:
            print(f"\n  Uncovered files ({len(uncovered_files)}):")
            for f in uncovered_files[:15]:
                print(f"    - {f}")
            if len(uncovered_files) > 15:
                print(f"    ... and {len(uncovered_files) - 15} more")

    finally:
        db.close()


def cmd_search_symbols(args: argparse.Namespace) -> None:
    """Search for symbols (functions, classes) by name."""
    import json as json_mod
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)

    if match:
        name, _ = match
        pinfo = registry.get_by_name(name)
        project_infos = [pinfo] if pinfo else []
    else:
        project_infos = registry.list_all()

    results: list[dict] = []
    for pinfo in project_infos:
        project_dir = get_project_dir(config.projects_dir, pinfo.id)
        idx_paths = IndexPaths(project_dir)
        if not idx_paths.store_db.exists():
            continue
        db = StoreDB(idx_paths.store_db)
        try:
            chunks = db.search_chunks_by_name(args.name, pinfo.id)
            for chunk in chunks:
                if args.type and chunk.node_type != args.type:
                    continue
                file_rec = db.get_file(chunk.file_id)
                results.append({
                    "project": pinfo.name,
                    "name": chunk.name,
                    "qualified_name": chunk.qualified_name,
                    "node_type": chunk.node_type,
                    "file": file_rec.relative_path if file_rec else chunk.file_id,
                    "line": chunk.start_line,
                })
        finally:
            db.close()

    if args.json:
        print(json_mod.dumps(results, indent=2, ensure_ascii=False))
    else:
        if not results:
            print(f"No symbols matching '{args.name}'")
            return
        for r in results[:30]:
            line = f"L{r['line']}" if r.get("line") else ""
            print(f"  {r['qualified_name'] or r['name']} ({r['node_type']}) — {r['file']}:{line}")
        if len(results) > 30:
            print(f"  ... and {len(results) - 30} more")


def cmd_remove_project(args: argparse.Namespace) -> None:
    """Unregister a project and optionally delete its index data."""
    import shutil
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    pinfo = registry.get_by_name(args.name)
    if not pinfo:
        print(f"Project '{args.name}' not found")
        return

    if not args.keep_index:
        project_dir = get_project_dir(config.projects_dir, pinfo.id)
        if project_dir.exists():
            shutil.rmtree(project_dir)
            print(f"Deleted index: {project_dir}")

    registry.remove(pinfo.id)
    print(f"Removed project: {args.name}")


def cmd_lookup_wiki(args: argparse.Namespace) -> None:
    """Look up a wiki page by query."""
    import json as json_mod
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, _ = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        page = wiki.lookup_page(pinfo.id, query=args.query, tag=args.tag)
        if not page:
            print(f"No wiki page found for: {args.query or args.tag}")
            return

        if args.json:
            print(json_mod.dumps({
                "title": page.title, "stale": page.stale,
                "version": page.version, "content": page.content,
            }, indent=2, ensure_ascii=False))
        else:
            stale_mark = " [STALE]" if page.stale else ""
            print(f"{page.title}{stale_mark} (v{page.version})")
            print(page.content)
    finally:
        db.close()


def cmd_verify_synthesis(args: argparse.Namespace) -> None:
    """Re-verify all synthesized wiki pages: file:line refs + symbol existence.

    Reports verified/failed/removed counts per page and overall health.
    """
    import json as json_mod
    from hybrid_search.index.synthesizer import verify_references, verify_symbols

    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, project_path = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki_store = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        all_pages = wiki_store.list_pages(pinfo.id, limit=200)
        synthesized = [p for p in all_pages if wiki_store.is_synthesized(p["page_id"])]

        if not synthesized:
            print("No synthesized pages to verify.")
            return

        results: list[dict] = []
        total_verified = 0
        total_failed = 0
        total_sym_ok = 0
        total_sym_missing = 0
        pages_with_issues = 0

        for page in synthesized:
            row = wiki_store.get_page_row(page["page_id"])
            content = row["content"] if row else ""
            if not content:
                continue

            ref_result = verify_references(content, project_path)
            sym_result = verify_symbols(content, db, pinfo.id)

            failed_count = len(ref_result.failed) + len(sym_result.missing)
            if failed_count > 0:
                pages_with_issues += 1

            total_verified += len(ref_result.verified)
            total_failed += len(ref_result.failed)
            total_sym_ok += len(sym_result.found)
            total_sym_missing += len(sym_result.missing)

            entry = {
                "title": page.get("title", page["page_id"]),
                "refs_ok": len(ref_result.verified),
                "refs_failed": len(ref_result.failed),
                "symbols_ok": len(sym_result.found),
                "symbols_missing": len(sym_result.missing),
                "failed_refs": ref_result.failed,
                "missing_symbols": sym_result.missing,
            }
            results.append(entry)

        # Auto-fix mode: update pages with cleaned content
        fixed = 0
        if args.fix:
            for page, entry in zip(synthesized, results):
                if entry["refs_failed"] > 0:
                    row = wiki_store.get_page_row(page["page_id"])
                    content = row["content"] if row else ""
                    ref_result = verify_references(content, project_path)
                    if ref_result.cleaned_content != content:
                        wiki_store.refresh_page(
                            page["page_id"], ref_result.cleaned_content,
                        )
                        fixed += 1

        # Output
        if args.json:
            report = {
                "project": name,
                "synthesized_pages": len(synthesized),
                "pages_with_issues": pages_with_issues,
                "totals": {
                    "refs_verified": total_verified,
                    "refs_failed": total_failed,
                    "symbols_found": total_sym_ok,
                    "symbols_missing": total_sym_missing,
                },
                "fixed": fixed,
                "pages": results,
            }
            print(json_mod.dumps(report, indent=2, ensure_ascii=False))
            return

        print(f"Project: {name}")
        print(f"  Synthesized pages: {len(synthesized)}")
        print(f"  File:line refs:    {total_verified} OK, {total_failed} failed")
        print(f"  Symbol refs:       {total_sym_ok} OK, {total_sym_missing} missing")

        if pages_with_issues:
            print(f"\n  Pages with issues ({pages_with_issues}):")
            for r in results:
                issues = r["refs_failed"] + r["symbols_missing"]
                if issues == 0:
                    continue
                print(f"    - {r['title']}: {r['refs_failed']} bad refs, {r['symbols_missing']} missing symbols")
                for ref in r["failed_refs"][:3]:
                    print(f"        ref: {ref}")
                for sym in r["missing_symbols"][:3]:
                    print(f"        sym: {sym}")

        if fixed > 0:
            print(f"\n  Fixed: {fixed} page(s) — bad refs removed from DB content")

        health = "HEALTHY" if pages_with_issues == 0 else f"ISSUES ({pages_with_issues} pages)"
        print(f"\n  Health: {health}")

    finally:
        db.close()


def cmd_synthesize_wiki(args: argparse.Namespace) -> None:
    """Two-phase wiki synthesis: --prepare collects context, --finalize saves results.

    Flow:
      1. synthesize-wiki --prepare  → writes context files to _synthesis_input/
      2. Claude Code reads context, writes synthesis to _synthesis_output/
      3. synthesize-wiki --finalize → verifies refs, merges, saves to DB
    """
    from hybrid_search.index.synthesizer import (
        ModuleContext,
        collect_module_context,
        estimate_tokens,
        finalize_module,
        prepare_context_file,
        should_skip_synthesis,
    )

    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if not match:
        print(f"No registered project found for: {cwd}")
        return

    name, project_path = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        return

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print("No index found. Run reindex first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        wiki_store = db.wiki_store(max_pages=config.wiki.max_pages_per_project)
        wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
        input_dir = wiki_dir / "_synthesis_input"
        output_dir = wiki_dir / "_synthesis_output"

        # -- FINALIZE MODE --
        if args.finalize:
            output_dir.mkdir(parents=True, exist_ok=True)
            output_files = sorted(output_dir.glob("*.md"))
            if not output_files:
                print(f"No synthesis output found in {output_dir}")
                print("Write synthesis to _synthesis_output/<module>.md first.")
                return

            finalized = 0
            for out_file in output_files:
                mod_name = out_file.stem
                synthesis_content = out_file.read_text(encoding="utf-8")
                if not synthesis_content.strip():
                    print(f"  Skip: {mod_name} (empty)")
                    continue

                result = finalize_module(
                    db, pinfo.id, mod_name, synthesis_content,
                    project_path, wiki_dir,
                )
                if "error" in result:
                    print(f"  Error: {result['error']}")
                else:
                    print(
                        f"  Finalized: {mod_name} "
                        f"({result['verified_refs']} refs OK, "
                        f"{result['failed_refs']} removed)"
                    )
                    finalized += 1

            # Clean up
            if finalized > 0:
                for f in output_files:
                    f.unlink()
                for f in input_dir.glob("*.md"):
                    f.unlink()
                print(f"\nDone: {finalized} module(s) finalized. Input/output files cleaned.")
            return

        # -- PREPARE / DRY-RUN MODE --

        # Determine target modules
        if args.module:
            target_modules = [args.module]
        else:
            staleness = wiki_store.check_staleness(pinfo.id)
            stale_pages = [p for p in staleness if p["stale"]]
            if stale_pages:
                target_modules = [p["title"] for p in stale_pages]
            else:
                all_pages = wiki_store.list_pages(pinfo.id, limit=200)
                unsynthesized = [
                    p["title"] for p in all_pages
                    if not wiki_store.is_synthesized(p["page_id"])
                ]
                target_modules = unsynthesized

        if not target_modules:
            print("No modules to synthesize (all up-to-date).")
            return

        # Collect contexts (skip unchanged modules via synthesis_hash)
        contexts: list[tuple[str, ModuleContext]] = []
        skipped_hash = 0
        for mod_name in target_modules:
            skip, reason = should_skip_synthesis(db, pinfo.id, mod_name, project_path)
            if skip:
                print(f"  Skip: {mod_name} ({reason})")
                skipped_hash += 1
                continue
            ctx = collect_module_context(db, pinfo.id, mod_name, project_path)
            if ctx:
                contexts.append((mod_name, ctx))
            else:
                print(f"  Skip: {mod_name} (no wiki page found)")

        if skipped_hash > 0:
            print(f"  ({skipped_hash} module(s) skipped — inputs unchanged)")

        if not contexts:
            print("No valid modules to synthesize.")
            return

        # Dry-run: show token estimates
        if args.dry_run:
            total_tokens = 0
            print(f"Project: {name}")
            print(f"\n{'Module':<30} {'Chunks':>6} {'Files':>5} {'Tokens':>10}")
            print("-" * 55)
            for mod_name, ctx in contexts:
                est = estimate_tokens(ctx)
                total_tokens += est["input_tokens"]
                print(
                    f"  {est['module']:<28} {est['source_chunks']:>6} "
                    f"{est['files']:>5} {est['input_tokens']:>10}"
                )
            print("-" * 55)
            print(f"  {'TOTAL':<28} {'':>6} {'':>5} {total_tokens:>10}")
            print(f"\nTargets: {len(contexts)} module(s)")
            return

        # Prepare: write context files
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        for mod_name, ctx in contexts:
            slug = mod_name.lower().replace(" ", "-")
            out_path = input_dir / f"{slug}.md"
            prepare_context_file(ctx, out_path)
            print(f"  Prepared: {out_path.name}")

        print(f"\n{len(contexts)} context file(s) written to {input_dir}")
        print(f"\nNext steps:")
        print(f"  1. Read each file in {input_dir}/")
        print(f"  2. Write synthesis to {output_dir}/<same-name>.md")
        print(f"  3. Run: python -m hybrid_search.cli synthesize-wiki --finalize --cwd {args.cwd}")

    finally:
        db.close()


def cmd_search(args: argparse.Namespace) -> None:
    """Run hybrid search from CLI — same engine as MCP tool."""
    import json as _json

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)

    from hybrid_search.search.orchestrator import SearchOrchestrator
    from hybrid_search.tools.hybrid_search import handle_hybrid_search

    orchestrator = SearchOrchestrator(config, registry, embedder)

    cwd = str(Path(args.cwd).resolve())
    node_types = [t.strip() for t in args.node_types.split(",")] if args.node_types else None

    result = handle_hybrid_search(
        orchestrator=orchestrator,
        query=args.query,
        project=args.project,
        limit=args.limit,
        file_pattern=args.file_pattern,
        node_types=node_types,
        cwd=cwd,
    )

    if args.json:
        print(_json.dumps(result, ensure_ascii=False, indent=2))
        return

    results = result.get("results", [])
    if not results:
        print("No results found.")
        return

    print(
        f"Query: {result.get('query_type')} | "
        f"BM25w: {result.get('effective_bm25_weight')} | "
        f"{result.get('query_time_ms', 0):.0f}ms | "
        f"{result.get('total_chunks_searched')} chunks"
    )
    print()

    for i, r in enumerate(results, 1):
        score = f"RRF={r['rrf_score']:.4f}"
        loc = f"{r['file_path']}:{r['start_line']}-{r['end_line']}"
        name = r.get("qualified_name") or r.get("name") or r.get("node_type", "")
        print(f"  {i}. [{score}] {name}")
        print(f"     {loc}")
        snippet = r.get("snippet", "")
        if snippet:
            for line in snippet.split("\n")[:3]:
                print(f"     | {line}")
        print()


def cmd_index(args: argparse.Namespace) -> None:
    """User-friendly index command — wraps reindex with sensible defaults."""
    # Build a namespace compatible with cmd_reindex
    args.cwd = args.path
    args.git_delta = False
    args.wiki_scope = "full"
    args.synthesize = False
    cmd_reindex(args)


def cmd_serve(_args: argparse.Namespace) -> None:
    """Start MCP server over stdio (for Claude Code / MCP clients)."""
    import asyncio
    from hybrid_search.server import _run_server

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    asyncio.run(_run_server(config))


def cmd_setup(args: argparse.Namespace) -> None:
    """One-time global setup: register MCP server + Claude Code hooks."""
    import json as _json

    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)
    venv_str = str(venv_python)

    # --- Step 1: ~/.claude.json — MCP server registration ---
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        data = _json.loads(claude_json.read_text())
    else:
        data = {}

    servers = data.setdefault("mcpServers", {})
    if "hybrid-search" in servers:
        existing_cmd = servers["hybrid-search"].get("command", "")
        if existing_cmd == venv_str:
            print(f"MCP server already registered: {claude_json}")
        else:
            servers["hybrid-search"] = {
                "command": venv_str,
                "args": ["-m", "hybrid_search.server"],
            }
            claude_json.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
            print(f"MCP server updated: {claude_json}")
    else:
        servers["hybrid-search"] = {
            "command": venv_str,
            "args": ["-m", "hybrid_search.server"],
        }
        claude_json.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
        print(f"MCP server registered: {claude_json}")

    # --- Step 2: ~/.claude/settings.json — global hooks ---
    settings_dir = Path.home() / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings_path = settings_dir / "settings.json"

    if settings_path.exists():
        settings = _json.loads(settings_path.read_text())
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})

    # Check if our hooks already exist
    pre_hooks = hooks.get("PreToolUse", [])
    has_auto_index = any(
        "hybrid-search/wiki" in str(h.get("hooks", [{}])[0].get("command", ""))
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Read"
    )
    has_stale_check = any(
        "STALE.md" in str(h.get("hooks", [{}])[0].get("command", ""))
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Edit|Write"
    )

    if has_auto_index and has_stale_check:
        print(f"Hooks already registered: {settings_path}")
    else:
        # Build fresh hook entries
        auto_index_hook = {
            "matcher": "Read",
            "hooks": [{
                "type": "command",
                "command": (
                    f'ROOT=$(git rev-parse --show-toplevel 2>/dev/null); '
                    f'VENV={venv_str}; '
                    f'[ -n "$ROOT" ] && [ -f "$VENV" ] && [ ! -d "$ROOT/.hybrid-search/wiki" ] && '
                    f'mkdir -p "$ROOT/.hybrid-search" && '
                    f'nohup sh -c \'"$1" -m hybrid_search.cli reindex --git-delta --wiki-scope affected --synthesize --cwd "$2" && '
                    f'"$1" -m hybrid_search.cli install-hook --cwd "$2"\' _ "$VENV" "$ROOT" '
                    f'> /dev/null 2>&1 & '
                    f'echo "hybrid-search: first-time indexing started in background for $ROOT"'
                ),
            }],
        }
        stale_hook = {
            "matcher": "Edit|Write",
            "hooks": [{
                "type": "command",
                "command": (
                    'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
                    '[ -n "$ROOT" ] && [ -f "$ROOT/.hybrid-search/wiki/STALE.md" ] && '
                    "echo 'STALE wiki pages detected — update them BEFORE editing code:' && "
                    'cat "$ROOT/.hybrid-search/wiki/STALE.md"'
                ),
            }],
        }
        gap_hook = {
            "matcher": "Edit|Write",
            "hooks": [{
                "type": "command",
                "command": (
                    'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
                    '[ -n "$ROOT" ] && [ -f "$ROOT/.hybrid-search/wiki-gaps.txt" ] && '
                    "echo 'Wiki gaps — new modules need wiki pages:' && "
                    'cat "$ROOT/.hybrid-search/wiki-gaps.txt"'
                ),
            }],
        }

        # Remove old hybrid-search hooks, keep others
        new_pre = [h for h in pre_hooks if not (
            isinstance(h, dict) and (
                "hybrid-search/wiki" in str(h.get("hooks", [{}])[0].get("command", ""))
                or "STALE.md" in str(h.get("hooks", [{}])[0].get("command", ""))
            )
        )]
        new_pre.extend([auto_index_hook, stale_hook])
        hooks["PreToolUse"] = new_pre

        post_hooks = hooks.get("PostToolUse", [])
        new_post = [h for h in post_hooks if not (
            isinstance(h, dict)
            and "wiki-gaps.txt" in str(h.get("hooks", [{}])[0].get("command", ""))
        )]
        new_post.append(gap_hook)
        hooks["PostToolUse"] = new_post

        settings_path.write_text(_json.dumps(settings, indent=2, ensure_ascii=False))
        print(f"Hooks registered: {settings_path}")

    # --- Step 3: ~/.claude/skills/ — install portable skills ---
    skills_src = Path(__file__).resolve().parents[2] / "skills"
    skills_dst = Path.home() / ".claude" / "skills"

    if skills_src.is_dir():
        installed = 0
        for skill_file in sorted(skills_src.glob("*.md")):
            skill_name = skill_file.stem
            dst_dir = skills_dst / skill_name
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_file = dst_dir / "skill.md"
            src_content = skill_file.read_text(encoding="utf-8")

            if dst_file.exists():
                existing = dst_file.read_text(encoding="utf-8")
                if existing == src_content:
                    continue  # identical, skip

            dst_file.write_text(src_content, encoding="utf-8")
            installed += 1

        if installed > 0:
            print(f"Skills installed: {installed} skill(s) → {skills_dst}")
        else:
            print(f"Skills already up-to-date: {skills_dst}")
    else:
        print(f"Skills source not found: {skills_src} (skipped)")

    print()
    print("Setup complete. Restart Claude Code to apply changes.")


def cmd_install_hook(args: argparse.Namespace) -> None:
    """Install post-commit hook in a project's .git/hooks/."""
    import subprocess
    import shutil

    cwd = Path(args.cwd).resolve()

    # Find .git dir for this project
    try:
        git_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(cwd), text=True,
        ).strip()
        git_dir = (cwd / git_dir).resolve()
    except subprocess.CalledProcessError:
        print(f"Not a git repository: {cwd}")
        return

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"

    # Find our venv python
    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    hook_content = f"""#!/bin/bash
# Hybrid Search — auto delta-reindex on commit (background, non-blocking)
PROJECT_DIR="$(git rev-parse --show-toplevel)"
nohup "{venv_python}" -m hybrid_search.cli reindex --git-delta --wiki-scope affected --synthesize --cwd "$PROJECT_DIR" > /dev/null 2>&1 &
"""

    if hook_path.exists():
        existing = hook_path.read_text()
        if "hybrid_search.cli" in existing:
            print(f"Hook already installed: {hook_path}")
            return
        # Append to existing hook
        with open(hook_path, "a") as f:
            f.write("\n# --- Hybrid Search auto-reindex ---\n")
            f.write(hook_content.split("\n", 1)[1])  # Skip shebang
        print(f"Appended to existing hook: {hook_path}")
    else:
        hook_path.write_text(hook_content)
        print(f"Installed hook: {hook_path}")

    hook_path.chmod(0o755)
    print("Post-commit hook will auto-reindex on every commit (background, non-blocking).")


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="hybrid-search-mcp",
        description="Hybrid BM25 + Vector search for codebases",
    )
    sub = parser.add_subparsers(dest="command")

    # ── Primary commands (standalone usage) ──
    p_index = sub.add_parser("index", help="Index a project directory")
    p_index.add_argument("path", nargs="?", default=".", help="Project directory (default: .)")
    p_index.add_argument("--force", action="store_true", help="Force full reindex")
    p_index.add_argument("--wiki", action="store_true", help="Auto-generate wiki after index")

    p_search = sub.add_parser("search", help="Hybrid BM25 + semantic search")
    p_search.add_argument("query", help="Search query (Korean or English)")
    p_search.add_argument("--cwd", default=".", help="Project directory (default: .)")
    p_search.add_argument("--project", help="Project name (auto-detected from cwd)")
    p_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    p_search.add_argument("--file-pattern", help="Glob filter (e.g., '*.ts')")
    p_search.add_argument("--node-types", help="Comma-separated: function,class,method")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")

    sub.add_parser("serve", help="Start MCP server (for Claude Code / MCP clients)")

    # ── Setup & admin ──
    sub.add_parser("setup", help="One-time setup: register MCP server + hooks in Claude Code")

    p_reindex = sub.add_parser("reindex", help="Delta reindex a project")
    p_reindex.add_argument("--cwd", default=".", help="Project directory")
    p_reindex.add_argument("--force", action="store_true", help="Force full reindex")
    p_reindex.add_argument("--git-delta", action="store_true", help="Use git diff for changed-file detection with full-scan fallback")
    p_reindex.add_argument("--wiki", action="store_true", help="Auto-generate wiki after reindex")
    p_reindex.add_argument(
        "--wiki-scope",
        choices=("full", "affected"),
        default="full",
        help="Wiki regeneration scope after reindex",
    )
    p_reindex.add_argument("--synthesize", action="store_true", help="Auto-prepare synthesis for stale modules after reindex")

    p_status = sub.add_parser("status", help="Show index status")

    p_stale = sub.add_parser("stale", help="Check wiki staleness")
    p_stale.add_argument("--cwd", default=".", help="Project directory")

    p_hook = sub.add_parser("install-hook", help="Install post-commit hook in a project")
    p_hook.add_argument("--cwd", default=".", help="Project directory")

    p_sync = sub.add_parser("sync-wiki", help="Sync disk wiki files to DB for staleness tracking")
    p_sync.add_argument("--cwd", default=".", help="Project directory")

    p_cg = sub.add_parser("call-graph-stats", help="Show call graph resolution statistics")
    p_cg.add_argument("--cwd", default=".", help="Project directory")

    p_plan = sub.add_parser("generate-wiki-plan", help="Generate module tree from call graph")
    p_plan.add_argument("--cwd", default=".", help="Project directory")
    p_plan.add_argument("--dry-run", action="store_true", help="Print plan without saving")

    p_verify = sub.add_parser("verify-wiki", help="Verify wiki coverage against module tree")
    p_verify.add_argument("--cwd", default=".", help="Project directory")
    p_verify.add_argument("--json", action="store_true", help="Output as JSON")

    p_genwiki = sub.add_parser("generate-wiki", help="Generate wiki pages from module tree")
    p_genwiki.add_argument("--cwd", default=".", help="Project directory")

    p_sym = sub.add_parser("search-symbols", help="Search symbols by name")
    p_sym.add_argument("name", help="Symbol name or pattern")
    p_sym.add_argument("--cwd", default=".", help="Project directory")
    p_sym.add_argument("--type", help="Filter: function, class, method, etc.")
    p_sym.add_argument("--json", action="store_true", help="Output as JSON")

    p_rm = sub.add_parser("remove-project", help="Unregister a project")
    p_rm.add_argument("name", help="Project name")
    p_rm.add_argument("--keep-index", action="store_true", help="Keep index data on disk")

    p_lookup = sub.add_parser("lookup-wiki", help="Look up a wiki page by query")
    p_lookup.add_argument("query", nargs="?", help="Question to look up")
    p_lookup.add_argument("--tag", help="Look up by tag instead")
    p_lookup.add_argument("--cwd", default=".", help="Project directory")
    p_lookup.add_argument("--json", action="store_true", help="Output as JSON")

    p_synth = sub.add_parser("synthesize-wiki", help="LLM synthesis for wiki pages")
    p_synth.add_argument("--module", help="Synthesize a specific module (default: all stale)")
    p_synth.add_argument("--cwd", default=".", help="Project directory")
    p_synth.add_argument("--dry-run", action="store_true", help="Show targets and token estimate only")
    p_synth.add_argument("--finalize", action="store_true", help="Finalize: verify refs, merge, save to DB")

    p_vsyn = sub.add_parser("verify-synthesis", help="Re-verify synthesized wiki pages (refs + symbols)")
    p_vsyn.add_argument("--cwd", default=".", help="Project directory")
    p_vsyn.add_argument("--json", action="store_true", help="Output as JSON")
    p_vsyn.add_argument("--fix", action="store_true", help="Auto-remove bad refs from DB content")

    args = parser.parse_args()

    if args.command == "index":
        cmd_index(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "stale":
        cmd_stale(args)
    elif args.command == "install-hook":
        cmd_install_hook(args)
    elif args.command == "sync-wiki":
        cmd_sync_wiki(args)
    elif args.command == "call-graph-stats":
        cmd_call_graph_stats(args)
    elif args.command == "generate-wiki-plan":
        cmd_generate_wiki_plan(args)
    elif args.command == "verify-wiki":
        cmd_verify_wiki(args)
    elif args.command == "generate-wiki":
        cmd_generate_wiki(args)
    elif args.command == "search-symbols":
        cmd_search_symbols(args)
    elif args.command == "remove-project":
        cmd_remove_project(args)
    elif args.command == "lookup-wiki":
        cmd_lookup_wiki(args)
    elif args.command == "synthesize-wiki":
        cmd_synthesize_wiki(args)
    elif args.command == "verify-synthesis":
        cmd_verify_synthesis(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
