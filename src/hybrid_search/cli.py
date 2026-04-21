"""CLI entrypoint for background indexing (git hook, no MCP overhead).

Usage:
    python -m hybrid_search.cli reindex [--cwd PATH] [--force]
    python -m hybrid_search.cli status
    python -m hybrid_search.cli stale [--cwd PATH]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.config import load_config
from hybrid_search.index.dag import generate_all_wiki_pages
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.index.scanner import (
    get_changed_files_from_git,
    parse_git_diff_name_status,
)

# M3: Post-commit hook captures ``git diff --name-status HEAD~1 HEAD`` at the
# exact commit moment and exports it via this env var. ``cmd_reindex`` then
# parses the pre-computed diff instead of re-invoking git — which would race
# if a second commit lands before the deferred reindex runs.
_HOOK_DIFF_ENV = "HYBRID_SEARCH_CHANGED_STATUS"

# M4: Lightweight signal file. Skills check existence to remind the user that
# wiki synthesis is pending. Cleared automatically when reindex finds no stale
# pages or when `synthesize-wiki --finalize` completes successfully.
_NEEDS_SYNTHESIS_FLAG = ".hybrid-search/needs_synthesis"
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
    """Install or update hybrid-search section in CLAUDE.md. Idempotent.

    On re-install, replaces the existing section in-place (marker-bounded
    from ``<!-- hybrid-search -->`` up to the next top-level ``## `` heading
    or EOF). First-install inserts at the top, after the first ``# `` H1 if
    present. Removal is exposed via ``_remove_claude_md``.
    """
    import re as _re
    claude_md = Path(project_path) / "CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        pattern = _re.compile(
            _re.escape(_CLAUDE_MD_MARKER) + r"\n## [^\n]+\n.*?(?=\n## |\Z)",
            flags=_re.DOTALL,
        )
        if pattern.search(content):
            # Lambda replacement avoids back-reference parsing in the section body.
            new_content = pattern.sub(lambda _m: _CLAUDE_MD_SECTION.rstrip("\n"), content)
            # Preserve the original's trailing newline if any — keeps diff minimal.
            if content.endswith("\n") and not new_content.endswith("\n"):
                new_content += "\n"
            if new_content != content:
                claude_md.write_text(new_content, encoding="utf-8")
                print("CLAUDE.md: hybrid-search section updated")
            return
        # First install — insert at TOP (after first H1 if exists)
        lines = content.split("\n")
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith("# "):
                insert_at = i + 1
                while insert_at < len(lines) and not lines[insert_at].strip():
                    insert_at += 1
                break
        lines.insert(insert_at, _CLAUDE_MD_SECTION)
        claude_md.write_text("\n".join(lines), encoding="utf-8")
        print("CLAUDE.md: hybrid-search section added (top)")
    else:
        claude_md.write_text(_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")
        print("CLAUDE.md: created with hybrid-search instructions")


def _remove_claude_md(project_path: str) -> bool:
    """Remove the hybrid-search section from CLAUDE.md. Returns True if removed.

    Uses the same marker-bounded regex as :func:`_ensure_claude_md`. Safe if
    the section is missing or the file does not exist.
    """
    import re as _re
    claude_md = Path(project_path) / "CLAUDE.md"
    if not claude_md.exists():
        return False
    content = claude_md.read_text(encoding="utf-8")
    pattern = _re.compile(
        r"\n*" + _re.escape(_CLAUDE_MD_MARKER) + r"\n## [^\n]+\n.*?(?=\n## |\Z)",
        flags=_re.DOTALL,
    )
    new_content = pattern.sub("", content)
    if new_content == content:
        return False
    claude_md.write_text(new_content.lstrip("\n"), encoding="utf-8")
    return True


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
        env_status = os.environ.get(_HOOK_DIFF_ENV)
        if env_status is not None:
            # M3: hook-provided diff (synchronously captured at commit moment).
            # Avoids subprocess re-invocation and a race where a second commit
            # between hook fire and deferred reindex would move HEAD~1.
            diff = parse_git_diff_name_status(env_status)
            changed_paths = list(dict.fromkeys(diff.added + diff.modified))
            deleted_paths = list(dict.fromkeys(diff.deleted))
            if not changed_paths and not deleted_paths:
                print("Hook diff: no changed files, skipping reindex.")
                return
            print(
                "Hook diff:"
                f" {len(diff.added)} added,"
                f" {len(diff.modified)} modified,"
                f" {len(diff.deleted)} deleted,"
                f" {len(diff.renamed)} renamed"
            )
        else:
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

    # Stale wiki marking — always check after reindex
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
                resolved = stats["extracted"] + stats["inferred"]
                print(
                    f"Call graph: {resolved} resolved "
                    f"({stats['extracted']} extracted + {stats['inferred']} inferred), "
                    f"{stats['unresolved']} unresolved"
                )
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

    # Auto-install post-commit + post-checkout hooks (once per project; respects core.hooksPath)
    hooks_dir = _git_hooks_dir(Path(project_path))
    commit_ok = (hooks_dir / "post-commit").exists() and \
        "hybrid_search.cli" in (hooks_dir / "post-commit").read_text()
    checkout_ok = (hooks_dir / "post-checkout").exists() and \
        "hybrid_search.cli" in (hooks_dir / "post-checkout").read_text()
    if not (commit_ok and checkout_ok):
        import argparse as _ap
        _hook_args = _ap.Namespace(cwd=project_path)
        cmd_install_hook(_hook_args)

    # Gap detection for new files
    _write_gap_flag(cwd, result.files_added)

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"  {err}")


def _write_needs_synthesis_flag(project_path: Path, stale_items: list[dict]) -> None:
    """Write a structured JSON signal listing stale modules.

    Skills read this via existence-check + json.load — no DB query needed.
    Modules are ordered by the DB's check_staleness sequence; we keep the
    first few to fit a single-screen reminder.
    """
    import datetime
    import json

    payload = {
        "stale_count": len(stale_items),
        "stale_modules": [p.get("title", p.get("page_id", "")) for p in stale_items[:20]],
        "detected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    flag_path = project_path / _NEEDS_SYNTHESIS_FLAG
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_needs_synthesis_flag(project_path: Path) -> bool:
    """Remove the flag if present. Returns True if a file was removed."""
    flag_path = project_path / _NEEDS_SYNTHESIS_FLAG
    if flag_path.exists():
        flag_path.unlink()
        return True
    return False


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
            project_root = Path(project_path)
            stale_md_path = project_root / ".hybrid-search" / "wiki" / "STALE.md"
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
            _write_needs_synthesis_flag(project_root, stale_items)
            print(f"Wiki: {len(stale_items)} stale page(s) → STALE.md written, needs_synthesis flag set")
        elif not stale_items and project_path:
            # Remove STALE.md if no stale pages
            project_root = Path(project_path)
            stale_md_path = project_root / ".hybrid-search" / "wiki" / "STALE.md"
            if stale_md_path.exists():
                stale_md_path.unlink()
                print("Wiki: all pages fresh, STALE.md removed")
            if _clear_needs_synthesis_flag(project_root):
                print("Wiki: needs_synthesis flag cleared")
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

        _write_wiki_coverage(db, pages, wiki_dir)

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


def _status_mark(ok: bool, warn: bool = False) -> str:
    return "⚠" if warn else ("✓" if ok else "✗")


def _check_global_status() -> None:
    """Print global installation health (MCP registration, hooks, skills, API key)."""
    import json as _json

    print("Global (~/.claude/):")

    # MCP server registration
    claude_json = Path.home() / ".claude.json"
    mcp_ok = claude_json.exists() and "hybrid-search" in claude_json.read_text()
    print(f"  {_status_mark(mcp_ok)} MCP server registered       ({claude_json})")

    # PreToolUse hooks in ~/.claude/settings.json
    settings_path = Path.home() / ".claude" / "settings.json"
    hook_specs = [
        ("auto_index", "Read", "hybrid-search/wiki"),
        ("stale",      "Edit|Write", "STALE.md"),
        ("gaps",       "Read|Edit|Write", "wiki-gaps"),
        ("route",      "Glob|Grep", "wiki/index.md"),
    ]
    installed_hooks: list[str] = []
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text())
            pre = settings.get("hooks", {}).get("PreToolUse", [])
            for name, matcher, keyword in hook_specs:
                found = any(
                    isinstance(h, dict)
                    and h.get("matcher") == matcher
                    and keyword in str(h.get("hooks", [{}])[0].get("command", ""))
                    for h in pre
                )
                if found:
                    installed_hooks.append(name)
        except (ValueError, OSError):
            pass
    total = len(hook_specs)
    n = len(installed_hooks)
    mark = _status_mark(n == total, warn=(0 < n < total))
    detail = ", ".join(installed_hooks) if installed_hooks else "none"
    print(f"  {mark} PreToolUse hooks: {n}/{total}  ({detail})")

    # Skills
    skills_dir = Path.home() / ".claude" / "skills"
    if skills_dir.exists():
        skills = sorted(d.name for d in skills_dir.iterdir() if d.is_dir() and (d / "skill.md").exists())
        own = [s for s in skills if s in {
            "search", "maintain", "setup-hybrid-search", "save-wiki",
            "rebuild-index", "bootstrap-wiki",
        }]
        print(f"  {_status_mark(len(own) > 0)} Skills installed: {len(own)} ({', '.join(own) or 'none'})")
    else:
        print(f"  ✗ Skills directory missing        ({skills_dir})")

    # API key
    import os
    api_ok = bool(os.environ.get("OPENAI_API_KEY"))
    if not api_ok:
        src_root = Path(__file__).resolve().parents[2]
        env_file = src_root / ".env.local"
        if env_file.exists() and "OPENAI_API_KEY" in env_file.read_text():
            api_ok = True
    print(f"  {_status_mark(api_ok)} OPENAI_API_KEY configured     ({'env or .env.local' if api_ok else 'MISSING'})")


def _check_project_status(project_path: Path) -> None:
    """Print per-project health (index, wiki, git hook, .gitignore, CLAUDE.md)."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    match = _detect_project(registry, str(project_path))

    print(f"\nProject ({project_path}):")

    if not match:
        print("  ✗ Not registered — run `hybrid-search-mcp index` first")
        return

    name, _ = match
    pinfo = registry.get_by_name(name)
    if not pinfo:
        print("  ✗ Registry record missing")
        return

    # Index
    print(f"  ✓ Indexed as {name!r}: {pinfo.file_count} files, {pinfo.chunk_count} chunks")
    if pinfo.last_indexed_at:
        print(f"    Last indexed: {pinfo.last_indexed_at}")

    # Wiki
    wiki_dir = project_path / ".hybrid-search" / "wiki"
    if wiki_dir.exists():
        pages = [p for p in wiki_dir.glob("*.md") if p.name not in {"STALE.md", "index.md"}]
        index_md = wiki_dir / "index.md"
        stale_md = wiki_dir / "STALE.md"
        has_index = index_md.exists()
        has_stale = stale_md.exists()
        mark = _status_mark(has_index, warn=has_stale)
        detail = f"{len(pages)} pages"
        if has_stale:
            detail += " (STALE.md present)"
        if not has_index:
            detail += " (index.md MISSING — route_hook won't fire)"
        print(f"  {mark} Wiki:                         {detail}")
    else:
        print(f"  ⚠ Wiki not created yet           (run /bootstrap-wiki)")

    # M4: needs_synthesis flag — user-facing reminder surfaced by /search skill.
    flag_path = project_path / _NEEDS_SYNTHESIS_FLAG
    if flag_path.exists():
        import json
        try:
            payload = json.loads(flag_path.read_text(encoding="utf-8"))
            count = payload.get("stale_count", "?")
            mods = payload.get("stale_modules", [])[:3]
            mod_preview = ", ".join(mods) + ("…" if len(payload.get("stale_modules", [])) > 3 else "")
            print(f"  ⚠ needs_synthesis:               {count} module(s) pending — run /maintain")
            if mod_preview:
                print(f"    Pending: {mod_preview}")
        except (json.JSONDecodeError, OSError):
            print(f"  ⚠ needs_synthesis flag present (unreadable) — run /maintain")

    # post-commit + post-checkout hooks (respect core.hooksPath — Husky compat)
    hooks_dir = _git_hooks_dir(project_path)
    for hook_name in ("post-commit", "post-checkout"):
        hook_path = hooks_dir / hook_name
        hook_ok = hook_path.exists() and _HOOK_IDENTITY_MARKER in hook_path.read_text()
        label = f"{hook_name} hook:"
        print(
            f"  {_status_mark(hook_ok)} {label:<30s}"
            f"{'installed' if hook_ok else 'MISSING'}"
        )

    # .gitignore
    gi_path = project_path / ".gitignore"
    required_gi = [".hybrid-search/wiki/", ".hybrid-search/coverage.json"]
    if gi_path.exists():
        content = gi_path.read_text()
        missing = [e for e in required_gi if e not in content]
        mark = _status_mark(not missing, warn=bool(missing))
        detail = "complete" if not missing else f"missing: {', '.join(missing)}"
        print(f"  {mark} .gitignore:                   {detail}")
    else:
        print("  ⚠ .gitignore missing             (consider adding .hybrid-search/wiki/)")

    # CLAUDE.md (routing section bounded by the hybrid-search marker)
    claude_md = project_path / "CLAUDE.md"
    if claude_md.exists():
        has_routing = _CLAUDE_MD_MARKER in claude_md.read_text(encoding="utf-8")
        print(f"  {_status_mark(has_routing, warn=not has_routing)} CLAUDE.md routing:            "
              f"{'present' if has_routing else 'marker missing — run install-hook'}")
    else:
        print("  ⚠ CLAUDE.md not found            (run install-hook to create)")


def cmd_status(args: argparse.Namespace) -> None:
    """Show hybrid-search-mcp installation + project health."""
    _check_global_status()

    cwd = Path(args.cwd).resolve() if hasattr(args, "cwd") else Path.cwd()
    # If --cwd is a git repo (or is ".") show current project health
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            project_root = Path(result.stdout.strip())
            _check_project_status(project_root)
    except Exception:
        pass

    # Also list all registered projects
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    projects = registry.list_all()
    if projects:
        print(f"\nAll registered projects ({len(projects)}):")
        for p in projects:
            print(f"  {p.name}: {p.file_count} files, {p.chunk_count} chunks — {p.path}")
    else:
        print("\nNo indexed projects yet.")


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
    wiki_files = [f for f in wiki_files if f.name not in ("index.md", "STALE.md")]

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

        # Build coverage from the wiki files we just synced (disk-authoritative)
        _write_wiki_coverage_from_files(db, pinfo.id, wiki_files, wiki_dir)
        print(f"Done: {synced} wiki pages synced to DB.")
    finally:
        db.close()


def _write_wiki_coverage_from_files(
    db: StoreDB, project_id: str, wiki_files: list[Path], wiki_dir: Path,
) -> None:
    """Write coverage.json based on the wiki files present on disk (not DB).

    Only considers dependencies of pages that actually exist as files,
    avoiding stale DB entries from deleted pages inflating coverage.
    """
    import json as _json_cov

    covered_files: set[str] = set()
    for wiki_file in wiki_files:
        content = wiki_file.read_text()
        deps = _resolve_wiki_deps(db, project_id, content)
        for dep in deps:
            file_rec = db.get_file(dep["file_id"])
            if file_rec:
                covered_files.add(file_rec.relative_path)

    sorted_files = sorted(covered_files)
    covered_dirs = sorted({
        str(Path(f).parent) for f in sorted_files if str(Path(f).parent) != "."
    })
    coverage_data = {
        "covered_files": sorted_files,
        "covered_dirs": covered_dirs,
        "total_pages": len(wiki_files),
        "total_covered_files": len(sorted_files),
    }
    (wiki_dir.parent / "coverage.json").write_text(
        _json_cov.dumps(coverage_data, ensure_ascii=False), encoding="utf-8"
    )


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


def _write_wiki_coverage(db: StoreDB, pages, wiki_dir: Path) -> None:
    """Write coverage.json — authoritative list of files covered by wiki."""
    import json as _json_cov

    covered_files = sorted({
        db.get_file(fid).relative_path
        for page in pages
        for fid in page.file_ids
        if db.get_file(fid) is not None
    })
    covered_dirs = sorted({
        str(Path(f).parent) for f in covered_files if str(Path(f).parent) != "."
    })
    coverage_data = {
        "covered_files": covered_files,
        "covered_dirs": covered_dirs,
        "total_pages": len(pages),
        "total_covered_files": len(covered_files),
    }
    (wiki_dir.parent / "coverage.json").write_text(
        _json_cov.dumps(coverage_data, ensure_ascii=False), encoding="utf-8"
    )


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

        extracted = sum(1 for e in edges if e["confidence"] == "extracted")
        inferred = sum(1 for e in edges if e["confidence"] == "inferred")
        resolved = sum(1 for e in edges if e["callee_chunk_id"] is not None)
        unresolved = total - resolved

        # Module-linked edges: import-call binding success (project-internal candidates)
        with_mod = [e for e in edges if e.get("callee_module")]
        mod_resolved = sum(1 for e in with_mod if e["callee_chunk_id"])
        without_mod = [e for e in edges if not e.get("callee_module")]
        nomod_resolved = sum(1 for e in without_mod if e["callee_chunk_id"])

        # "Project dependency edges" = extracted + inferred (useful for CodeWiki / topo sort)
        project_deps = extracted + inferred

        print(f"Project: {name}")
        print(f"  Total edges:       {total}")
        print(f"  Project deps:      {project_deps} (extracted {extracted} + inferred {inferred})")
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

        _write_wiki_coverage(db, pages, wiki_dir)
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


def cmd_detect_wiki_gaps(args: argparse.Namespace) -> None:
    """Detect modules without wiki coverage — pure filesystem + git, no DB/embedding needed."""
    import json as json_mod
    import subprocess

    cwd = Path(args.cwd).resolve()
    gaps_dir = cwd / ".hybrid-search"
    wiki_dir = gaps_dir / "wiki"
    coverage_json = gaps_dir / "coverage.json"

    if not wiki_dir.exists():
        # No wiki at all — write a bootstrap-needed gap
        import json as _json_gap
        gaps_dir.mkdir(parents=True, exist_ok=True)
        msg = "Wiki not initialized. Run /maintain or /bootstrap-wiki to generate wiki pages.\n"
        (gaps_dir / "wiki-gaps.txt").write_text(msg)
        (gaps_dir / "wiki-gaps.json").write_text(
            _json_gap.dumps({"status": "bootstrap-needed", "gaps": []}, ensure_ascii=False)
        )
        if not args.quiet:
            print(msg.strip())
        return

    # 1. Collect existing wiki coverage
    covered_dirs: set[str] = set()
    if coverage_json.exists():
        # Prefer authoritative coverage.json
        try:
            cov_data = json_mod.loads(coverage_json.read_text())
            covered_dirs = set(cov_data.get("covered_dirs", []))
        except Exception:
            pass

    if not covered_dirs:
        # Fallback: scan wiki files for file paths
        for wiki_file in wiki_dir.glob("*.md"):
            if wiki_file.name in ("index.md", "STALE.md"):
                continue
            try:
                content = wiki_file.read_text(errors="replace")
                for line in content.splitlines():
                    if line.strip().startswith("- `") and "`" in line[3:]:
                        fpath = line.strip()[3:].split("`")[0]
                        d = str(Path(fpath).parent)
                        if d and d != ".":
                            covered_dirs.add(d)
            except Exception:
                continue

    # 2. Find new/changed files from git (last commit, or all tracked files)
    try:
        if args.git_delta:
            result = subprocess.run(
                ["git", "diff", "--name-status", "--diff-filter=A", "HEAD~1..HEAD"],
                cwd=str(cwd), capture_output=True, text=True, timeout=5,
            )
            new_files = [
                line.split("\t", 1)[1] for line in result.stdout.strip().splitlines()
                if "\t" in line
            ]
        else:
            # Full scan: all tracked source files
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=str(cwd), capture_output=True, text=True, timeout=10,
            )
            source_exts = {".ts", ".tsx", ".js", ".jsx", ".py", ".rb", ".go", ".rs", ".java", ".swift", ".kt", ".sql"}
            new_files = [
                f for f in result.stdout.strip().splitlines()
                if Path(f).suffix in source_exts
            ]
    except Exception:
        return  # git not available, skip

    if not new_files:
        return

    # 3. Group files by directory
    dir_files: dict[str, list[str]] = {}
    for f in new_files:
        d = str(Path(f).parent)
        if d and d != ".":
            dir_files.setdefault(d, []).append(f)

    # 4. Find directories not covered by any wiki page
    uncovered: dict[str, list[str]] = {}
    for d, files in dir_files.items():
        # Check if this dir or any parent is covered
        parts = Path(d).parts
        is_covered = False
        for i in range(len(parts), 0, -1):
            ancestor = str(Path(*parts[:i]))
            if ancestor in covered_dirs:
                is_covered = True
                break
        if not is_covered:
            uncovered[d] = files

    # 5. Collapse child directories into parent groups
    groups: dict[str, list[str]] = {}
    sorted_dirs = sorted(uncovered.keys())
    for d in sorted_dirs:
        # Find if a parent dir is already a group
        merged = False
        for parent in list(groups.keys()):
            if d.startswith(parent + "/"):
                groups[parent].extend(uncovered[d])
                merged = True
                break
        if not merged:
            groups[d] = list(uncovered[d])

    # 6. Filter: only report groups with 3+ files
    significant = {d: files for d, files in groups.items() if len(files) >= 3}

    gaps_txt = gaps_dir / "wiki-gaps.txt"
    gaps_json = gaps_dir / "wiki-gaps.json"
    gaps_dir.mkdir(parents=True, exist_ok=True)

    if not significant:
        if gaps_txt.exists():
            gaps_txt.write_text("")
        if gaps_json.exists():
            gaps_json.write_text("{}")
        return

    # 7. Write results
    missing = [
        {"module_root": d, "file_count": len(files), "sample_files": files[:2]}
        for d, files in sorted(significant.items(), key=lambda x: -len(x[1]))
    ]

    gaps_data = {"gaps": missing, "total_missing": len(missing)}
    gaps_json.write_text(json_mod.dumps(gaps_data, indent=2, ensure_ascii=False))

    lines = [f"New modules without wiki coverage ({len(missing)}):"]
    for m in missing[:10]:
        lines.append(f"  - {m['module_root']} ({m['file_count']} files)")
    if len(missing) > 10:
        lines.append(f"  ... and {len(missing) - 10} more")
    lines.append("Run /maintain to generate wiki pages.")
    gaps_txt.write_text("\n".join(lines) + "\n")

    if not args.quiet:
        print("\n".join(lines))


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

                # M4: clear the needs_synthesis flag if no pages are stale anymore.
                # Finalize rewrites file_hash_at_compile, so a fresh staleness
                # check reflects the post-finalize state.
                remaining = [
                    p for p in wiki_store.check_staleness(pinfo.id) if p["stale"]
                ]
                project_root = Path(project_path)
                if not remaining:
                    if _clear_needs_synthesis_flag(project_root):
                        print("needs_synthesis flag cleared.")
                else:
                    # Still stale — refresh the flag so the module list is accurate.
                    _write_needs_synthesis_flag(project_root, remaining)
                    print(f"needs_synthesis flag updated: {len(remaining)} module(s) still pending.")
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


def _resolve_chunk_for_graph(
    db: StoreDB, project_id: str, token: str
) -> str | None:
    """Resolve a chunk_id OR symbol token to a single chunk_id for graph CLI.

    Priority: raw chunk_id → exact qualified_name → exact name → fuzzy LIKE.
    Returns None if unresolved.
    """
    chunk = db.get_chunk(token)
    if chunk:
        return chunk.id
    chunk = db.find_chunk_by_qualified_name(token, project_id)
    if chunk:
        return chunk.id
    chunks = db.find_chunks_by_name(token, project_id)
    if chunks:
        return chunks[0].id
    chunks = db.search_chunks_by_name(token, project_id)
    if chunks:
        return chunks[0].id
    return None


def _open_single_project_db(
    project: str | None, cwd: str
) -> tuple[object, StoreDB] | None:
    """Open DB for a single project (by name, or auto-detect from cwd). Caller closes."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    if project:
        pinfo = registry.get_by_name(project)
        if not pinfo:
            print(f"Project '{project}' not found", file=sys.stderr)
            return None
    else:
        match = _detect_project(registry, str(Path(cwd).resolve()))
        if not match:
            print(
                "No indexed project detected for this directory. Use --project NAME.",
                file=sys.stderr,
            )
            return None
        pinfo = registry.get_by_name(match[0])
        if not pinfo:
            print(f"Project '{match[0]}' missing from registry", file=sys.stderr)
            return None

    project_dir = get_project_dir(config.projects_dir, pinfo.id)
    idx_paths = IndexPaths(project_dir)
    if not idx_paths.store_db.exists():
        print(f"No index found for '{pinfo.name}'. Run `hybrid-search-mcp index`.", file=sys.stderr)
        return None
    return pinfo, StoreDB(idx_paths.store_db)


def cmd_god_nodes(args: argparse.Namespace) -> None:
    """List top-N authority chunks (most-called functions/classes) in a project."""
    import json as _json

    opened = _open_single_project_db(args.project, args.cwd)
    if not opened:
        sys.exit(1)
    pinfo, db = opened

    try:
        rows = db.get_god_nodes(
            pinfo.id, limit=args.top, min_confidence=args.min_confidence
        )
    finally:
        db.close()

    if args.json:
        print(_json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if not rows:
        print(f"No god nodes found in '{pinfo.name}' (min_confidence={args.min_confidence}).")
        return

    print(f"Top {len(rows)} god nodes in '{pinfo.name}' (min_confidence={args.min_confidence}):")
    print()
    width = len(str(rows[0]["in_degree"]))
    for i, r in enumerate(rows, 1):
        name = r.get("qualified_name") or r.get("name") or r["id"]
        loc = f"{r['relative_path']}:{r['start_line']}"
        deg = str(r["in_degree"]).rjust(width)
        score = r.get("max_score") or 0.0
        print(f"  {i:>2}. in={deg}  score={score:.2f}  {name}  ({r.get('node_type', '?')})")
        print(f"      {loc}")


# ── annotate-wiki (M5) — inject god-nodes into wiki index.md ──

_WIKI_GOD_START = "<!-- hybrid-search:god-nodes:start -->"
_WIKI_GOD_END = "<!-- hybrid-search:god-nodes:end -->"


def _module_slug(name: str) -> str:
    """Slugify a module name the same way dag.generate_all_wiki_pages does."""
    return name.lower().replace(" ", "-").replace("(", "").replace(")", "")


def _build_chunk_module_map(db: StoreDB, project_id: str) -> dict[str, str]:
    """Map chunk_id → wiki module name (including isolated modules).

    Re-runs the wiki plan (graph queries only, no LLM). Used to produce
    `[[module]]` wikilinks in the annotated god-nodes section.
    """
    from hybrid_search.index.dag import generate_wiki_plan

    plan = generate_wiki_plan(db, project_id)
    chunk_to_module: dict[str, str] = {}
    for module in list(plan.modules) + list(plan.isolated_modules):
        for cid in module.chunks:
            chunk_to_module[cid] = module.name
    return chunk_to_module


def _format_god_nodes_section(
    rows: list[dict],
    chunk_to_module: dict[str, str],
    top: int,
) -> str:
    """Render god-nodes rows as a marker-bounded markdown section.

    Returns "" if rows is empty — caller should skip insertion entirely
    so we don't leave behind an empty section.
    """
    if not rows:
        return ""

    lines: list[str] = [_WIKI_GOD_START]
    lines.append(f"## 핵심 모듈 (God Nodes Top {min(top, len(rows))})")
    lines.append("")
    lines.append(
        "> 호출 그래프 in-degree 기준 상위 심볼. "
        "`hybrid-search-mcp god-nodes` 서브커맨드로 재생성됩니다."
    )
    lines.append("")
    for i, r in enumerate(rows[:top], 1):
        symbol = r.get("qualified_name") or r.get("name") or r["id"]
        node_type = r.get("node_type") or "?"
        in_deg = r.get("in_degree", 0)
        module = chunk_to_module.get(r["id"])
        if module:
            slug = _module_slug(module)
            # Wiki-style link — rendered by the wiki reader; plain [text](slug.md)
            # also works for agents that resolve markdown links.
            prefix = f"[[{module}]]({slug}.md)"
        else:
            prefix = "_(unscoped)_"
        lines.append(
            f"{i}. {prefix} — `{symbol}` (in={in_deg}, type={node_type})"
        )
    lines.append("")
    lines.append(_WIKI_GOD_END)
    return "\n".join(lines)


def _apply_god_nodes_to_index(existing: str, section: str) -> str:
    """Insert or replace the marker-bounded god-nodes section in index.md.

    - If section is "" and no existing block: return unchanged.
    - If section is "" and an existing block is present: strip the block.
    - If existing block present: replace in place.
    - Otherwise: insert after the first H1 (`# ...`) heading; if no H1,
      prepend to the file.

    The marker pair guarantees idempotency — running twice produces no diff.
    Content outside the markers is never touched.
    """
    import re as _re

    pattern = _re.compile(
        _re.escape(_WIKI_GOD_START) + r".*?" + _re.escape(_WIKI_GOD_END),
        flags=_re.DOTALL,
    )

    if not section:
        # Remove existing block (if any); collapse a stray blank line pair.
        stripped = pattern.sub("", existing)
        return _re.sub(r"\n{3,}", "\n\n", stripped)

    if pattern.search(existing):
        return pattern.sub(lambda _m: section, existing)

    # Insert after first H1 heading, preserving any blank line after it.
    lines = existing.split("\n") if existing else []
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = i + 1
            # Skip a single blank line after the H1 for clean spacing.
            if insert_at < len(lines) and not lines[insert_at].strip():
                insert_at += 1
            break

    block = ["", section, ""]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    # Normalize: drop accidental triple blank lines.
    joined = "\n".join(new_lines)
    return _re.sub(r"\n{3,}", "\n\n", joined)


def cmd_annotate_wiki(args: argparse.Namespace) -> None:
    """Annotate .hybrid-search/wiki/index.md with the god-nodes Top-N section.

    Idempotent: re-running replaces the marker-bounded block in place.
    Empty result (no god nodes) strips any existing block so we never
    leave a stale empty section behind.
    """
    opened = _open_single_project_db(args.project, args.cwd)
    if not opened:
        sys.exit(1)
    pinfo, db = opened

    try:
        rows = db.get_god_nodes(
            pinfo.id, limit=args.top, min_confidence=args.min_confidence
        )
        chunk_to_module = _build_chunk_module_map(db, pinfo.id) if rows else {}
    finally:
        db.close()

    section = _format_god_nodes_section(rows, chunk_to_module, args.top)

    project_root = Path(pinfo.path)
    wiki_dir = project_root / ".hybrid-search" / "wiki"
    index_path = wiki_dir / "index.md"

    if not index_path.exists():
        if not rows:
            print(f"No god nodes and no index.md — nothing to do ({index_path}).")
            return
        # First-time bootstrap: create a minimal index.md so we have a host.
        wiki_dir.mkdir(parents=True, exist_ok=True)
        existing = "# Wiki Index\n"
    else:
        existing = index_path.read_text(encoding="utf-8")

    updated = _apply_god_nodes_to_index(existing, section)

    if updated == existing:
        print(f"Wiki god-nodes section unchanged: {index_path}")
        return

    # Preserve trailing newline convention — markdown files usually end with \n.
    if existing.endswith("\n") and not updated.endswith("\n"):
        updated += "\n"
    index_path.write_text(updated, encoding="utf-8")
    if not rows:
        print(f"Wiki god-nodes section removed (no nodes found): {index_path}")
    else:
        print(f"Wiki god-nodes section updated ({len(rows[:args.top])} entries): {index_path}")


# ── qa (Sprint 2) — read-side of the Memory Layer ──────────────────────

def _resolve_qa_root(args: argparse.Namespace) -> Path | None:
    """Resolve project root for qa commands. Prefers --project, falls back to
    a registry match on --cwd, then the raw cwd path.

    Returns None and prints to stderr when --project is given but not registered.
    """
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    if getattr(args, "project", None):
        pinfo = registry.get_by_name(args.project)
        if pinfo is None:
            print(f"Project not found: {args.project}", file=sys.stderr)
            return None
        return Path(pinfo.path)
    detected = _detect_project(registry, getattr(args, "cwd", "."))
    if detected is not None:
        return Path(detected[1])
    return Path(getattr(args, "cwd", ".")).resolve()


def _truncate_preview(text: str, limit: int = 72) -> str:
    flat = text.replace("\n", " ").strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _iter_all_project_roots() -> Iterator[tuple[str, Path]]:
    """Yield (project_name, project_root) for every registered project.

    Silent on the home directory (same policy as _detect_project).
    """
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    home = Path.home().resolve()
    for pinfo in registry.list_all():
        root = Path(pinfo.path)
        if root.resolve() == home:
            continue
        yield pinfo.name, root


def cmd_qa_list(args: argparse.Namespace) -> None:
    """List recent qa logs. --all sweeps every registered project."""
    from hybrid_search.memory import reader

    # Sources: either a single project or the whole registry.
    sources: list[tuple[str | None, Path]]
    if getattr(args, "all", False):
        if args.project or (args.cwd and args.cwd != "."):
            print("--all is mutually exclusive with --project/--cwd", file=sys.stderr)
            sys.exit(1)
        sources = [(name, root) for name, root in _iter_all_project_roots()]
    else:
        root = _resolve_qa_root(args)
        if root is None:
            sys.exit(1)
        sources = [(None, root)]

    cutoff: datetime | None = None
    if args.since:
        try:
            cutoff = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"Invalid --since date: {args.since}", file=sys.stderr)
            sys.exit(1)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)

    # Aggregate newest-first across all sources.
    pooled: list[tuple[str | None, reader.QAIndex]] = []
    for label, root in sources:
        for idx in reader.iter_qa_indexes(root):
            if cutoff and (idx.timestamp is None or idx.timestamp < cutoff):
                continue
            pooled.append((label, idx))
    pooled.sort(
        key=lambda pair: pair[1].timestamp or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    pooled = pooled[: args.limit]

    if args.json:
        import json as _json

        print(_json.dumps(
            [
                {
                    "project": label,
                    "id": i.id,
                    "query": i.query,
                    "query_type": i.query_type,
                    "bm25_weight": i.effective_bm25_weight,
                    "timestamp": i.timestamp.isoformat() if i.timestamp else None,
                    "result_count": i.result_count,
                    "path": str(i.path),
                }
                for label, i in pooled
            ],
            ensure_ascii=False,
            indent=2,
        ))
        return

    if not pooled:
        if getattr(args, "all", False):
            print("No qa logs across registered projects.")
        else:
            print(f"No qa logs under {sources[0][1] / reader.QA_DIRNAME}")
        return

    for label, idx in pooled:
        ts = idx.timestamp.strftime("%Y-%m-%d %H:%M") if idx.timestamp else "?"
        preview = _truncate_preview(idx.query)
        prefix = f"{label}:" if label else ""
        print(f"{prefix}{idx.id}  [{idx.query_type:<14}] n={idx.result_count}  {ts}  {preview}")


def cmd_qa_show(args: argparse.Namespace) -> None:
    """Print a single qa log by id / stem / hash prefix."""
    from hybrid_search.memory import reader

    root = _resolve_qa_root(args)
    if root is None:
        sys.exit(1)

    idx = reader.find_qa_by_id(root, args.id)
    if idx is None:
        print(f"qa log not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(idx.path.read_text(encoding="utf-8"))


def cmd_qa_grep(args: argparse.Namespace) -> None:
    """Grep across qa log frontmatter + body. Newest files first."""
    from hybrid_search.memory import reader

    root = _resolve_qa_root(args)
    if root is None:
        sys.exit(1)

    hits = list(reader.grep_qa(root, args.term, case_insensitive=not args.case_sensitive))
    if not hits:
        sys.exit(1)  # ripgrep-style: non-zero when no matches

    for hit in hits:
        print(f"{hit.index.id}:{hit.lineno}:{hit.line}")


def cmd_qa_prune(args: argparse.Namespace) -> None:
    """Delete qa logs older than --older-than / --before. --dry-run supported."""
    from hybrid_search.memory import reader

    if (args.older_than is None) == (args.before is None):
        print("pass exactly one of --older-than / --before", file=sys.stderr)
        sys.exit(2)

    root = _resolve_qa_root(args)
    if root is None:
        sys.exit(1)

    try:
        cutoff = reader.resolve_cutoff(
            older_than=args.older_than, before=args.before
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    result = reader.prune_older_than(root, cutoff, dry_run=args.dry_run)
    tag = "would remove" if args.dry_run else "removed"
    print(f"{tag} {len(result.deleted)} qa log(s) older than {cutoff.isoformat()}")
    if args.verbose:
        for p in result.deleted:
            print(f"  {tag}: {p}")
    if result.skipped:
        print(f"  skipped {len(result.skipped)} (unlink failed)")
    if result.dirs_removed:
        print(f"  also removed {len(result.dirs_removed)} empty directory/ies")


def cmd_qa_stats(args: argparse.Namespace) -> None:
    """Summary stats over a project's qa logs."""
    from collections import Counter
    from hybrid_search.memory import reader

    root = _resolve_qa_root(args)
    if root is None:
        sys.exit(1)

    indexes = list(reader.iter_qa_indexes(root))
    total = len(indexes)
    if total == 0:
        print(f"No qa logs under {root / reader.QA_DIRNAME}")
        return

    by_month: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    for idx in indexes:
        by_type[idx.query_type] += 1
        if idx.timestamp is not None:
            by_month[idx.timestamp.strftime("%Y-%m")] += 1

    print(f"qa logs under {root / reader.QA_DIRNAME}")
    print(f"  total:        {total}")
    print("  by query_type:")
    for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<16} {v}")
    print("  by month:")
    for k, v in sorted(by_month.items()):
        print(f"    {k}            {v}")


def _bfs_shortest_path(
    db: StoreDB, project_id: str, start: str, goal: str, min_confidence: str
) -> list[str] | None:
    """BFS on call_edges (caller → callee) from start to goal. Returns chunk_id path or None."""
    from collections import deque

    if start == goal:
        return [start]

    visited: set[str] = {start}
    parent: dict[str, str] = {}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        callees = db.get_callees(node, project_id, min_confidence)
        for callee in callees:
            cid = callee.get("callee_chunk_id")
            if not cid or cid in visited:
                continue
            visited.add(cid)
            parent[cid] = node
            if cid == goal:
                path = [cid]
                cur = cid
                while cur in parent:
                    cur = parent[cur]
                    path.append(cur)
                path.reverse()
                return path
            queue.append(cid)
    return None


def cmd_shortest_path(args: argparse.Namespace) -> None:
    """Find shortest call-graph path between two chunks/symbols."""
    import json as _json

    opened = _open_single_project_db(args.project, args.cwd)
    if not opened:
        sys.exit(1)
    pinfo, db = opened

    try:
        start_id = _resolve_chunk_for_graph(db, pinfo.id, args.source)
        goal_id = _resolve_chunk_for_graph(db, pinfo.id, args.target)
        if not start_id:
            print(f"Could not resolve source: {args.source}", file=sys.stderr)
            sys.exit(2)
        if not goal_id:
            print(f"Could not resolve target: {args.target}", file=sys.stderr)
            sys.exit(2)

        direction = "forward"
        path = _bfs_shortest_path(db, pinfo.id, start_id, goal_id, args.min_confidence)
        if path is None:
            # Try reverse direction (target → source via callees)
            reverse = _bfs_shortest_path(db, pinfo.id, goal_id, start_id, args.min_confidence)
            if reverse is not None:
                path = list(reversed(reverse))
                direction = "reverse"

        if path is None:
            if args.json:
                print(_json.dumps({"found": False, "source": start_id, "target": goal_id}))
            else:
                print(f"No path between {args.source} and {args.target}.")
            return

        nodes = []
        for cid in path:
            chunk = db.get_chunk(cid)
            if not chunk:
                nodes.append({"chunk_id": cid, "name": None})
                continue
            file_rec = db.get_file(chunk.file_id) if chunk.file_id else None
            nodes.append({
                "chunk_id": cid,
                "name": chunk.name,
                "qualified_name": chunk.qualified_name,
                "node_type": chunk.node_type,
                "file_path": file_rec.relative_path if file_rec else None,
                "start_line": chunk.start_line,
            })
    finally:
        db.close()

    if args.json:
        print(_json.dumps({
            "found": True,
            "direction": direction,
            "hops": len(nodes) - 1,
            "path": nodes,
        }, ensure_ascii=False, indent=2))
        return

    arrow = "→" if direction == "forward" else "←"
    print(f"Shortest path ({direction}, {len(nodes) - 1} hops):")
    print()
    for i, n in enumerate(nodes):
        name = n.get("qualified_name") or n.get("name") or n["chunk_id"]
        loc = f"{n.get('file_path', '?')}:{n.get('start_line') or '?'}"
        prefix = "    " if i == 0 else f"  {arrow} "
        print(f"{prefix}{name}")
        print(f"      {loc}")


def cmd_subgraph(args: argparse.Namespace) -> None:
    """Dump N-hop forward + reverse call graph around a chunk/symbol."""
    import json as _json
    from collections import deque

    opened = _open_single_project_db(args.project, args.cwd)
    if not opened:
        sys.exit(1)
    pinfo, db = opened

    try:
        root_id = _resolve_chunk_for_graph(db, pinfo.id, args.symbol)
        if not root_id:
            print(f"Could not resolve: {args.symbol}", file=sys.stderr)
            sys.exit(2)

        # Bidirectional BFS — callees (forward) + callers (reverse).
        def bfs(get_neighbors, id_key: str) -> list[dict]:
            nodes: list[dict] = []
            visited: set[str] = {root_id}
            queue: deque[tuple[str, int]] = deque([(root_id, 0)])
            while queue:
                node, depth = queue.popleft()
                if depth >= args.hops:
                    continue
                for edge in get_neighbors(node, pinfo.id, args.min_confidence):
                    nid = edge.get(id_key)
                    if not nid:
                        continue
                    if nid in visited:
                        continue
                    visited.add(nid)
                    nodes.append({
                        "chunk_id": nid,
                        "name": edge.get("name"),
                        "qualified_name": edge.get("qualified_name"),
                        "node_type": edge.get("node_type"),
                        "file_path": edge.get("relative_path"),
                        "start_line": edge.get("start_line"),
                        "confidence": edge.get("confidence"),
                        "depth": depth + 1,
                        "parent": node,
                    })
                    queue.append((nid, depth + 1))
            return nodes

        callees = bfs(db.get_callees, "callee_chunk_id")
        callers = bfs(db.get_callers, "caller_chunk_id")

        root_chunk = db.get_chunk(root_id)
        root_file = db.get_file(root_chunk.file_id) if root_chunk and root_chunk.file_id else None
    finally:
        db.close()

    if args.json:
        print(_json.dumps({
            "root": {
                "chunk_id": root_id,
                "name": root_chunk.name if root_chunk else None,
                "qualified_name": root_chunk.qualified_name if root_chunk else None,
                "file_path": root_file.relative_path if root_file else None,
                "start_line": root_chunk.start_line if root_chunk else None,
            },
            "hops": args.hops,
            "callees": callees,
            "callers": callers,
        }, ensure_ascii=False, indent=2))
        return

    root_name = (root_chunk.qualified_name if root_chunk else None) or args.symbol
    print(f"Subgraph ({args.hops}-hop) around {root_name}:")
    print(f"  {root_file.relative_path if root_file else '?'}:{root_chunk.start_line if root_chunk else '?'}")
    print()
    print(f"Callees (forward, {len(callees)}):")
    for n in callees[:50]:
        name = n.get("qualified_name") or n.get("name") or n["chunk_id"]
        print(f"  d={n['depth']}  {name}  ({n.get('file_path', '?')}:{n.get('start_line') or '?'})")
    if len(callees) > 50:
        print(f"  ... and {len(callees) - 50} more")
    print()
    print(f"Callers (reverse, {len(callers)}):")
    for n in callers[:50]:
        name = n.get("qualified_name") or n.get("name") or n["chunk_id"]
        print(f"  d={n['depth']}  {name}  ({n.get('file_path', '?')}:{n.get('start_line') or '?'})")
    if len(callers) > 50:
        print(f"  ... and {len(callers) - 50} more")


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
    has_gaps_check = any(
        "wiki-gaps" in str(h.get("hooks", [{}])[0].get("command", ""))
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Read|Edit|Write"
    )
    has_route_hook = any(
        "wiki/index.md" in str(h.get("hooks", [{}])[0].get("command", ""))
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Glob|Grep"
    )

    if has_auto_index and has_stale_check and has_gaps_check and has_route_hook:
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
                    f'nohup sh -c \'"$1" -m hybrid_search.cli reindex --synthesize --cwd "$2" && '
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
        gaps_hook = {
            "matcher": "Read|Edit|Write",
            "hooks": [{
                "type": "command",
                "command": (
                    'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
                    '[ -n "$ROOT" ] && [ -s "$ROOT/.hybrid-search/wiki-gaps.txt" ] && '
                    '{ [ ! -f "$ROOT/.hybrid-search/.gaps-shown" ] || '
                    '[ "$ROOT/.hybrid-search/wiki-gaps.txt" -nt "$ROOT/.hybrid-search/.gaps-shown" ]; } && '
                    'cat "$ROOT/.hybrid-search/wiki-gaps.txt" && '
                    'touch "$ROOT/.hybrid-search/.gaps-shown"'
                ),
            }],
        }
        # Routing hook: before Grep/Glob, remind Claude of wiki when index exists.
        # Gate: .hybrid-search/wiki/index.md present → emit additionalContext JSON.
        # Scope: current project only. Cross-project queries use hybrid_search(project=...).
        route_hook = {
            "matcher": "Glob|Grep",
            "hooks": [{
                "type": "command",
                "command": (
                    'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
                    '[ -n "$ROOT" ] && [ -f "$ROOT/.hybrid-search/wiki/index.md" ] && '
                    'echo \'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
                    '"additionalContext":"hybrid-search: 이 프로젝트에 wiki 인덱스가 있습니다. '
                    '구조/관계/설계 질문은 .hybrid-search/wiki/index.md 먼저 확인하세요. '
                    '한국어 자연어 질의는 mcp__hybrid-search__hybrid_search 도구 사용. '
                    '다른 프로젝트 참조 시 project 파라미터 지원."}}\''
                ),
            }],
        }

        # Remove old hybrid-search hooks, keep others
        new_pre = [h for h in pre_hooks if not (
            isinstance(h, dict) and (
                "hybrid-search/wiki" in str(h.get("hooks", [{}])[0].get("command", ""))
                or "STALE.md" in str(h.get("hooks", [{}])[0].get("command", ""))
                or "wiki-gaps" in str(h.get("hooks", [{}])[0].get("command", ""))
                or "wiki/index.md" in str(h.get("hooks", [{}])[0].get("command", ""))
            )
        )]
        new_pre.extend([auto_index_hook, stale_hook, gaps_hook, route_hook])
        hooks["PreToolUse"] = new_pre

        # Remove old PostToolUse wiki-gaps hook (moved to PreToolUse)
        post_hooks = hooks.get("PostToolUse", [])
        new_post = [h for h in post_hooks if not (
            isinstance(h, dict)
            and "wiki-gaps.txt" in str(h.get("hooks", [{}])[0].get("command", ""))
        )]
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


def _git_hooks_dir(repo_root: Path) -> Path:
    """Resolve the git hooks directory, respecting ``core.hooksPath`` (Husky compat).

    Order of resolution:
    1. ``git config --get core.hooksPath`` — absolute or repo-relative.
    2. ``git rev-parse --git-path hooks`` — correct for worktrees / submodules.
    3. Fallback: ``<repo_root>/.git/hooks``.
    """
    import subprocess
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            custom = Path(res.stdout.strip()).expanduser()
            return custom if custom.is_absolute() else (repo_root / custom).resolve()
    except Exception:
        pass
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            p = Path(res.stdout.strip())
            return p if p.is_absolute() else (repo_root / p).resolve()
    except Exception:
        pass
    return repo_root / ".git" / "hooks"


# Identity marker used to detect a previously-installed hybrid-search hook.
# Substring is stable across hook types — both post-commit and post-checkout
# contain this — so legacy installs that lacked a version marker are also
# recognized. Keep string literal in sync with the script templates below.
_HOOK_IDENTITY_MARKER = "hybrid_search.cli"


def _build_post_commit_script(venv_python: Path) -> str:
    """Build the post-commit hook body (delta reindex + gap detection).

    M3: captures ``git diff --name-status HEAD~1 HEAD`` synchronously at the
    commit moment and exports it via ``HYBRID_SEARCH_CHANGED_STATUS`` so the
    deferred background reindex sees the diff for *this* commit, not
    whatever HEAD~1 has become by the time nohup runs. Env is inherited by
    the ``nohup bash -c`` child.

    Initial commit: ``git rev-parse HEAD~1`` fails, env stays unset, and
    ``cmd_reindex --git-delta`` falls back to its internal full-scan path.
    """
    return f"""#!/bin/bash
# hybrid-search-mcp:post-commit — auto delta-reindex on commit (background, non-blocking)
PROJECT_DIR="$(git rev-parse --show-toplevel)"
mkdir -p "$PROJECT_DIR/.hybrid-search"

# M3: capture diff synchronously so deferred reindex sees THIS commit's diff,
# not a stale HEAD~1..HEAD recomputed later after a rapid follow-up commit.
if git rev-parse HEAD~1 >/dev/null 2>&1; then
  HOOK_DIFF="$(git diff --name-status HEAD~1 HEAD 2>/dev/null)"
  if [ -z "$HOOK_DIFF" ]; then
    exit 0  # no-op commit (e.g., amend with no changes) — nothing to reindex
  fi
  export HYBRID_SEARCH_CHANGED_STATUS="$HOOK_DIFF"
fi
# Initial commit: env stays unset → cmd_reindex falls back internally.

# 1. Gap detection — always runs, no lock, cheap
"{venv_python}" -m hybrid_search.cli detect-wiki-gaps --git-delta --quiet --cwd "$PROJECT_DIR" || true

# 2. Reindex — locked to prevent concurrent runs
LOCK_FILE="$PROJECT_DIR/.hybrid-search/.reindex.lock"
if [ -f "$LOCK_FILE" ]; then
  LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
  if kill -0 "$LOCK_PID" 2>/dev/null; then
    exit 0  # another reindex is running, skip
  fi
  rm -f "$LOCK_FILE"  # stale lock
fi
nohup bash -c '
  echo $$ > "'"$LOCK_FILE"'"
  "{venv_python}" -m hybrid_search.cli reindex --git-delta --wiki-scope affected --synthesize --cwd "'"$PROJECT_DIR"'" || true
  rm -f "'"$LOCK_FILE"'"
' > /dev/null 2>&1 &
"""


def _build_post_checkout_script(venv_python: Path) -> str:
    """Build the post-checkout hook body (branch-switch-only reindex).

    Gates:
    - ``$3 == "1"`` — only branch switches; file checkouts are skipped
    - ``.hybrid-search/`` must already exist — no auto-bootstrap on untracked
      projects

    Uses filesystem-delta reindex (not ``--git-delta``) because ``HEAD~1..HEAD``
    is not a meaningful reference post-switch — we want "HEAD vs last indexed
    state", which the hash/mtime prefilter already provides.
    """
    return f"""#!/bin/bash
# hybrid-search-mcp:post-checkout — auto delta-reindex on branch switch (background)

# Args: $1=prev_head, $2=new_head, $3=flag (1=branch switch, 0=file checkout)
[ "$3" = "1" ] || exit 0

PROJECT_DIR="$(git rev-parse --show-toplevel)"

# Skip when hybrid-search isn't initialized here (no auto-bootstrap)
[ -d "$PROJECT_DIR/.hybrid-search" ] || exit 0

# Shared lock with post-commit — only one reindex at a time
LOCK_FILE="$PROJECT_DIR/.hybrid-search/.reindex.lock"
if [ -f "$LOCK_FILE" ]; then
  LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
  if kill -0 "$LOCK_PID" 2>/dev/null; then
    exit 0
  fi
  rm -f "$LOCK_FILE"
fi
nohup bash -c '
  echo $$ > "'"$LOCK_FILE"'"
  "{venv_python}" -m hybrid_search.cli reindex --wiki-scope affected --cwd "'"$PROJECT_DIR"'" || true
  rm -f "'"$LOCK_FILE"'"
' > /dev/null 2>&1 &
"""


def _install_hook_file(
    hook_path: Path,
    hook_content: str,
    section_header: str,
) -> str:
    """Write ``hook_content`` to ``hook_path`` or append idempotently.

    Returns a short status string: ``installed``, ``appended``, or
    ``already-installed``. Makes the hook file executable.
    """
    status: str
    if hook_path.exists():
        existing = hook_path.read_text()
        if _HOOK_IDENTITY_MARKER in existing:
            status = "already-installed"
        else:
            with open(hook_path, "a") as f:
                f.write(f"\n# --- {section_header} ---\n")
                f.write(hook_content.split("\n", 1)[1])  # drop shebang on append
            status = "appended"
    else:
        hook_path.write_text(hook_content)
        status = "installed"

    hook_path.chmod(0o755)
    return status


def cmd_install_hook(args: argparse.Namespace) -> None:
    """Install post-commit + post-checkout hooks, respecting core.hooksPath."""
    import subprocess

    cwd = Path(args.cwd).resolve()

    # Verify this is a git repo first
    try:
        subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "--git-dir"],
            text=True,
        )
    except subprocess.CalledProcessError:
        print(f"Not a git repository: {cwd}")
        return

    hooks_dir = _git_hooks_dir(cwd)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Find our venv python (baked into hook content at install time)
    venv_python = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    commit_status = _install_hook_file(
        hooks_dir / "post-commit",
        _build_post_commit_script(venv_python),
        section_header="Hybrid Search auto-reindex (post-commit)",
    )
    checkout_status = _install_hook_file(
        hooks_dir / "post-checkout",
        _build_post_checkout_script(venv_python),
        section_header="Hybrid Search auto-reindex (post-checkout)",
    )

    print(f"post-commit:   {commit_status}  ({hooks_dir / 'post-commit'})")
    print(f"post-checkout: {checkout_status}  ({hooks_dir / 'post-checkout'})")
    print(
        "Hooks will auto-reindex on commit AND branch switch "
        "(background, non-blocking, shared lock)."
    )

    # Ensure .hybrid-search/ artifacts are git-ignored (per-machine wiki policy).
    _ensure_gitignore_entries(cwd)

    # Ensure CLAUDE.md has the routing section (idempotent — updates if present).
    _ensure_claude_md(str(cwd))


def _ensure_gitignore_entries(project_root: Path) -> None:
    """Add .hybrid-search/ artifacts to .gitignore if missing.

    Wiki pages and auxiliary files are machine-local (DB ↔ wiki consistency).
    This makes each machine own its wiki independently.
    """
    gi_path = project_root / ".gitignore"
    required = [
        ".hybrid-search/wiki/",
        ".hybrid-search/wiki-gaps.*",
        ".hybrid-search/coverage.json",
        ".hybrid-search/.reindex.lock",
        ".hybrid-search/.gaps-shown",
        ".hybrid-search/needs_synthesis",
        ".hybrid-search/qa/",
    ]
    existing = gi_path.read_text(encoding="utf-8") if gi_path.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    # Match exact lines; treat trailing-slash variants as equivalent for directory patterns.
    def _present(entry: str) -> bool:
        variants = {entry, entry.rstrip("/"), entry.rstrip("/") + "/"}
        return bool(variants & existing_lines)
    missing = [entry for entry in required if not _present(entry)]
    if not missing:
        return

    block = "\n# hybrid-search-mcp (auto-added — wiki is machine-local)\n" + "\n".join(missing) + "\n"
    new_content = (existing.rstrip() + "\n" + block) if existing else block.lstrip()
    gi_path.write_text(new_content, encoding="utf-8")
    print(f"Added {len(missing)} entries to .gitignore")


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
    p_status.add_argument("--cwd", default=".", help="Filter to project at this directory")

    p_stale = sub.add_parser("stale", help="Check wiki staleness")
    p_stale.add_argument("--cwd", default=".", help="Project directory")

    p_hook = sub.add_parser(
        "install-hook",
        help="Install post-commit + post-checkout hooks in a project",
    )
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

    p_gaps = sub.add_parser("detect-wiki-gaps", help="Detect modules without wiki coverage (filesystem + git only)")
    p_gaps.add_argument("--cwd", default=".", help="Project directory")
    p_gaps.add_argument("--git-delta", action="store_true", help="Only check files added in last commit")
    p_gaps.add_argument("--quiet", action="store_true", help="No stdout output, only write files")

    p_vsyn = sub.add_parser("verify-synthesis", help="Re-verify synthesized wiki pages (refs + symbols)")
    p_vsyn.add_argument("--cwd", default=".", help="Project directory")
    p_vsyn.add_argument("--json", action="store_true", help="Output as JSON")
    p_vsyn.add_argument("--fix", action="store_true", help="Auto-remove bad refs from DB content")

    # ── Graph exploration (M5) ──
    p_god = sub.add_parser("god-nodes", help="Top-N authority chunks (most-called functions/classes)")
    p_god.add_argument("--top", type=int, default=20, help="Number of god nodes to return")
    p_god.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_god.add_argument("--project", help="Project name (overrides --cwd)")
    p_god.add_argument(
        "--min-confidence",
        choices=("ambiguous", "inferred", "extracted"),
        default="inferred",
        help="Minimum call-edge confidence (default: inferred)",
    )
    p_god.add_argument("--json", action="store_true", help="Output as JSON")

    p_annot = sub.add_parser(
        "annotate-wiki",
        help="Inject god-nodes Top-N into .hybrid-search/wiki/index.md (idempotent)",
    )
    p_annot.add_argument("--top", type=int, default=10, help="Number of god nodes to show (default: 10)")
    p_annot.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_annot.add_argument("--project", help="Project name (overrides --cwd)")
    p_annot.add_argument(
        "--min-confidence",
        choices=("ambiguous", "inferred", "extracted"),
        default="inferred",
        help="Minimum call-edge confidence (default: inferred)",
    )

    p_path = sub.add_parser("shortest-path", help="Shortest call-graph path between two chunks/symbols")
    p_path.add_argument("source", help="Source chunk_id, qualified_name, or symbol name")
    p_path.add_argument("target", help="Target chunk_id, qualified_name, or symbol name")
    p_path.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_path.add_argument("--project", help="Project name (overrides --cwd)")
    p_path.add_argument(
        "--min-confidence",
        choices=("ambiguous", "inferred", "extracted"),
        default="inferred",
        help="Minimum call-edge confidence (default: inferred)",
    )
    p_path.add_argument("--json", action="store_true", help="Output as JSON")

    p_qa_list = sub.add_parser("qa-list", help="List recent qa logs (Memory Layer)")
    p_qa_list.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_list.add_argument("--project", help="Project name (overrides --cwd)")
    p_qa_list.add_argument("--limit", type=int, default=20, help="Max entries (default: 20)")
    p_qa_list.add_argument("--since", help="ISO date cutoff, e.g. 2026-04-01")
    p_qa_list.add_argument("--json", action="store_true", help="Output as JSON")
    p_qa_list.add_argument(
        "--all",
        action="store_true",
        help="Aggregate across every registered project (mutually exclusive with --project/--cwd)",
    )

    p_qa_show = sub.add_parser("qa-show", help="Print a single qa log by id / hash prefix")
    p_qa_show.add_argument("id", help="qa log id, file stem, or hash prefix (≥4 chars)")
    p_qa_show.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_show.add_argument("--project", help="Project name (overrides --cwd)")

    p_qa_grep = sub.add_parser("qa-grep", help="Grep across qa log frontmatter + body")
    p_qa_grep.add_argument("term", help="Substring to search for")
    p_qa_grep.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_grep.add_argument("--project", help="Project name (overrides --cwd)")
    p_qa_grep.add_argument("--case-sensitive", action="store_true", help="Respect case (default: off)")

    p_qa_stats = sub.add_parser("qa-stats", help="Summary of qa logs (total / by type / by month)")
    p_qa_stats.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_stats.add_argument("--project", help="Project name (overrides --cwd)")

    p_qa_prune = sub.add_parser(
        "qa-prune",
        help="Delete qa logs older than --older-than or --before",
    )
    p_qa_prune.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_prune.add_argument("--project", help="Project name (overrides --cwd)")
    p_qa_prune.add_argument(
        "--older-than",
        help="Relative duration: 30d / 12h / 2w / 3m",
    )
    p_qa_prune.add_argument(
        "--before",
        help="Absolute cutoff (ISO date): e.g. 2026-01-01",
    )
    p_qa_prune.add_argument("--dry-run", action="store_true", help="Print, don't delete")
    p_qa_prune.add_argument("--verbose", action="store_true", help="Print every path")

    p_sub = sub.add_parser("subgraph", help="N-hop forward+reverse call graph around a chunk/symbol")
    p_sub.add_argument("symbol", help="Chunk_id, qualified_name, or symbol name")
    p_sub.add_argument("--hops", type=int, default=2, help="Traversal depth (default: 2)")
    p_sub.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_sub.add_argument("--project", help="Project name (overrides --cwd)")
    p_sub.add_argument(
        "--min-confidence",
        choices=("ambiguous", "inferred", "extracted"),
        default="inferred",
        help="Minimum call-edge confidence (default: inferred)",
    )
    p_sub.add_argument("--json", action="store_true", help="Output as JSON")

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
    elif args.command == "detect-wiki-gaps":
        cmd_detect_wiki_gaps(args)
    elif args.command == "god-nodes":
        cmd_god_nodes(args)
    elif args.command == "annotate-wiki":
        cmd_annotate_wiki(args)
    elif args.command == "shortest-path":
        cmd_shortest_path(args)
    elif args.command == "subgraph":
        cmd_subgraph(args)
    elif args.command == "qa-list":
        cmd_qa_list(args)
    elif args.command == "qa-show":
        cmd_qa_show(args)
    elif args.command == "qa-grep":
        cmd_qa_grep(args)
    elif args.command == "qa-stats":
        cmd_qa_stats(args)
    elif args.command == "qa-prune":
        cmd_qa_prune(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
