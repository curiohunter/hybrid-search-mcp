"""Wiki orphan cleanup — delete pages whose sources are no longer indexed.

The module_synth pass writes one wiki page per module, derived from a set of
source-file references. When a source disappears (real delete, branch switch,
or a new ``.gitignore`` entry) the scanner drops its record from the store
DB — but the markdown page on disk is left behind. Over time these orphans
bloat the wiki directory and pollute retrieval.

This module closes that leak:

- :func:`find_orphans` — compare wiki pages against the store DB's file set.
- :func:`cleanup_orphans` — delete orphans (with dry-run preview).

Both functions are pure I/O — no tests run them against a live network. The
reindex pipeline calls ``cleanup_orphans`` at the end of each run; the CLI
command ``hybrid-search-mcp wiki-cleanup`` exposes the same entry point for
one-shot maintenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_FILES_HEADING_RE = re.compile(r"^##\s+Files\s*$", re.MULTILINE)
_FILE_BULLET_RE = re.compile(r"^- `([^`]+)`\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s", re.MULTILINE)


def extract_file_refs(body: str) -> list[str]:
    """Pull relative-path bullets from the ``## Files`` section of a wiki page."""
    m = _FILES_HEADING_RE.search(body)
    if not m:
        return []
    start = m.end()
    nxt = _NEXT_HEADING_RE.search(body, start)
    block = body[start : nxt.start() if nxt else len(body)]
    return _FILE_BULLET_RE.findall(block)


@dataclass
class WikiCleanupResult:
    scanned: int
    orphans: list[Path]
    deleted: list[Path]
    skipped_errors: list[tuple[Path, str]]


def _normalise_ref(ref: str) -> str:
    # Pages sometimes reference a file with ``:Lx-Ly`` trailer; the DB
    # tracks the bare path. Chop anything after the first ``:``.
    return ref.split(":", 1)[0] if ":" in ref else ref


def find_orphans(
    wiki_dir: Path,
    indexed_paths: set[str],
) -> tuple[list[Path], int]:
    """Return ``(orphans, scanned_count)`` over ``wiki_dir``.

    A page is an **orphan** when *every* ``## Files`` bullet references a
    path that is not in ``indexed_paths``. Pages with no ``## Files``
    section (e.g. the root ``index.md``) are preserved — they're structural
    metadata, not module-derived.
    """
    if not wiki_dir.is_dir():
        return [], 0

    orphans: list[Path] = []
    scanned = 0
    for page in wiki_dir.glob("*.md"):
        scanned += 1
        try:
            body = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs = extract_file_refs(body)
        if not refs:
            continue
        if all(_normalise_ref(r) not in indexed_paths for r in refs):
            orphans.append(page)
    return orphans, scanned


def cleanup_orphans(
    wiki_dir: Path,
    indexed_paths: set[str],
    *,
    dry_run: bool = False,
) -> WikiCleanupResult:
    """Delete orphan wiki pages. Returns the full audit trail."""
    orphans, scanned = find_orphans(wiki_dir, indexed_paths)

    if dry_run or not orphans:
        return WikiCleanupResult(
            scanned=scanned,
            orphans=orphans,
            deleted=[],
            skipped_errors=[],
        )

    deleted: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for p in orphans:
        try:
            p.unlink()
            deleted.append(p)
        except OSError as exc:
            errors.append((p, str(exc)))
    return WikiCleanupResult(
        scanned=scanned,
        orphans=orphans,
        deleted=deleted,
        skipped_errors=errors,
    )


def collect_indexed_paths(project_root: Path) -> set[str] | None:
    """Return ``relative_path`` set from the project's store DB, or ``None``.

    Returns None when the project isn't registered or the DB is unreachable;
    callers should treat this as "can't run cleanup safely" and skip.
    """
    try:
        from hybrid_search.config import load_config
        from hybrid_search.project import ProjectRegistry
        from hybrid_search.storage.indexes import IndexPaths, get_project_dir
    except ImportError:
        return None

    try:
        cfg = load_config()
        registry = ProjectRegistry(cfg.global_dir)
    except Exception:
        return None

    info = registry.get_by_path(str(project_root))
    if info is None:
        for cand in registry.list_all():
            try:
                if Path(cand.path).resolve() == project_root.resolve():
                    info = cand
                    break
            except OSError:
                continue
    if info is None:
        return None

    import sqlite3

    try:
        pdir = get_project_dir(cfg.projects_dir, info.id)
        idx = IndexPaths(pdir)
        if not idx.store_db.exists():
            return None
        conn = sqlite3.connect(str(idx.store_db))
        try:
            cur = conn.execute("SELECT relative_path FROM files")
            return {row[0] for row in cur}
        finally:
            conn.close()
    except Exception:
        return None
