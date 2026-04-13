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

    # Auto sync wiki if wiki directory exists
    wiki_dir = Path(project_path) / ".hybrid-search" / "wiki"
    if wiki_dir.exists() and any(wiki_dir.glob("*.md")):
        print("Auto-syncing wiki to DB...")
        # Reuse sync logic inline
        import argparse as _ap
        _sync_args = _ap.Namespace(cwd=project_path)
        cmd_sync_wiki(_sync_args)

    # Gap detection for new files
    _write_gap_flag(cwd, result.files_added)

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"  {err}")


def _mark_stale_wikis(config, registry: ProjectRegistry, project_name: str) -> None:
    """Mark wiki pages as stale when their source files changed."""
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
        stale_count = sum(1 for p in stale_pages if p["stale"])
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

    p_status = sub.add_parser("status", help="Show index status")

    p_stale = sub.add_parser("stale", help="Check wiki staleness")
    p_stale.add_argument("--cwd", default=".", help="Project directory")

    p_hook = sub.add_parser("install-hook", help="Install post-commit hook in a project")
    p_hook.add_argument("--cwd", default=".", help="Project directory")

    p_sync = sub.add_parser("sync-wiki", help="Sync disk wiki files to DB for staleness tracking")
    p_sync.add_argument("--cwd", default=".", help="Project directory")

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
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
