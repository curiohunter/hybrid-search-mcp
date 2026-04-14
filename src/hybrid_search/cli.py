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
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.project import ProjectRegistry
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger("hybrid_search.cli")


def _detect_project(registry: ProjectRegistry, cwd: str) -> tuple[str, str] | None:
    """Find registered project matching cwd. Returns (name, path) or None."""
    cwd_path = Path(cwd).resolve()
    for pinfo in registry.list_all():
        project_path = Path(pinfo.path).resolve()
        try:
            cwd_path.relative_to(project_path)
            return pinfo.name, pinfo.path
        except ValueError:
            pass
    return None


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

    start = time.monotonic()
    print(f"Reindexing: {project_name} ({project_path})")

    def progress(current: int, total: int, path: str) -> None:
        if total > 0 and current % 50 == 0:
            print(f"  [{current}/{total}] {path}")

    result = pipeline.index_project(
        project_path, project_name, force=args.force, on_progress=progress,
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

    # Auto-generate wiki if --wiki flag
    if getattr(args, "wiki", False):
        print("Generating wiki from module tree...")
        import argparse as _ap
        _wiki_args = _ap.Namespace(cwd=project_path)
        cmd_generate_wiki(_wiki_args)
    else:
        # Auto sync existing wiki if wiki directory exists
        wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
        if wiki_dir.exists() and any(wiki_dir.glob("*.md")):
            print("Auto-syncing wiki to DB...")
            import argparse as _ap
            _sync_args = _ap.Namespace(cwd=project_path)
            cmd_sync_wiki(_sync_args)

    # Re-check staleness after sync (cleans up STALE.md when all pages are fresh)
    _mark_stale_wikis(config, registry, project_name)

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


def cmd_synthesize_wiki(args: argparse.Namespace) -> None:
    """Two-phase wiki synthesis: --prepare collects context, --finalize saves results.

    Flow:
      1. synthesize-wiki --prepare  → writes context files to _synthesis_input/
      2. Claude Code reads context, writes synthesis to _synthesis_output/
      3. synthesize-wiki --finalize → verifies refs, merges, saves to DB
    """
    from hybrid_search.index.synthesizer import (
        collect_module_context,
        estimate_tokens,
        finalize_module,
        prepare_context_file,
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
                unsynthesized = []
                for p in all_pages:
                    row = db._conn.execute(
                        "SELECT synthesis_model FROM wiki_pages WHERE id = ?",
                        (p["page_id"],),
                    ).fetchone()
                    if row and not row["synthesis_model"]:
                        unsynthesized.append(p["title"])
                target_modules = unsynthesized

        if not target_modules:
            print("No modules to synthesize (all up-to-date).")
            return

        # Collect contexts
        contexts: list[tuple[str, object]] = []
        for mod_name in target_modules:
            ctx = collect_module_context(db, pinfo.id, mod_name, project_path)
            if ctx:
                contexts.append((mod_name, ctx))
            else:
                print(f"  Skip: {mod_name} (no wiki page found)")

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
nohup "{venv_python}" -m hybrid_search.cli reindex --cwd "$PROJECT_DIR" > /dev/null 2>&1 &
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
        prog="hybrid-search",
        description="Hybrid Search CLI — background indexing for git hooks",
    )
    sub = parser.add_subparsers(dest="command")

    p_reindex = sub.add_parser("reindex", help="Delta reindex a project")
    p_reindex.add_argument("--cwd", default=".", help="Project directory")
    p_reindex.add_argument("--force", action="store_true", help="Force full reindex")
    p_reindex.add_argument("--wiki", action="store_true", help="Auto-generate wiki after reindex")

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

    args = parser.parse_args()

    if args.command == "reindex":
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
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
