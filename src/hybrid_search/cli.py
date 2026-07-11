"""CLI entrypoint for background indexing (git hook, no MCP overhead).

Usage:
    python -m hybrid_search.cli reindex [--cwd PATH] [--force]
    python -m hybrid_search.cli status
    python -m hybrid_search.cli stale [--cwd PATH]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.config import load_config
from hybrid_search.index.conversation_indexer import ConversationIndexer
from hybrid_search.index.dag import generate_all_wiki_pages
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.index.scanner import (
    excluded_paths_summary,
    get_changed_files_from_git,
    parse_git_diff_name_status,
)
from hybrid_search.memory.routing_template import (
    BEGIN_RE,
    LEGACY_CLAUDE_MARKER,
    ROUTING_BODY,
    agents_block,
    apply_update,
    claude_block,
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


def _load_gold_queries(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    queries = raw.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError(f"{path} does not contain a query list")
    return queries


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile with interpolation for deterministic calibration."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _calibrate_router_confidence(orchestrator, queries: list[dict], *, project, cwd, limit) -> dict[str, float]:
    top_scores: list[float] = []
    score_gaps: list[float] = []
    top_cosines: list[float] = []
    for item in queries:
        query = item.get("query") or item.get("prompt")
        if not query:
            continue
        if item.get("expected_tool") not in (None, "hybrid_search"):
            continue
        response = orchestrator.hybrid_search(
            query=query,
            project=project,
            cwd=cwd,
            limit=limit,
        )
        top_scores.append(float(response.top_score))
        # Calibrate the gap on the same quantity classification uses — the
        # different-file effective gap, not the raw top1-top2 spread.
        gap = getattr(response, "effective_gap", None)
        if gap is None:
            gap = response.score_gap
        if gap is not None:
            score_gaps.append(float(gap))
        cosine = getattr(response, "top_cosine", None)
        if cosine is not None:
            top_cosines.append(float(cosine))
    return {
        "strong_score": round(_percentile(top_scores, 0.67), 6),
        "strong_gap": round(_percentile(score_gaps, 0.67), 6),
        "weak_score": round(_percentile(top_scores, 0.33), 6),
        # Median semantic similarity of representative queries: a
        # weak-classified response whose best match clears what a typical
        # answerable query scores is a rescue candidate, not a miss.
        "cosine_anchor": round(_percentile(top_cosines, 0.5), 6),
    }


def _router_confidence_block(thresholds: dict[str, float]) -> str:
    return (
        "[router.confidence]\n"
        f"strong_score = {thresholds['strong_score']:.6f}\n"
        f"strong_gap = {thresholds['strong_gap']:.6f}\n"
        f"weak_score = {thresholds['weak_score']:.6f}\n"
        f"cosine_anchor = {thresholds.get('cosine_anchor', 0.0):.6f}\n"
    )


def _write_router_confidence_config(config_path: Path, thresholds: dict[str, float]) -> bool:
    """Write [router.confidence] idempotently. Returns True when bytes changed."""
    import re as _re

    config_path.parent.mkdir(parents=True, exist_ok=True)
    old = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    block = _router_confidence_block(thresholds)
    pattern = _re.compile(r"(?ms)^\[router\.confidence\]\n.*?(?=^\[|\Z)")
    if pattern.search(old):
        new = pattern.sub(block.rstrip("\n") + "\n\n", old).rstrip() + "\n"
    else:
        sep = "\n\n" if old.strip() else ""
        new = old.rstrip() + sep + block
    if new == old:
        return False
    config_path.write_text(new, encoding="utf-8")
    return True


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


_CLAUDE_MD_MARKER = LEGACY_CLAUDE_MARKER


def _claude_md_has_routing(text: str) -> bool:
    """True if CLAUDE.md carries a hybrid-search routing block.

    install-hook writes the versioned ``<!-- BEGIN hybrid-search-mcp routing
    vN -->`` marker; older installs used the legacy ``<!-- hybrid-search -->``
    string. Accept either so status never mis-reports a real block as missing.
    """
    return bool(BEGIN_RE.search(text)) or _CLAUDE_MD_MARKER in text

# v0.3.0: imperative routing rules that name the MCP tool explicitly. The
# weaker descriptive version shipped in v0.2.x let Claude drift toward
# Grep for exploratory questions; this version locks tool choice per
# question category and cites the MCP path by full name.
_CLAUDE_MD_SECTION = f"{_CLAUDE_MD_MARKER}\n{ROUTING_BODY}"


def _ensure_claude_md(project_path: str, *, force: bool = False) -> None:
    """Install or update the versioned hybrid-search routing block."""
    claude_md = Path(project_path) / "CLAUDE.md"
    result = apply_update(claude_md, claude_block(), force=force)
    if result.status == "fresh_install":
        print("CLAUDE.md: hybrid-search routing block added")
    elif result.status == "migrate_legacy":
        print("CLAUDE.md: migrated legacy routing block to v1")
    elif result.status == "update":
        print("CLAUDE.md: hybrid-search routing block updated")


def _remove_claude_md(project_path: str) -> bool:
    """Remove the hybrid-search section from CLAUDE.md. Returns True if removed."""
    import re as _re
    claude_md = Path(project_path) / "CLAUDE.md"
    if not claude_md.exists():
        return False
    content = claude_md.read_text(encoding="utf-8")
    legacy_pattern = _re.compile(
        r"\n*" + _re.escape(_CLAUDE_MD_MARKER) + r"\n## [^\n]+\n.*?(?=\n## |\Z)",
        flags=_re.DOTALL,
    )
    v1_pattern = _re.compile(
        r"\n*^<!-- BEGIN hybrid-search-mcp routing v\d+ -->\n.*?"
        r"^<!-- END hybrid-search-mcp routing v\d+ -->\n?",
        flags=_re.DOTALL | _re.MULTILINE,
    )
    new_content = v1_pattern.sub("", legacy_pattern.sub("", content))
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
    if getattr(args, "include_content", False):
        config = replace(
            config,
            indexing=replace(config.indexing, include_content=True),
        )
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

    # Memory Layer auto-prune — journald-style two-ceiling policy.
    _run_auto_prune(config, Path(project_path))

    # Feature genesis: index new commit messages (delta by hash — only
    # commits not yet in the store get embedded).
    _run_commit_indexing(config, registry, project_name, project_path)

    # Orphan wiki cleanup — delete pages whose source files are no longer
    # in the DB (gitignore drift or real removal).
    _run_wiki_cleanup(Path(project_path))

    # v0.4.0 — Memory integrity pass: stale qa, semantic dedup, archive TTL.
    _run_memory_integrity(config, registry, project_name, Path(project_path))

    if result.errors:
        print(f"Errors: {len(result.errors)}")
        for err in result.errors[:5]:
            print(f"  {err}")


def _run_commit_indexing(
    config: Config,
    registry: ProjectRegistry,
    project_name: str,
    project_path: str,
) -> None:
    """Index new git commit messages. Silent no-op outside a git repo."""
    try:
        from hybrid_search.index.commit_indexer import CommitIndexer

        embedder = Embedder(config.embedding, config.models_dir)
        indexer = CommitIndexer(config, registry, embedder)
        result = indexer.index_commits(project_path, project_name)
        if result.commits_indexed or result.commits_removed:
            print(
                f"Commits indexed: +{result.commits_indexed}"
                + (f", -{result.commits_removed} pruned" if result.commits_removed else "")
            )
    except Exception as exc:  # never block reindex on history indexing
        logger.debug("commit indexing skipped: %s", exc)


def _run_memory_integrity(
    config: Config,
    registry: ProjectRegistry,
    project_name: str,
    project_path: Path,
) -> None:
    """Run v0.4.0 integrity pass — stale qa, semantic dedup, archive TTL.

    Silent no-op when disabled in config or when the project isn't
    registered / the store DB is missing. Reports summary counts on
    stdout when the pass actually moves files.
    """
    from hybrid_search.memory import integrity

    cfg = getattr(config.memory, "integrity", None)
    if cfg is None or not cfg.enabled:
        return

    qa_root = integrity.project_root_qa_dir = project_path / integrity.QA_DIRNAME
    archive_root = project_path / integrity.QA_ARCHIVE_DIRNAME
    if not qa_root.is_dir() and not archive_root.is_dir():
        return

    pinfo = registry.get_by_name(project_name)
    indexed_paths: set[str] | None = None
    qa_log_chunks: list[tuple[str, str, float]] | None = None
    get_vector = None

    if pinfo is not None:
        try:
            import sqlite3
            pdir = get_project_dir(config.projects_dir, pinfo.id)
            idx = IndexPaths(pdir)
            if idx.store_db.exists():
                conn = sqlite3.connect(str(idx.store_db))
                try:
                    cur = conn.execute("SELECT relative_path FROM files")
                    indexed_paths = {row[0] for row in cur}
                    # Fetch qa_log chunks: chunk_id, absolute qa path, mtime.
                    cur = conn.execute(
                        """
                        SELECT c.id, f.relative_path
                        FROM chunks c JOIN files f ON c.file_id = f.id
                        WHERE c.node_type = 'qa_log'
                        """
                    )
                    rows = cur.fetchall()
                finally:
                    conn.close()
                qa_log_chunks = []
                for chunk_id, rel in rows:
                    abs_path = project_path / rel
                    try:
                        mtime = abs_path.stat().st_mtime
                    except OSError:
                        continue
                    qa_log_chunks.append((chunk_id, str(abs_path), mtime))

                # Vector-engine hook — only needed when we actually have
                # qa_log chunks to compare. Loading it is cheap; the HNSW
                # index is already on disk from the indexing pipeline.
                if qa_log_chunks:
                    try:
                        from hybrid_search.search.vector import VectorEngine
                        # Embedder carries the dim the indexer wrote with,
                        # so the engine loads cleanly against the existing
                        # usearch file.
                        probe = Embedder(config.embedding, config.models_dir)
                        vector_engine = VectorEngine(
                            idx.vectors_dir,
                            embedding_dim=probe.embedding_dim,
                        )
                        get_vector = vector_engine.get_vector
                    except Exception:
                        get_vector = None
        except Exception:
            pass

    report = integrity.run_integrity_pass(
        project_path,
        indexed_paths=indexed_paths,
        qa_log_chunks=qa_log_chunks,
        get_vector=get_vector,
        config=integrity.IntegrityConfig(
            enabled=cfg.enabled,
            dedup_threshold=cfg.dedup_threshold,
            archive_ttl_days=cfg.archive_ttl_days,
        ),
    )

    parts = []
    if report.stale_archived:
        parts.append(f"{len(report.stale_archived)} stale qa archived")
    if report.dedup_pairs:
        parts.append(f"{len(report.dedup_pairs)} dedup pair(s)")
    if report.archive_purged:
        parts.append(f"{len(report.archive_purged)} archive entr(ies) purged")
    if parts:
        print("Memory integrity: " + "; ".join(parts) + ".")


def _run_wiki_cleanup(project_path: Path) -> None:
    """Delete wiki pages whose source files are no longer in the DB.

    Silent no-op when the wiki dir or store DB are missing (first-run
    projects). Reports the count on success.
    """
    from hybrid_search import wiki_cleanup

    wiki_dir = project_path / ".hybrid-search" / "wiki"
    if not wiki_dir.is_dir():
        return

    indexed = wiki_cleanup.collect_indexed_paths(project_path)
    if indexed is None:
        return

    result = wiki_cleanup.cleanup_orphans(wiki_dir, indexed)
    if result.deleted:
        print(f"Wiki cleanup: removed {len(result.deleted)} orphan page(s).")
    if result.skipped_errors:
        print(f"  {len(result.skipped_errors)} page(s) failed to unlink")


def _run_auto_prune(config: Config, project_path: Path) -> None:
    """Apply ``config.memory`` retention rules to the project's qa_log dir.

    First run on a project is a dry-run unless the user has acknowledged the
    policy via ``.hybrid-search/qa/.prune-confirmed`` or the config flag
    ``memory.require_first_run_confirm = false``. This prevents accidental
    deletion of accumulated Q&A when a user installs a newer version that
    defaults on.
    """
    from hybrid_search.memory import reader

    mem = config.memory
    if not mem.auto_prune:
        return

    qa_root = reader.qa_dir(project_path)
    if not qa_root.is_dir():
        return

    confirm_marker = qa_root / ".prune-confirmed"
    is_first_run = mem.require_first_run_confirm and not confirm_marker.exists()

    result = reader.auto_prune(
        project_path,
        retention_days=mem.retention_days,
        max_files=mem.max_files,
        dry_run=is_first_run,
    )
    if not result.deleted and not result.skipped:
        # Mark as confirmed even when nothing to prune — subsequent runs can
        # act immediately without another dry-run gate.
        if is_first_run:
            try:
                confirm_marker.write_text("")
            except OSError:
                pass
        return

    if is_first_run:
        print(
            f"Memory Layer auto-prune (dry-run): would remove {len(result.deleted)} qa log(s) "
            f"older than {mem.retention_days}d or beyond the {mem.max_files}-file ceiling."
        )
        print(
            "  Run `hybrid-search qa-prune --older-than "
            f"{mem.retention_days}d --confirm-first-run` to activate auto-prune on future reindexes, "
            "or set `memory.require_first_run_confirm = false` in config.toml."
        )
        return

    print(
        f"Memory Layer auto-prune: removed {len(result.deleted)} qa log(s) "
        f"(retention: {mem.retention_days}d, max: {mem.max_files})."
    )
    if result.skipped:
        print(f"  skipped {len(result.skipped)} (unlink failed)")


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


def _memory_health(project_path: Path) -> dict[str, object]:
    """Collect P7/P8 product-health data without printing.

    The status command has historically printed checks inline. Product UX
    commands need the same facts for doctor, refresh, recall, and the static
    report, so this helper is intentionally file/DB based and cheap.
    """
    from hybrid_search.memory import cards, reader

    project_path = project_path.resolve()
    claude_count, claude_sources = _claude_memory_hook_status([
        project_path / ".claude" / "settings.local.json",
        project_path / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ])
    claude_missing = [
        event
        for event in ("PreToolUse", "SessionStart", "UserPromptSubmit", "Stop")
        if event not in _claude_memory_hook_events([
            project_path / ".claude" / "settings.local.json",
            project_path / ".claude" / "settings.json",
            Path.home() / ".claude" / "settings.json",
            Path.home() / ".claude" / "settings.local.json",
        ])
    ]

    try:
        from hybrid_search import codex_hooks

        codex = codex_hooks.codex_status(project_path)
    except Exception:
        codex = {
            "project_hooks": False,
            "user_hooks": False,
            "project_feature": False,
            "project_mcp": False,
            "user_feature": False,
            "user_mcp": False,
        }

    qa_indexes = list(reader.iter_qa_indexes(project_path))
    trigger_counts: Counter[str] = Counter(idx.trigger or "legacy" for idx in qa_indexes)
    completed_triggers = {"stop_hook", "codex_stop_hook"}
    completed_count = sum(trigger_counts[t] for t in completed_triggers)
    mcp_tool_count = trigger_counts["mcp_tool"]
    card_items = list(cards.iter_cards(project_path))
    fact_count = sum(1 for _ in cards.iter_facts(project_path))
    last_compaction = _last_mtime(cards.card_dir(project_path))
    cards_indexed = _memory_cards_indexed(project_path)
    routing_present = _CLAUDE_MD_MARKER in _safe_read_text(project_path / "CLAUDE.md")
    agents_present = "<!-- hybrid-search-mcp:codex-routing -->" in _safe_read_text(project_path / "AGENTS.md")
    try:
        config = load_config()
        excluded_summary = excluded_paths_summary(project_path, config.indexing).counts
    except Exception:
        excluded_summary = {}

    return {
        "project_path": project_path,
        "claude_count": claude_count,
        "claude_missing": claude_missing,
        "claude_sources": claude_sources,
        "codex": codex,
        "codex_ready": bool(
            (codex.get("project_hooks") and codex.get("project_feature") and codex.get("project_mcp"))
            or (codex.get("user_hooks") and codex.get("user_feature") and codex.get("user_mcp"))
        ),
        "qa_count": len(qa_indexes),
        "completed_qa_count": completed_count,
        "mcp_tool_count": mcp_tool_count,
        "trigger_counts": trigger_counts,
        "card_count": len(card_items),
        "fact_count": fact_count,
        "cards_indexed": cards_indexed,
        "last_compaction": last_compaction,
        "routing_present": routing_present,
        "agents_present": agents_present,
        "excluded_paths_summary": excluded_summary,
        "recent_cards": card_items[:8],
        "recent_qa": qa_indexes[:8],
    }


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _last_mtime(root: Path) -> datetime | None:
    if not root.is_dir():
        return None
    newest: float | None = None
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, tz=timezone.utc)


def _claude_memory_hook_events(paths: list[Path]) -> set[str]:
    import json as _json

    present: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            settings = _json.loads(path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            continue
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            continue
        for event in ("PreToolUse", "SessionStart", "UserPromptSubmit", "Stop"):
            if "hybrid_search.cli qa-hook" in _json.dumps(hooks.get(event, [])):
                present.add(event)
    return present


def _memory_cards_indexed(project_path: Path) -> bool:
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    match = _detect_project(registry, str(project_path))
    if match is None:
        return False
    pinfo = registry.get_by_name(match[0])
    if pinfo is None:
        return False
    idx = IndexPaths(get_project_dir(config.projects_dir, pinfo.id))
    if not idx.store_db.exists():
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(str(idx.store_db))
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM chunks
                WHERE node_type IN ('memory_card', 'domain_term', 'episodic_example')
                """
            ).fetchone()
            return bool(row and row[0] > 0)
        finally:
            conn.close()
    except Exception:
        return False


def _print_doctor_report(health: dict[str, object]) -> None:
    ready = (
        health["claude_count"] == 4
        and bool(health["codex_ready"])
        and int(health["card_count"]) > 0
        and (bool(health["cards_indexed"]) or int(health["card_count"]) == 0)
    )
    print("Memory is ready." if ready else "Memory is not fully active.")
    print()
    missing = health["claude_missing"]
    miss = f" (missing {', '.join(missing)})" if missing else ""
    print(f"Claude: {health['claude_count']}/4 hooks{miss}")
    codex = health["codex"]
    codex_parts = []
    if codex.get("project_hooks"):
        codex_parts.append("project hooks")
    if codex.get("project_feature"):
        codex_parts.append("project feature")
    if codex.get("project_mcp"):
        codex_parts.append("project MCP")
    if codex.get("user_hooks"):
        codex_parts.append("user hooks")
    if codex.get("user_feature"):
        codex_parts.append("user feature")
    if codex.get("user_mcp"):
        codex_parts.append("user MCP")
    print(f"Codex:  {', '.join(codex_parts) if codex_parts else 'missing'}")
    print(
        f"Corpus: qa={health['qa_count']}, cards={health['card_count']}, "
        f"facts={health['fact_count']}"
    )
    print(
        f"Recent QA: {health['mcp_tool_count']} mcp_tool logs, "
        f"{health['completed_qa_count']} completed-turn logs"
    )
    indexed = "yes" if health["cards_indexed"] else "no"
    print(f"Indexed cards: {indexed}")
    last = health["last_compaction"]
    print(f"Last compaction: {last.isoformat() if isinstance(last, datetime) else 'never'}")
    excluded = health.get("excluded_paths_summary") or {}
    if isinstance(excluded, dict):
        print("Excluded paths summary:")
        print(f"  extension: {int(excluded.get('extension', 0))}")
        print(f"  oversize_md: {int(excluded.get('oversize_md', 0))}")
        print(f"  manual: {int(excluded.get('manual', 0))}")

    fixes: list[str] = []
    if health["claude_count"] != 4 or not health["codex_ready"]:
        fixes.append("hybrid-search-mcp setup --cwd .")
        fixes.append("restart Claude/Codex")
    if int(health["qa_count"]) > 0 and int(health["card_count"]) == 0:
        fixes.append("hybrid-search-mcp memory refresh --cwd .")
    if int(health["card_count"]) > 0 and not health["cards_indexed"]:
        fixes.append("hybrid-search-mcp reindex --cwd . --force")
    if fixes:
        print()
        print("Fix:")
        for fix in dict.fromkeys(fixes):
            print(f"  {fix}")


def _claude_memory_hook_status(paths: list[Path]) -> tuple[int, list[str]]:
    """Return count/detail for Claude memory qa-hook entries across settings files."""
    import json as _json

    events = ("PreToolUse", "SessionStart", "UserPromptSubmit", "Stop")
    present: set[str] = set()
    sources: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            settings = _json.loads(path.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            continue
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            continue
        source_hit = False
        for event in events:
            entries = hooks.get(event, [])
            if "hybrid_search.cli qa-hook" in _json.dumps(entries):
                present.add(event)
                source_hit = True
        if source_hit:
            sources.append(str(path))
    return len(present), sources


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
    print(f"  {mark} Claude setup hooks: {n}/{total}  ({detail})")

    mem_n, mem_sources = _claude_memory_hook_status([
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ])
    print(
        f"  {_status_mark(mem_n == 4, warn=(0 < mem_n < 4))} "
        f"Claude memory hooks: {mem_n}/4  "
        f"({', '.join(mem_sources) if mem_sources else 'none'})"
    )

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
        has_routing = _claude_md_has_routing(claude_md.read_text(encoding="utf-8"))
        print(f"  {_status_mark(has_routing, warn=not has_routing)} CLAUDE.md routing:            "
              f"{'present' if has_routing else 'marker missing — run install-hook'}")
    else:
        print("  ⚠ CLAUDE.md not found            (run install-hook to create)")

    mem_n, mem_sources = _claude_memory_hook_status([
        project_path / ".claude" / "settings.local.json",
        project_path / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ])
    print(
        f"  {_status_mark(mem_n == 4, warn=(0 < mem_n < 4))} "
        f"Claude memory hooks:          {mem_n}/4"
        f"{' (' + ', '.join(mem_sources) + ')' if mem_sources else ' (run install-memory-hook)'}"
    )

    try:
        from hybrid_search.memory import cards

        qa_count = _count_qa_files(project_path)
        card_count = sum(1 for _ in cards.iter_card_files(project_path))
        fact_count = sum(1 for _ in cards.iter_facts(project_path))
        print(
            f"  {_status_mark(card_count > 0, warn=qa_count > 0 and card_count == 0)} "
            f"Memory corpus:               qa={qa_count}, cards={card_count}, facts={fact_count}"
        )
    except Exception:
        print("  ⚠ Memory corpus status unavailable")

    # Codex memory hooks / MCP config
    try:
        from hybrid_search import codex_hooks

        cstat = codex_hooks.codex_status(project_path)
        project_hook_ok = bool(cstat["project_hooks"])
        user_hook_ok = bool(cstat["user_hooks"])
        any_hook_ok = project_hook_ok or user_hook_ok
        detail = []
        if project_hook_ok:
            detail.append("project")
        if user_hook_ok:
            detail.append("user")
        print(
            f"  {_status_mark(any_hook_ok)} Codex hooks:                 "
            f"{', '.join(detail) if detail else 'MISSING'}"
        )
        codex_cfg_ok = (
            (cstat["project_feature"] and cstat["project_mcp"])
            or (cstat["user_feature"] and cstat["user_mcp"])
        )
        cfg_detail = []
        if cstat["project_feature"]:
            cfg_detail.append("project feature")
        if cstat["project_mcp"]:
            cfg_detail.append("project MCP")
        if cstat["user_feature"]:
            cfg_detail.append("user feature")
        if cstat["user_mcp"]:
            cfg_detail.append("user MCP")
        print(
            f"  {_status_mark(codex_cfg_ok, warn=any_hook_ok and not codex_cfg_ok)} "
            f"Codex config:                {', '.join(cfg_detail) if cfg_detail else 'MISSING'}"
        )
        if project_hook_ok:
            print("    Note: project-local Codex hooks require the project .codex layer to be trusted.")
        if cstat["agents_override"]:
            print("  ⚠ AGENTS.override.md present     (it takes precedence over AGENTS.md)")
        if cstat["agents_near_limit"]:
            print("  ⚠ AGENTS.md near 32 KiB limit    (Codex may truncate project docs)")
    except Exception:
        print("  ⚠ Codex status unavailable")


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


def cmd_drift(args: argparse.Namespace) -> None:
    """Check filesystem drift vs. index (Phase 6 L4 watchdog).

    Read-only: diffs disk against the DB's recorded files and prints an
    actionable summary. Low-frequency operation — intentionally kept as a
    CLI command rather than an MCP tool so it doesn't consume agent
    context on every session.
    """
    from hybrid_search.index.drift import detect_drift

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
        print("No index found — run `hybrid-search index` first.")
        return

    db = StoreDB(idx_paths.store_db)
    try:
        report = detect_drift(pinfo.id, Path(pinfo.path), db, config.indexing)
        print(f"[{name}] {report.summary_line()}")
        if args.verbose and report.is_drifted:
            if report.added:
                print(f"  added ({len(report.added)}):")
                for p in report.added[:20]:
                    print(f"    + {p}")
                if len(report.added) > 20:
                    print(f"    ... +{len(report.added) - 20} more")
            if report.changed:
                print(f"  changed ({len(report.changed)}):")
                for p in report.changed[:20]:
                    print(f"    ~ {p}")
                if len(report.changed) > 20:
                    print(f"    ... +{len(report.changed) - 20} more")
            if report.deleted:
                print(f"  deleted ({len(report.deleted)}):")
                for p in report.deleted[:20]:
                    print(f"    - {p}")
                if len(report.deleted) > 20:
                    print(f"    ... +{len(report.deleted) - 20} more")
        if report.is_drifted:
            print("Run `hybrid-search reindex` to bring the index in sync.")
    finally:
        db.close()


def cmd_viewer(args: argparse.Namespace) -> None:
    """Render the local memory viewer (.hybrid-search/viewer.html)."""
    import sqlite3

    from hybrid_search.viewer import write_viewer

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    name = match[0] if match else Path(cwd).name
    project_root = Path(match[1]) if match else Path(cwd)

    stats: dict = {}
    pinfo = registry.get_by_name(name) if match else None
    if pinfo:
        store = IndexPaths(get_project_dir(config.projects_dir, pinfo.id)).store_db
        if store.exists():
            conn = sqlite3.connect(str(store))
            try:
                rows = conn.execute(
                    "SELECT node_type, COUNT(*) FROM chunks WHERE project_id = ? "
                    "GROUP BY node_type",
                    (pinfo.id,),
                ).fetchall()
            finally:
                conn.close()
            by_type = dict(rows)
            memory_types = {"qa_log", "memory_card", "domain_term", "episodic_example"}
            stats = {
                "코드 청크": sum(
                    v for k, v in by_type.items()
                    if k not in memory_types and k not in ("conv_turn", "commit")
                ),
                "대화 턴": by_type.get("conv_turn", 0),
                "커밋": by_type.get("commit", 0),
                "Q&A": by_type.get("qa_log", 0),
                "메모리 카드": by_type.get("memory_card", 0),
            }

    out = write_viewer(project_root, name, stats)
    print(f"Memory viewer written: {out}")
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(out.as_uri())


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


_PRESERVED_WIKI_NAMES = frozenset({"STALE.md"})


def _cleanup_orphan_wiki_pages(wiki_dir: Path, expected_filenames: set[str]) -> int:
    """Delete top-level .md files in wiki_dir that aren't in expected_filenames.

    Called after full regeneration to purge pages from previous runs whose
    module names have changed (e.g. ``test_wiki-1.md`` after the fragmentation
    fix). Preserves ``STALE.md`` and any files inside sub-directories (synthesis
    staging dirs).
    """
    if not wiki_dir.exists():
        return 0
    removed = 0
    for path in wiki_dir.iterdir():
        if not path.is_file() or path.suffix != ".md":
            continue
        if path.name in _PRESERVED_WIKI_NAMES:
            continue
        if path.name in expected_filenames:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


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

        expected_filenames = {page.filename for page in pages}
        removed = _cleanup_orphan_wiki_pages(wiki_dir, expected_filenames)

        print(f"  Wrote {written} pages to {wiki_dir}")
        if removed:
            print(f"  Removed {removed} orphan page(s)")

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


def cmd_recalibrate(args: argparse.Namespace) -> None:
    """Derive router confidence thresholds from a gold set and persist them."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)

    from hybrid_search.search.orchestrator import SearchOrchestrator

    cwd = str(Path(args.cwd).resolve())
    orchestrator = SearchOrchestrator(config, registry, embedder)
    thresholds = _calibrate_router_confidence(
        orchestrator,
        _load_gold_queries(Path(args.gold)),
        project=args.project,
        cwd=cwd,
        limit=args.limit,
    )
    # Write where load_config actually reads (the data dir, normally
    # ~/.hybrid-search). The old target (cwd/config.toml) was never loaded
    # back, so calibration silently had no effect.
    from hybrid_search.config import DEFAULT_DATA_DIR

    config_path = Path(getattr(config, "data_dir", DEFAULT_DATA_DIR)) / "config.toml"
    changed = _write_router_confidence_config(config_path, thresholds)
    status = "updated" if changed else "unchanged"
    print(
        "router.confidence "
        f"{status} ({config_path}): strong_score={thresholds['strong_score']:.6f}, "
        f"strong_gap={thresholds['strong_gap']:.6f}, "
        f"weak_score={thresholds['weak_score']:.6f}, "
        f"cosine_anchor={thresholds.get('cosine_anchor', 0.0):.6f}"
    )


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

    # Drop the reindex dry-run gate: the user has explicitly pruned once and
    # understands the policy, so subsequent auto-prunes can act directly.
    if not args.dry_run and getattr(args, "confirm_first_run", False):
        marker = reader.qa_dir(root) / ".prune-confirmed"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("")
            print("  auto-prune activated for future reindexes.")
        except OSError as exc:
            print(f"  warning: could not write confirm marker ({exc})", file=sys.stderr)


def cmd_qa_hook(args: argparse.Namespace) -> None:
    """Hook entry — read JSON from stdin, emit context JSON on stdout, exit 0."""
    from hybrid_search import hooks
    rc = hooks.cli_main()
    sys.exit(rc)


def cmd_codex_hook(args: argparse.Namespace) -> None:
    """Codex hook entry — read JSON from stdin, always emit JSON on stdout."""
    from hybrid_search import codex_hooks

    rc = codex_hooks.cli_main()
    sys.exit(rc)


def cmd_wiki_cleanup(args: argparse.Namespace) -> None:
    """Delete orphan wiki pages (DB-based detection)."""
    from hybrid_search import wiki_cleanup

    project_path = Path(args.cwd).resolve()
    wiki_dir = project_path / ".hybrid-search" / "wiki"
    if not wiki_dir.is_dir():
        print(f"No wiki directory at {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    indexed = wiki_cleanup.collect_indexed_paths(project_path)
    if indexed is None:
        print(
            "Store DB unreachable — is this project registered? "
            "Run `hybrid-search-mcp index .` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    result = wiki_cleanup.cleanup_orphans(
        wiki_dir,
        indexed,
        dry_run=args.dry_run,
    )

    tag = "[dry-run]" if args.dry_run else ""
    print(f"{tag} scanned {result.scanned} page(s)")
    print(f"{tag} orphans identified: {len(result.orphans)}")
    if args.verbose:
        for p in result.orphans[:50]:
            print(f"  orphan: {p.name}")
        if len(result.orphans) > 50:
            print(f"  ... and {len(result.orphans) - 50} more")
    if args.dry_run:
        print("[dry-run] nothing removed")
        return
    print(f"removed {len(result.deleted)} page(s)")
    if result.skipped_errors:
        print(f"  failed to unlink {len(result.skipped_errors)} page(s)")


def cmd_install_memory_hook(args: argparse.Namespace) -> None:
    """Merge memory hook config into .claude/settings(.local).json atomically."""
    from hybrid_search import hooks

    if getattr(args, "global_scope", False):
        settings_path = Path.home() / ".claude" / "settings.json"
    else:
        base = Path(args.cwd).resolve() / ".claude"
        fname = "settings.local.json" if args.settings == "local" else "settings.json"
        settings_path = base / fname

    result = hooks.install_memory_hook(settings_path, dry_run=args.dry_run)
    status = result["status"]
    added = result.get("added", 0)
    updated = result.get("updated", 0)
    if status == "exists":
        print(f"Memory hook already present in {settings_path} — nothing to do.")
        return
    if status == "dry-run":
        parts = []
        if added:
            parts.append(f"add {added} hook block(s)")
        if updated:
            parts.append(f"refresh {updated} stale Python path(s)")
        print(f"Would {', '.join(parts)} in {settings_path}.")
        return
    msg = []
    if added:
        msg.append(f"installed {added} hook block(s)")
    if updated:
        msg.append(f"refreshed {updated} stale Python path(s)")
    print(f"Memory hook: {'; '.join(msg)} → {settings_path}")
    print("  Restart any running Claude Code sessions to pick up the change.")


def cmd_install_codex_hook(args: argparse.Namespace) -> None:
    """Install Codex lifecycle hooks and MCP config."""
    from hybrid_search import codex_hooks

    project_root = Path(args.cwd).resolve()
    result = codex_hooks.install_codex_hook(
        project_root,
        user=bool(getattr(args, "user", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    scope = "user" if result.get("user") else "project"
    if result["status"] == "dry-run":
        print(
            f"Would install Codex memory hook ({scope}) → "
            f"{result['hooks_path']} and {result['config_path']}"
        )
        return
    if result["status"] == "exists":
        print(f"Codex memory hook already present ({scope}) — nothing to do.")
    else:
        parts = []
        if result.get("added"):
            parts.append(f"installed {result['added']} hook block(s)")
        if result.get("updated"):
            parts.append(f"refreshed {result['updated']} stale Python path(s)")
        if result.get("feature_changed"):
            parts.append("enabled hooks")
        if result.get("mcp_changed"):
            parts.append("registered MCP server")
        if result.get("gitignore_changed"):
            parts.append("updated .gitignore")
        if result.get("agents_changed"):
            parts.append("updated AGENTS.md")
        print(f"Codex memory hook: {'; '.join(parts) or 'updated'}")
    print(f"  hooks: {result['hooks_path']}")
    print(f"  config: {result['config_path']}")
    if not result.get("user"):
        print("  Start Codex in this trusted project and run status/smoke checks before relying on it.")


def cmd_qa_stats(args: argparse.Namespace) -> None:
    """Summary stats over a project's qa logs.

    v0.4.0 surfaces active / archived / recent-churn counts alongside the
    by-type / by-month breakdown so users can see the integrity pass
    actually doing something (archive tier keeps the deletes visible
    until TTL expires).
    """
    from collections import Counter
    from hybrid_search.memory import integrity, reader

    root = _resolve_qa_root(args)
    if root is None:
        sys.exit(1)

    indexes = list(reader.iter_qa_indexes(root))
    total = len(indexes)

    active = integrity.count_active(root)
    archived = integrity.count_archived(root)
    recent_archive = integrity.count_recent_archive_additions(root, window_days=7)

    print(f"qa logs under {root / reader.QA_DIRNAME}")
    print(f"  active:          {active}")
    print(f"  archived:        {archived}")
    print(f"  recent archive   {recent_archive}  (last 7d)")
    print(f"  total ever:      {active + archived}")

    if total == 0:
        return

    by_month: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    by_trigger: Counter[str] = Counter()
    for idx in indexes:
        by_type[idx.query_type] += 1
        if idx.timestamp is not None:
            by_month[idx.timestamp.strftime("%Y-%m")] += 1
        if idx.trigger:
            by_trigger[idx.trigger] += 1

    print("  by query_type:")
    for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<16} {v}")
    print("  by month:")
    for k, v in sorted(by_month.items()):
        print(f"    {k}            {v}")
    if by_trigger:
        print("  by trigger:")
        for k, v in sorted(by_trigger.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<16} {v}")


def cmd_integrity(args: argparse.Namespace) -> None:
    """Run the Memory integrity pass on-demand (same logic as reindex tail)."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    project_path = Path(args.cwd).resolve()

    match = _detect_project(registry, str(project_path))
    if match is None:
        print(
            f"Project at {project_path} isn't registered. "
            "Run `hybrid-search-mcp index .` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    project_name = match[0]

    # Override dedup threshold if user asked.
    if args.dedup_threshold is not None:
        from hybrid_search.config import MemoryConfig, MemoryIntegrityConfig
        # Build a one-shot config override — we don't mutate the frozen
        # dataclass; we swap in a new MemoryConfig that shadows the original.
        cfg_override = MemoryConfig(
            auto_prune=config.memory.auto_prune,
            retention_days=config.memory.retention_days,
            max_files=config.memory.max_files,
            require_first_run_confirm=config.memory.require_first_run_confirm,
            integrity=MemoryIntegrityConfig(
                enabled=True,
                dedup_threshold=args.dedup_threshold,
                archive_ttl_days=config.memory.integrity.archive_ttl_days,
            ),
        )
        config = type(config)(**{**config.__dict__, "memory": cfg_override})

    active_before = _count_qa_files(project_path)
    _run_memory_integrity(config, registry, project_name, project_path)
    active_after = _count_qa_files(project_path)

    print(f"qa active: {active_before} → {active_after}  (Δ {active_after - active_before})")


def _count_qa_files(project_path: Path) -> int:
    """Count the .md files currently in ``.hybrid-search/qa/``."""
    root = project_path / ".hybrid-search" / "qa"
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.md"))


def cmd_qa_restore(args: argparse.Namespace) -> None:
    """Move an archived qa back into the active qa/ tree."""
    from hybrid_search.memory import integrity

    root = _resolve_qa_root(args)
    if root is None:
        sys.exit(1)
    restored = integrity.restore_archived(root, args.id)
    if restored is None:
        print(f"No archived qa found for '{args.id}'", file=sys.stderr)
        sys.exit(1)
    print(f"Restored: {restored}")


def _resolve_memory_root(args: argparse.Namespace) -> Path | None:
    return _resolve_qa_root(args)


def cmd_memory_card_create(args: argparse.Namespace) -> None:
    """Create a compact memory card from an existing qa log."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    path = cards.create_card_from_qa(root, args.from_qa, card_type=args.type)
    if path is None:
        print(f"qa log not found or unreadable: {args.from_qa}", file=sys.stderr)
        sys.exit(1)
    print(f"Memory card created: {path}")


def cmd_memory_card_list(args: argparse.Namespace) -> None:
    """List memory cards for a project."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    items = list(cards.iter_cards(root))[: args.limit]
    if args.json:
        import json as _json

        print(_json.dumps([
            {
                "id": c.id,
                "type": c.type,
                "summary": c.summary,
                "query": c.query,
                "topics": list(c.topics),
                "files": list(c.files),
                "source_ids": list(c.source_ids),
                "path": str(c.path),
            }
            for c in items
        ], ensure_ascii=False, indent=2))
        return
    if not items:
        print(f"No memory cards under {root / cards.CARD_DIRNAME}")
        return
    for card in items:
        ts = card.timestamp.strftime("%Y-%m-%d %H:%M") if card.timestamp else "?"
        print(f"{card.id}  [{card.type}/{card.confidence}/{card.status}]  {ts}  {_truncate_preview(card.summary)}")


def cmd_memory_card_show(args: argparse.Namespace) -> None:
    """Print a single memory card by id/stem/hash suffix."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    path = cards.find_card_by_id(root, args.id)
    if path is None:
        print(f"memory card not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(path.read_text(encoding="utf-8"))


def cmd_memory_card_grep(args: argparse.Namespace) -> None:
    """Grep memory cards. Newest files first."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    needle = args.term if args.case_sensitive else args.term.lower()
    found = False
    for card_path in cards.iter_card_files(root):
        text = card_path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            hay = line if args.case_sensitive else line.lower()
            if needle in hay:
                card = cards.parse_card(card_path)
                cid = card.id if card else card_path.stem
                print(f"{cid}:{lineno}:{line}")
                found = True
    if not found:
        sys.exit(1)


def cmd_memory_compact(args: argparse.Namespace) -> None:
    """Promote qa logs into compact memory cards."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    result = cards.compact_qa_to_cards(
        root,
        since=args.since,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}memory compact: {result['created']} created / {result['candidates']} candidate(s)")
    if args.verbose:
        for path in result.get("paths", []):
            print(f"  {path}")


def cmd_memory_procedural_review(args: argparse.Namespace) -> None:
    """Generate procedural memory candidates for human review."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    path = cards.write_procedural_candidates(root)
    if path is None:
        print("No procedural candidates found.")
        return
    print(f"Procedural candidates written: {path}")


def cmd_memory_facts_export(args: argparse.Namespace) -> None:
    """Export graph-lite temporal facts from memory cards."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    path = cards.export_facts(root)
    if path is None:
        print("No facts exported.")
        return
    print(f"Facts exported: {path}")


def cmd_memory_facts_list(args: argparse.Namespace) -> None:
    """List exported graph-lite facts."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    needle = (args.query or "").lower()
    count = 0
    for fact in cards.iter_facts(root):
        text = " ".join(str(v) for v in fact.values()).lower()
        if needle and needle not in text:
            continue
        print(f"{fact.get('subject')} — {fact.get('predicate')} — {fact.get('object')}")
        print(f"  source: {fact.get('source')}")
        count += 1
        if count >= args.limit:
            break
    if count == 0:
        sys.exit(1)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Diagnose whether project memory is truly operational."""
    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    _print_doctor_report(_memory_health(root))


def cmd_memory_refresh(args: argparse.Namespace) -> None:
    """Run the deterministic memory consolidation loop for one project."""
    from hybrid_search.memory import cards

    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)

    before = _memory_health(root)
    print("Hybrid Memory Refresh")
    print()
    print(f"Project: {root.name}")
    print()
    print("Hooks")
    print(f"  Claude: {before['claude_count']}/4 {'ready' if before['claude_count'] == 4 else 'incomplete'}")
    print(f"  Codex:  {'ready' if before['codex_ready'] else 'incomplete'}")
    if not args.allow_incomplete_hooks and (before["claude_count"] != 4 or not before["codex_ready"]):
        print()
        print("Memory refresh stopped because hooks are incomplete.")
        print("Run `hybrid-search-mcp setup --cwd .`, restart Claude/Codex, then retry.")
        sys.exit(1)

    compact = cards.compact_qa_to_cards(root, since=args.since, limit=args.limit)
    proc_path = cards.write_procedural_candidates(root)
    facts_path = cards.export_facts(root)

    reindexed = False
    # Reindex only when searchable memory files changed. Procedural/facts
    # exports are product surfaces, but cards are the retrieval unit.
    if compact["created"] or args.force_reindex:
        re_args = argparse.Namespace(
            cwd=str(root),
            force=args.force_reindex,
            git_delta=False,
            wiki=False,
            wiki_scope="full",
            synthesize=False,
        )
        cmd_reindex(re_args)
        reindexed = True

    after = _memory_health(root)
    print()
    print("Memory")
    print(f"  QA logs:       {after['qa_count']}")
    print(f"  New cards:     {compact['created']}")
    print(f"  Total cards:   {after['card_count']}")
    print(f"  Facts:         {after['fact_count']}")
    print()
    print("Index")
    print(f"  Reindexed:     {'yes' if reindexed else 'skipped'}")
    print(f"  Cards indexed: {'yes' if after['cards_indexed'] else 'no'}")
    suggestions = _suggest_recall_prompts(after)
    if suggestions:
        print()
        print("Try")
        print(f'  "{suggestions[0]}"')


def _suggest_recall_prompts(health: dict[str, object], *, limit: int = 4) -> list[str]:
    prompts: list[str] = []
    for card in health.get("recent_cards", []):
        query = getattr(card, "query", "") or getattr(card, "summary", "")
        query = _truncate_preview(query, 54)
        if query:
            prompts.append(f"{query}에 대해 지난번에 뭐라고 했지?")
        if len(prompts) >= limit:
            break
    if prompts:
        return prompts
    for idx in health.get("recent_qa", []):
        query = _truncate_preview(getattr(idx, "query", ""), 54)
        if query:
            prompts.append(f"{query}에 대해 어떤 대화를 나눴지?")
        if len(prompts) >= limit:
            break
    return prompts


def cmd_memory_recall(args: argparse.Namespace) -> None:
    """Memory-first search for humans: cards, then completed turns, then raw logs."""
    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)

    try:
        from hybrid_search.config import load_config
        from hybrid_search.index.embedder import Embedder
        from hybrid_search.project import ProjectRegistry
        from hybrid_search.search.orchestrator import SearchOrchestrator
    except Exception as exc:
        print(f"Search unavailable: {exc}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    registry = ProjectRegistry(cfg.global_dir)
    embedder = Embedder(cfg.embedding, cfg.models_dir)
    orch = SearchOrchestrator(config=cfg, registry=registry, embedder=embedder)
    response = orch.hybrid_search(
        query=args.query,
        cwd=str(root),
        limit=args.limit,
    )
    results = list(getattr(response, "results", []) or [])
    memory = [
        r for r in results
        if getattr(r, "node_type", None) in {"domain_term", "memory_card", "episodic_example", "qa_log"}
    ]
    memory.sort(key=lambda r: {
        "domain_term": 0,
        "memory_card": 1,
        "episodic_example": 2,
        "qa_log": 3,
    }.get(getattr(r, "node_type", ""), 9))

    if not memory:
        print("No memory results found.")
        health = _memory_health(root)
        if int(health["qa_count"]) == 0:
            print("Completed-turn memory is empty. Run setup, restart the client, then ask again.")
        elif int(health["card_count"]) == 0:
            print("QA logs exist but no cards are available. Run `hybrid-search-mcp memory refresh --cwd .`.")
        sys.exit(1)

    has_card = any(getattr(r, "node_type", None) == "memory_card" for r in memory)
    for idx, r in enumerate(memory[: args.limit], start=1):
        nt = getattr(r, "node_type", "?")
        path = getattr(r, "file_path", "?")
        name = getattr(r, "name", None) or getattr(r, "qualified_name", None) or ""
        print(f"{idx}. [{nt}] {path}{' — ' + name if name else ''}")
        snippet = (getattr(r, "snippet", None) or getattr(r, "content", "") or "").strip()
        if snippet:
            print(f"   {_truncate_preview(snippet, 160)}")
    if not has_card:
        print()
        print("Only raw/tool-search memory surfaced. Run `hybrid-search-mcp memory refresh --cwd .` to promote useful turns into cards.")


def cmd_memory_open(args: argparse.Namespace) -> None:
    """Generate the static visual Memory report."""
    root = _resolve_memory_root(args)
    if root is None:
        sys.exit(1)
    path = _write_memory_report(root)
    print(f"Memory report: {path}")
    if not args.no_open:
        try:
            import webbrowser

            webbrowser.open(path.resolve().as_uri())
        except Exception:
            pass


def _write_memory_report(project_root: Path) -> Path:
    health = _memory_health(project_root)
    warnings: list[str] = []
    if health["claude_count"] != 4:
        warnings.append("Claude Stop/UserPromptSubmit memory hooks are incomplete.")
    if not health["codex_ready"]:
        warnings.append("Codex memory hooks or project MCP config are incomplete.")
    if int(health["card_count"]) == 0:
        warnings.append("No memory cards exist yet.")
    if int(health["mcp_tool_count"]) > int(health["completed_qa_count"]):
        warnings.append("Recent memory is mostly tool-search logs, not completed conversation turns.")

    suggestions = _suggest_recall_prompts(health)
    rows = []
    for card in health.get("recent_cards", []):
        rows.append(
            "<tr>"
            f"<td>{html.escape(getattr(card, 'id', ''))}</td>"
            f"<td>{html.escape(getattr(card, 'summary', ''))}</td>"
            f"<td>{html.escape(', '.join(getattr(card, 'topics', ()) or ()))}</td>"
            f"<td>{html.escape(', '.join(getattr(card, 'source_ids', ()) or ()))}</td>"
            "</tr>"
        )
    warn_html = "".join(f"<li>{html.escape(w)}</li>" for w in warnings) or "<li>No blocking warnings.</li>"
    sug_html = "".join(f"<li>{html.escape(s)}</li>" for s in suggestions) or "<li>No suggestions yet.</li>"
    card_rows = "\n".join(rows) or '<tr><td colspan="4">No memory cards yet.</td></tr>'
    completed = int(health["completed_qa_count"])
    qa_count = int(health["qa_count"])
    promoted = int(health["card_count"])
    unpromoted = max(0, qa_count - promoted)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Hybrid Search Memory Report</title>
  <style>
    body {{ margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #18202a; background: #f6f7f9; }}
    header {{ padding: 28px 36px; background: #18202a; color: white; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 28px; }}
    section {{ margin: 0 0 24px; padding: 20px; background: white; border: 1px solid #d9dee7; border-radius: 8px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
    .metric {{ padding: 12px; border: 1px solid #e1e5ec; border-radius: 6px; }}
    .metric b {{ display: block; font-size: 24px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #e5e9f0; text-align: left; vertical-align: top; }}
    code {{ background: #eef1f5; padding: 1px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
  <header>
    <h1>Hybrid Search Memory Report</h1>
    <div>{html.escape(str(project_root))}</div>
  </header>
  <main>
    <section>
      <h2>Readiness</h2>
      <div class="grid">
        <div class="metric"><span>Claude hooks</span><b>{health['claude_count']}/4</b></div>
        <div class="metric"><span>Codex</span><b>{'ready' if health['codex_ready'] else 'missing'}</b></div>
        <div class="metric"><span>Cards indexed</span><b>{'yes' if health['cards_indexed'] else 'no'}</b></div>
        <div class="metric"><span>Last compaction</span><b>{html.escape(health['last_compaction'].date().isoformat() if isinstance(health['last_compaction'], datetime) else 'never')}</b></div>
      </div>
    </section>
    <section>
      <h2>Corpus</h2>
      <div class="grid">
        <div class="metric"><span>QA logs</span><b>{qa_count}</b></div>
        <div class="metric"><span>Completed turns</span><b>{completed}</b></div>
        <div class="metric"><span>mcp_tool logs</span><b>{health['mcp_tool_count']}</b></div>
        <div class="metric"><span>Cards</span><b>{promoted}</b></div>
        <div class="metric"><span>Facts</span><b>{health['fact_count']}</b></div>
        <div class="metric"><span>Unpromoted QA</span><b>{unpromoted}</b></div>
      </div>
    </section>
    <section>
      <h2>Recent Memory Cards</h2>
      <table>
        <thead><tr><th>ID</th><th>Summary</th><th>Topics</th><th>Source QA</th></tr></thead>
        <tbody>{card_rows}</tbody>
      </table>
    </section>
    <section>
      <h2>Suggested Recall Prompts</h2>
      <ul>{sug_html}</ul>
    </section>
    <section>
      <h2>Warnings</h2>
      <ul>{warn_html}</ul>
      <p>Fix path: <code>hybrid-search-mcp setup --cwd .</code>, restart Claude/Codex, then <code>hybrid-search-mcp memory refresh --cwd .</code>.</p>
    </section>
  </main>
</body>
</html>
"""
    path = project_root / ".hybrid-search" / "memory" / "report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_text, encoding="utf-8")
    return path


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_conv_lock(lock_path: Path) -> bool:
    """Best-effort PID lock so concurrent conv-index runs don't clash on the
    Tantivy/USearch writers. Returns False when another live run holds it."""
    try:
        if lock_path.exists():
            try:
                holder = int(lock_path.read_text().strip())
            except (ValueError, OSError):
                holder = None
            if holder and holder != os.getpid() and _pid_alive(holder):
                return False
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()))
        return True
    except OSError:
        return False


def cmd_index_conversations(args: argparse.Namespace) -> None:
    """Index Claude Code + Codex transcripts for the project at cwd.

    Without ``--transcript`` it scans all of the project's transcripts
    (``~/.claude/projects/<slug>/*.jsonl`` + Codex sessions whose
    ``session_meta.cwd`` matches). With ``--transcript`` it indexes a single
    session file — the cheap per-turn path the Stop hooks call. Each turn
    becomes a searchable ``conv_turn`` chunk: cross-tool recall where a
    question answered in one agent becomes context the other can find.
    """
    from hybrid_search.project import project_hash
    from hybrid_search.storage.indexes import get_project_dir

    config = load_config()
    registry = ProjectRegistry(config.global_dir)

    cwd = str(Path(args.cwd).resolve())
    match = _detect_project(registry, cwd)
    if match:
        project_name, project_path = match
    else:
        project_path, project_name = cwd, Path(cwd).name

    project_dir = get_project_dir(config.projects_dir, project_hash(str(Path(project_path).resolve())))
    lock_path = project_dir / ".conv-index.lock"
    if not _acquire_conv_lock(lock_path):
        print("Conversation indexing already running, skipping.")
        return

    try:
        embedder = Embedder(config.embedding, config.models_dir)
        indexer = ConversationIndexer(config, registry, embedder)
        raw_source = getattr(args, "source", "auto")
        source = None if raw_source in (None, "auto") else raw_source
        transcript = getattr(args, "transcript", None)
        if transcript:
            result = indexer.index_transcript(transcript, project_path, source=source)
        else:
            result = indexer.index_conversations(project_path, project_name=project_name)
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass

    print(
        f"Conversation indexing for {result.project_name}: "
        f"{result.sessions_indexed} sessions indexed, "
        f"{result.sessions_skipped} unchanged, {result.chunks_total} chunks."
    )


def cmd_maintain(args: argparse.Namespace) -> None:
    """Codex-friendly maintenance wrapper for index + wiki synthesis lifecycle.

    Claude Code has a `/maintain` skill that can spawn synthesis agents. Codex
    does not have that skill surface, so this CLI command performs the
    deterministic parts and gives an explicit handoff when LLM-authored
    synthesis files are still needed.
    """
    project_root = Path(args.cwd).resolve()
    wiki_dir = project_root / ".hybrid-search" / "wiki"
    input_dir = wiki_dir / "_synthesis_input"
    output_dir = wiki_dir / "_synthesis_output"

    print("Hybrid Search Maintain")
    print()
    print(f"Project: {project_root}")
    print()
    print("Step 1: reindex + synthesis prepare")
    re_args = argparse.Namespace(
        cwd=str(project_root),
        force=args.force_reindex,
        git_delta=False,
        wiki=False,
        wiki_scope="full",
        synthesize=True,
    )
    cmd_reindex(re_args)

    output_files = _md_files(output_dir)
    if output_files:
        print()
        print(f"Step 2: finalize {len(output_files)} synthesis output file(s)")
        fin_args = argparse.Namespace(
            cwd=str(project_root),
            module=None,
            dry_run=False,
            finalize=True,
        )
        cmd_synthesize_wiki(fin_args)
    else:
        input_files = _md_files(input_dir)
        if input_files:
            print()
            print("Step 2: synthesis input is ready")
            print(f"  Input dir:  {input_dir}")
            print(f"  Output dir: {output_dir}")
            print(f"  Pending:    {len(input_files)} module(s)")
            for path in input_files[:10]:
                print(f"    - {path.name}")
            if len(input_files) > 10:
                print(f"    ... and {len(input_files) - 10} more")
            print()
            print("Next:")
            print("  Write one output markdown file per input file, then run:")
            print(f"  hybrid-search-mcp maintain --cwd {project_root}")
            if not args.keep_going:
                return
        else:
            print()
            print("Step 2: no synthesis input/output pending")

    print()
    print("Step 3: verify synthesis")
    verify_args = argparse.Namespace(cwd=str(project_root), json=False, fix=True)
    cmd_verify_synthesis(verify_args)

    if not args.no_status:
        print()
        print("Step 4: status")
        status_args = argparse.Namespace(cwd=str(project_root))
        cmd_status(status_args)


def _md_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(p for p in path.glob("*.md") if p.is_file())


def cmd_serve(_args: argparse.Namespace) -> None:
    """Start MCP server over stdio (for Claude Code / MCP clients)."""
    import asyncio
    from hybrid_search.server import _run_server

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    asyncio.run(_run_server(config))


def cmd_setup(args: argparse.Namespace) -> None:
    """One-command setup: global Claude surface plus project memory hooks."""
    import json as _json

    project_path = Path(getattr(args, "cwd", ".")).resolve()
    dry_run = bool(getattr(args, "dry_run", False))
    force = bool(getattr(args, "force", False))

    if dry_run:
        try:
            for path, block in (
                (project_path / "CLAUDE.md", claude_block()),
                (project_path / "AGENTS.md", agents_block()),
            ):
                result = apply_update(path, block, dry_run=True, force=force)
                if result.diff:
                    print(result.diff, end="" if result.diff.endswith("\n") else "\n")
                else:
                    print(f"{path.name}: no change")
        except (RuntimeError, NotImplementedError) as exc:
            print(f"ERROR: {exc}")
            raise SystemExit(1) from exc
        print("setup --dry-run: no files written; hook/config installation skipped.")
        return

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

    # Check if our hooks already exist. The `"|| true"` requirement doubles
    # as a migration marker: hooks written before it exit non-zero whenever
    # their gate condition is unmet (non-git folder, no STALE.md, …) and
    # Claude Code surfaces that as a "hook error" on every Read/Edit — so
    # legacy entries must be detected as missing and rewritten.
    def _is_current(h: dict, needle: str) -> bool:
        cmd = str(h.get("hooks", [{}])[0].get("command", ""))
        return needle in cmd and "|| true" in cmd

    pre_hooks = hooks.get("PreToolUse", [])
    has_auto_index = any(
        _is_current(h, "hybrid-search/wiki")
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Read"
    )
    has_stale_check = any(
        _is_current(h, "STALE.md")
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Edit|Write"
    )
    has_gaps_check = any(
        _is_current(h, "wiki-gaps")
        for h in pre_hooks
        if isinstance(h, dict) and h.get("matcher") == "Read|Edit|Write"
    )
    has_route_hook = any(
        _is_current(h, "wiki/index.md")
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
                    f'echo "hybrid-search: first-time indexing started in background for $ROOT" '
                    f'|| true'
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
                    'cat "$ROOT/.hybrid-search/wiki/STALE.md" || true'
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
                    'touch "$ROOT/.hybrid-search/.gaps-shown" || true'
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
                    '다른 프로젝트 참조 시 project 파라미터 지원."}}\' || true'
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
    # Repo checkout first (editable/dev installs), then the copy packaged
    # inside the wheel — a plain `pip install` has no skills/ directory.
    skills_src = Path(__file__).resolve().parents[2] / "skills"
    if not skills_src.is_dir():
        skills_src = Path(__file__).resolve().parent / "_skills"
    skills_dst = Path.home() / ".claude" / "skills"

    if skills_src.is_dir():
        installed = 0
        manifest = _load_skill_manifest(skills_dst)
        for skill_file in sorted(skills_src.glob("*.md")):
            skill_name = skill_file.stem
            dst_dir = skills_dst / skill_name
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst_file = dst_dir / "skill.md"
            src_content = skill_file.read_text(encoding="utf-8")
            src_sha = _sha256_text(src_content)

            if dst_file.exists():
                existing = dst_file.read_text(encoding="utf-8")
                if existing == src_content:
                    manifest[skill_name] = src_sha
                    continue  # identical, skip
                # A skill.md we did NOT write (no manifest match) is the
                # user's own — back it up so teardown can restore it. These
                # are generic names ("search", "maintain"); clobbering a
                # user's skill silently is not acceptable.
                if _sha256_text(existing) != manifest.get(skill_name):
                    backup = dst_dir / "skill.md.pre-memory-layer"
                    if not backup.exists():
                        backup.write_text(existing, encoding="utf-8")
                        print(f"Skill '{skill_name}': existing user skill backed up → {backup.name}")

            dst_file.write_text(src_content, encoding="utf-8")
            manifest[skill_name] = src_sha
            installed += 1

        _save_skill_manifest(skills_dst, manifest)
        if installed > 0:
            print(f"Skills installed: {installed} skill(s) → {skills_dst}")
        else:
            print(f"Skills already up-to-date: {skills_dst}")
    else:
        print(f"Skills source not found: {skills_src} (skipped)")

    # --- Step 4: project-local memory product surface ---
    if getattr(args, "global_only", False):
        # Plugin-bootstrap path: the global surface above is everything the
        # session needs; per-project onboarding happens via the auto_index
        # PreToolUse hook the first time a project file is Read.
        print()
        print("Setup complete (global only). Restart Claude/Codex to apply changes.")
        return

    try:
        from hybrid_search import codex_hooks, hooks as memory_hooks

        project_settings = project_path / ".claude" / "settings.local.json"
        mem_result = memory_hooks.install_memory_hook(project_settings)
        if mem_result["status"] == "exists":
            print(f"Claude memory hooks already present: {project_settings}")
        else:
            print(f"Claude memory hooks installed: {project_settings}")

        codex_result = codex_hooks.install_codex_hook(project_path, force=force)
        if codex_result["status"] == "exists":
            print(f"Codex memory hooks already present: {codex_result['hooks_path']}")
        else:
            print(f"Codex memory hooks installed: {codex_result['hooks_path']}")

        _ensure_claude_md(str(project_path), force=force)
        _ensure_gitignore_entries(project_path)
    except (RuntimeError, NotImplementedError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"Project memory setup skipped: {exc}")

    print()
    health = _memory_health(project_path)
    print(
        f"Claude memory hooks: {health['claude_count']}/4; "
        f"Codex hooks: {'ready' if health['codex_ready'] else 'incomplete'}"
    )
    print("Setup complete. Restart Claude/Codex to apply changes.")


_SKILL_MANIFEST_NAME = ".memory-layer-manifest.json"


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_skill_manifest(skills_dst: Path) -> dict:
    import json as _json

    path = skills_dst / _SKILL_MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_skill_manifest(skills_dst: Path, manifest: dict) -> None:
    import json as _json

    skills_dst.mkdir(parents=True, exist_ok=True)
    (skills_dst / _SKILL_MANIFEST_NAME).write_text(
        _json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def cmd_teardown(args: argparse.Namespace) -> None:
    """Remove the global surface ``setup`` installed.

    Plugin uninstall has no cleanup lifecycle hook, so the thin-installer
    design leaves the MCP registration, global hooks, and skills behind —
    this command is the documented removal path. Project-local files
    (CLAUDE.md blocks, .claude/settings.local.json, .hybrid-search/) are
    left alone: they belong to each project and are removed per-project
    with ``setup``'s project tooling.
    """
    import json as _json

    removed: list[str] = []

    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = _json.loads(claude_json.read_text())
        except ValueError:
            data = None
        if isinstance(data, dict) and "hybrid-search" in data.get("mcpServers", {}):
            srv = data["mcpServers"]["hybrid-search"]
            # Ownership check: only remove a registration that actually runs
            # our server — a user's unrelated entry under the same key stays.
            ours = isinstance(srv, dict) and (
                "hybrid_search.server" in " ".join(str(a) for a in srv.get("args", []))
                or "hybrid_search" in str(srv.get("command", ""))
            )
            if ours:
                del data["mcpServers"]["hybrid-search"]
                claude_json.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
                removed.append(f"MCP server registration ({claude_json})")
            else:
                print("Kept mcpServers['hybrid-search'] — it does not point at our server.")

    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text())
        except ValueError:
            settings = None
        if isinstance(settings, dict) and isinstance(settings.get("hooks"), dict):
            # Same needles setup writes: the wiki/product hooks plus the
            # qa-hook memory entries. Anything else is the user's.
            needles = (
                "hybrid-search/wiki", "STALE.md", "wiki-gaps",
                "wiki/index.md", "hybrid_search.cli qa-hook",
            )

            def _is_ours(entry: object) -> bool:
                if not isinstance(entry, dict):
                    return False
                cmds = [str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict)]
                return any(n in c for n in needles for c in cmds)

            dropped = 0
            for event, entries in list(settings["hooks"].items()):
                if not isinstance(entries, list):
                    continue
                kept = [e for e in entries if not _is_ours(e)]
                dropped += len(entries) - len(kept)
                settings["hooks"][event] = kept
            if dropped:
                settings_path.write_text(
                    _json.dumps(settings, indent=2, ensure_ascii=False)
                )
                removed.append(f"{dropped} hook entr{'y' if dropped == 1 else 'ies'} ({settings_path})")

    skills_dst = Path.home() / ".claude" / "skills"
    our_skills = (
        "bootstrap-wiki", "maintain", "rebuild-index",
        "save-wiki", "search", "setup-hybrid-search",
    )
    import shutil as _shutil
    manifest = _load_skill_manifest(skills_dst)
    for name in our_skills:
        skill_dir = skills_dst / name
        skill_md = skill_dir / "skill.md"
        if not skill_md.exists():
            continue
        current_sha = _sha256_text(skill_md.read_text(encoding="utf-8"))
        # Ownership check: these are generic names — only delete content we
        # installed (manifest SHA). A user's own "search" skill survives.
        if manifest.get(name) != current_sha:
            print(f"Kept skill '{name}' — content is not ours (no manifest match).")
            continue
        backup = skill_dir / "skill.md.pre-memory-layer"
        if backup.exists():
            # The user had a skill of this name before setup — restore it.
            skill_md.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            backup.unlink()
            removed.append(f"skill {name} (user's original restored)")
        else:
            _shutil.rmtree(skill_dir)
            removed.append(f"skill {name}")
        manifest.pop(name, None)
    if manifest:
        _save_skill_manifest(skills_dst, manifest)
    else:
        (skills_dst / _SKILL_MANIFEST_NAME).unlink(missing_ok=True)

    if removed:
        for item in removed:
            print(f"Removed: {item}")
        print("Teardown complete. Restart Claude Code to apply.")
        print("Per-project files (.hybrid-search/, CLAUDE.md block) are untouched.")
    else:
        print("Nothing to remove — global surface not installed.")


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
        ".hybrid-search/qa-archive/",  # v0.4.0 — archive tier
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

    p_conv = sub.add_parser(
        "index-conversations",
        help="Index Claude Code + Codex transcripts for cross-tool recall",
    )
    p_conv.add_argument("--cwd", default=".", help="Project directory (default: .)")
    p_conv.add_argument(
        "--transcript",
        help="Index a single transcript file (per-session, used by Stop hooks)",
    )
    p_conv.add_argument(
        "--source",
        choices=["claude", "codex", "auto"],
        default="auto",
        help="Transcript source for --transcript (default: auto-detect)",
    )

    p_search = sub.add_parser("search", help="Hybrid BM25 + semantic search")
    p_search.add_argument("query", help="Search query (Korean or English)")
    p_search.add_argument("--cwd", default=".", help="Project directory (default: .)")
    p_search.add_argument("--project", help="Project name (auto-detected from cwd)")
    p_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
    p_search.add_argument("--file-pattern", help="Glob filter (e.g., '*.ts')")
    p_search.add_argument("--node-types", help="Comma-separated: function,class,method")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")

    p_recal = sub.add_parser(
        "recalibrate",
        help="Derive router confidence thresholds from a gold set",
    )
    p_recal.add_argument("--cwd", required=True, help="Project directory")
    p_recal.add_argument("--gold", required=True, help="Gold set JSON path")
    p_recal.add_argument("--project", help="Registered project name")
    p_recal.add_argument("--limit", type=int, default=10, help="Search limit")

    sub.add_parser("serve", help="Start MCP server (for Claude Code / MCP clients)")

    # ── Setup & admin ──
    p_setup = sub.add_parser("setup", help="One-time setup: register MCP server + memory hooks")
    p_setup.add_argument("--cwd", default=".", help="Project directory")
    p_setup.add_argument("--dry-run", action="store_true", help="Preview setup changes without writing")
    p_setup.add_argument("--force", action="store_true", help="Recover from corrupted routing markers")
    p_setup.add_argument(
        "--global-only",
        action="store_true",
        help="Register only the global surface (MCP, hooks, skills); skip project files",
    )

    sub.add_parser(
        "teardown",
        help="Remove the global surface setup installed (MCP, hooks, skills)",
    )

    p_doctor = sub.add_parser("doctor", help="Diagnose Memory Layer setup and corpus health")
    p_doctor.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_doctor.add_argument("--project", help="Project name (overrides --cwd)")

    p_maintain = sub.add_parser("maintain", help="Codex-friendly index/wiki maintenance")
    p_maintain.add_argument("--cwd", default=".", help="Project directory")
    p_maintain.add_argument(
        "--force-reindex",
        action="store_true",
        help="Force full reindex instead of normal scan",
    )
    p_maintain.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue to verify/status even when synthesis input is pending",
    )
    p_maintain.add_argument("--no-status", action="store_true", help="Skip final status output")

    p_reindex = sub.add_parser("reindex", help="Delta reindex a project")
    p_reindex.add_argument("--cwd", default=".", help="Project directory")
    p_reindex.add_argument("--force", action="store_true", help="Force full reindex")
    p_reindex.add_argument("--git-delta", action="store_true", help="Use git diff for changed-file detection with full-scan fallback")
    p_reindex.add_argument(
        "--include-content",
        action="store_true",
        help="Include content files normally skipped as retrieval noise",
    )
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

    p_viewer = sub.add_parser(
        "viewer", help="Render the local memory viewer (.hybrid-search/viewer.html)"
    )
    p_viewer.add_argument("--cwd", default=".", help="Project directory")
    p_viewer.add_argument("--open", action="store_true", help="Open in the default browser")

    p_drift = sub.add_parser(
        "drift",
        help="Check filesystem drift vs. index (Phase 6 L4 watchdog)",
    )
    p_drift.add_argument("--cwd", default=".", help="Project directory")
    p_drift.add_argument("-v", "--verbose", action="store_true",
                          help="List the drifted files")

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

    p_qa_stats = sub.add_parser("qa-stats", help="Summary of qa logs (active / archived / by type / by month)")
    p_qa_stats.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_stats.add_argument("--project", help="Project name (overrides --cwd)")

    p_qa_restore = sub.add_parser(
        "qa-restore",
        help="Restore an archived qa entry back into qa/",
    )
    p_qa_restore.add_argument("id", help="qa stem, hash prefix (≥4), or friendly id")
    p_qa_restore.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_qa_restore.add_argument("--project", help="Project name (overrides --cwd)")

    p_mem = sub.add_parser("memory-card", help="Create/list/show/grep compact memory cards")
    mem_sub = p_mem.add_subparsers(dest="memory_card_command")

    p_mem_create = mem_sub.add_parser("create", help="Create a memory card from a qa log")
    p_mem_create.add_argument("--from-qa", required=True, help="qa log id/hash/stem")
    p_mem_create.add_argument(
        "--type",
        choices=("memory_card", "domain_term"),
        default="memory_card",
        help="Card type to create",
    )
    p_mem_create.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_mem_create.add_argument("--project", help="Project name (overrides --cwd)")

    p_mem_list = mem_sub.add_parser("list", help="List memory cards")
    p_mem_list.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_mem_list.add_argument("--project", help="Project name (overrides --cwd)")
    p_mem_list.add_argument("--limit", type=int, default=20, help="Max cards (default: 20)")
    p_mem_list.add_argument("--json", action="store_true", help="Output as JSON")

    p_mem_show = mem_sub.add_parser("show", help="Show a memory card")
    p_mem_show.add_argument("id", help="memory card id/hash/stem")
    p_mem_show.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_mem_show.add_argument("--project", help="Project name (overrides --cwd)")

    p_mem_grep = mem_sub.add_parser("grep", help="Grep memory cards")
    p_mem_grep.add_argument("term", help="Substring to search for")
    p_mem_grep.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_mem_grep.add_argument("--project", help="Project name (overrides --cwd)")
    p_mem_grep.add_argument("--case-sensitive", action="store_true")

    p_mem_compact = sub.add_parser("memory", help="Memory maintenance commands")
    memory_sub = p_mem_compact.add_subparsers(dest="memory_command")
    p_compact = memory_sub.add_parser("compact", help="Promote qa logs into memory cards")
    p_compact.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_compact.add_argument("--project", help="Project name (overrides --cwd)")
    p_compact.add_argument("--since", help="Only compact qa newer than duration, e.g. 7d")
    p_compact.add_argument("--limit", type=int, default=None, help="Max qa logs to promote")
    p_compact.add_argument("--dry-run", action="store_true")
    p_compact.add_argument("--verbose", action="store_true")

    p_proc = memory_sub.add_parser("procedural", help="Procedural memory commands")
    proc_sub = p_proc.add_subparsers(dest="procedural_command")
    p_proc_review = proc_sub.add_parser("review", help="Write procedural candidates")
    p_proc_review.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_proc_review.add_argument("--project", help="Project name (overrides --cwd)")

    p_facts = memory_sub.add_parser("facts", help="Graph-lite facts commands")
    facts_sub = p_facts.add_subparsers(dest="facts_command")
    p_facts_export = facts_sub.add_parser("export", help="Export facts from memory cards")
    p_facts_export.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_facts_export.add_argument("--project", help="Project name (overrides --cwd)")
    p_facts_list = facts_sub.add_parser("list", help="List exported facts")
    p_facts_list.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_facts_list.add_argument("--project", help="Project name (overrides --cwd)")
    p_facts_list.add_argument("--query", help="Filter substring")
    p_facts_list.add_argument("--limit", type=int, default=20)

    p_refresh = memory_sub.add_parser("refresh", help="Compact memory, export facts, and reindex")
    p_refresh.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_refresh.add_argument("--project", help="Project name (overrides --cwd)")
    p_refresh.add_argument("--since", help="Only compact qa newer than duration, e.g. 7d")
    p_refresh.add_argument("--limit", type=int, default=None, help="Max qa logs to promote")
    p_refresh.add_argument("--force-reindex", action="store_true", help="Force full reindex after refresh")
    p_refresh.add_argument(
        "--allow-incomplete-hooks",
        action="store_true",
        help="Run even when Claude/Codex hooks are incomplete",
    )

    p_recall = memory_sub.add_parser("recall", help="Memory-first search over cards and qa logs")
    p_recall.add_argument("query", help="Recall question")
    p_recall.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_recall.add_argument("--project", help="Project name (overrides --cwd)")
    p_recall.add_argument("--limit", type=int, default=8)

    p_open = memory_sub.add_parser("open", help="Generate a static visual memory report")
    p_open.add_argument("--cwd", default=".", help="Project directory (auto-detect)")
    p_open.add_argument("--project", help="Project name (overrides --cwd)")
    p_open.add_argument("--no-open", action="store_true", help="Only print report path")

    p_integrity = sub.add_parser(
        "integrity",
        help="Run the Memory integrity pass now (qa stale + dedup + archive TTL)",
    )
    p_integrity.add_argument("--cwd", default=".", help="Project directory")
    p_integrity.add_argument(
        "--dedup-threshold",
        type=float,
        default=None,
        help="Override cosine similarity threshold (default: config value)",
    )
    p_integrity.add_argument("--verbose", action="store_true", help="List every archived path")

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
    p_qa_prune.add_argument(
        "--confirm-first-run",
        action="store_true",
        help="Drop the first-run dry-run gate on future reindex auto-prunes",
    )

    sub.add_parser(
        "qa-hook",
        help="Claude Code PreToolUse/SessionStart hook entry (reads stdin JSON)",
    )
    sub.add_parser(
        "codex-hook",
        help="Codex lifecycle hook entry (reads stdin JSON, writes JSON)",
    )

    p_wiki_clean = sub.add_parser(
        "wiki-cleanup",
        help="Delete wiki pages whose source files are no longer indexed",
    )
    p_wiki_clean.add_argument("--cwd", default=".", help="Project directory")
    p_wiki_clean.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    p_wiki_clean.add_argument("--verbose", action="store_true", help="List every orphan path")

    p_install_hook = sub.add_parser(
        "install-memory-hook",
        help="Merge the memory PreToolUse+SessionStart hook into .claude/settings.json",
    )
    p_install_hook.add_argument(
        "--cwd", default=".",
        help="Project directory whose .claude/settings.json to patch (default: cwd)",
    )
    p_install_hook.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Install into ~/.claude/settings.json (user scope) instead of project",
    )
    p_install_hook.add_argument(
        "--settings",
        choices=("shared", "local"),
        default="local",
        help="Target file: 'shared' = .claude/settings.json (check in to git), "
             "'local' = .claude/settings.local.json (default, gitignored)",
    )
    p_install_hook.add_argument("--dry-run", action="store_true")

    p_install_codex = sub.add_parser(
        "install-codex-hook",
        help="Install Codex memory hooks plus Codex MCP config",
    )
    p_install_codex.add_argument("--cwd", default=".", help="Project directory")
    p_install_codex.add_argument(
        "--user",
        action="store_true",
        help="Install into ~/.codex instead of project .codex",
    )
    p_install_codex.add_argument(
        "--target",
        choices=("local", "user"),
        default=None,
        help="Alias for scope selection: local = project .codex, user = ~/.codex",
    )
    p_install_codex.add_argument("--dry-run", action="store_true")

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
    elif args.command == "index-conversations":
        cmd_index_conversations(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "recalibrate":
        cmd_recalibrate(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "teardown":
        cmd_teardown(args)
    elif args.command == "doctor":
        cmd_doctor(args)
    elif args.command == "maintain":
        cmd_maintain(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "stale":
        cmd_stale(args)
    elif args.command == "viewer":
        cmd_viewer(args)
    elif args.command == "drift":
        cmd_drift(args)
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
    elif args.command == "qa-restore":
        cmd_qa_restore(args)
    elif args.command == "memory-card":
        if args.memory_card_command == "create":
            cmd_memory_card_create(args)
        elif args.memory_card_command == "list":
            cmd_memory_card_list(args)
        elif args.memory_card_command == "show":
            cmd_memory_card_show(args)
        elif args.memory_card_command == "grep":
            cmd_memory_card_grep(args)
        else:
            parser.parse_args([args.command, "--help"])
    elif args.command == "memory":
        if args.memory_command == "compact":
            cmd_memory_compact(args)
        elif args.memory_command == "procedural" and args.procedural_command == "review":
            cmd_memory_procedural_review(args)
        elif args.memory_command == "facts" and args.facts_command == "export":
            cmd_memory_facts_export(args)
        elif args.memory_command == "facts" and args.facts_command == "list":
            cmd_memory_facts_list(args)
        elif args.memory_command == "refresh":
            cmd_memory_refresh(args)
        elif args.memory_command == "recall":
            cmd_memory_recall(args)
        elif args.memory_command == "open":
            cmd_memory_open(args)
        else:
            parser.parse_args([args.command, "--help"])
    elif args.command == "integrity":
        cmd_integrity(args)
    elif args.command == "qa-prune":
        cmd_qa_prune(args)
    elif args.command == "qa-hook":
        cmd_qa_hook(args)
    elif args.command == "codex-hook":
        cmd_codex_hook(args)
    elif args.command == "install-memory-hook":
        cmd_install_memory_hook(args)
    elif args.command == "install-codex-hook":
        if getattr(args, "target", None) == "user":
            args.user = True
        cmd_install_codex_hook(args)
    elif args.command == "wiki-cleanup":
        cmd_wiki_cleanup(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
