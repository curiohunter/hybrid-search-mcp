"""Wiki bloat audit — classify every wiki page by DB-indexed liveness.

Walks ``<project>/.hybrid-search/wiki/*.md``, extracts the file paths listed
under each page's ``## Files`` section, and checks which of those sources
are present in the project's store DB. A source on disk but not in the DB
(gitignore drift) counts the same as a deleted source — both mean the wiki
page can no longer be reached by search and is pure noise.

Categories:
  - ``healthy``         — every referenced file is in the DB
  - ``zombie``          — every referenced file is absent from the DB
  - ``partial_stale``   — some refs in DB, some gone
  - ``empty``           — no file list found (e.g. index pages)

Also measures the ``-isolated[-isolated...]`` collision-suffix pattern.

Usage:
    python scripts/wiki_bloat_audit.py --cwd /path --out report.md
    python scripts/wiki_bloat_audit.py --cwd /path --delete --dry-run
    python scripts/wiki_bloat_audit.py --cwd /path --delete
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# Heading that starts the file list in a wiki page. The body right after
# this heading is a sequence of ``- \`path\``` bullets.
_FILES_HEADING = re.compile(r"^##\s+Files\s*$", re.MULTILINE)
_FILE_BULLET = re.compile(r"^- `([^`]+)`\s*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s", re.MULTILINE)


@dataclass
class PageRecord:
    path: Path
    stem: str
    file_refs: list[str] = field(default_factory=list)
    existing_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    category: str = "unknown"
    isolated_depth: int = 0  # how many trailing "-isolated" tokens


def extract_file_refs(body: str) -> list[str]:
    """Pull ``- `path`` bullets from the section immediately after ## Files."""
    m = _FILES_HEADING.search(body)
    if not m:
        return []
    start = m.end()
    next_h = _NEXT_HEADING.search(body, start)
    block = body[start : next_h.start() if next_h else len(body)]
    return _FILE_BULLET.findall(block)


def count_isolated_depth(stem: str) -> int:
    """Number of trailing ``-isolated`` tokens (optionally with ``-<digit>``)."""
    # Collapse ``-isolated-3-isolated-isolated`` → depth=3 (the digit segment
    # is just a variant index, not an extra isolation level).
    cleaned = re.sub(r"-\d+(?=-isolated|$)", "", stem)
    parts = cleaned.split("-isolated")
    # "page" → ["page"] → depth 0
    # "page-isolated" → ["page", ""] → depth 1
    # "page-isolated-isolated" → ["page", "", ""] → depth 2
    return max(0, len(parts) - 1)


def audit_page(
    md_path: Path,
    project_root: Path,
    indexed_paths: set[str] | None = None,
) -> PageRecord:
    """Classify one wiki page.

    When ``indexed_paths`` is provided the judgement uses the DB snapshot
    (a reference is "alive" iff its relative path is in the index). Falls
    back to on-disk existence when the DB set is ``None``; useful for
    environments where the project isn't registered yet.
    """
    rec = PageRecord(path=md_path, stem=md_path.stem)
    try:
        body = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        rec.category = "unreadable"
        return rec

    rec.file_refs = extract_file_refs(body)
    rec.isolated_depth = count_isolated_depth(md_path.stem)

    if not rec.file_refs:
        rec.category = "empty"
        return rec

    for ref in rec.file_refs:
        clean = ref.split(":", 1)[0] if ":" in ref else ref
        if indexed_paths is not None:
            alive = clean in indexed_paths
        else:
            alive = (project_root / clean).is_file()
        (rec.existing_files if alive else rec.missing_files).append(ref)

    if not rec.existing_files:
        rec.category = "zombie"
    elif not rec.missing_files:
        rec.category = "healthy"
    else:
        rec.category = "partial_stale"
    return rec


def load_indexed_paths(project_root: Path) -> set[str] | None:
    """Return the set of ``relative_path`` values currently in the project's
    store DB, or ``None`` when the project isn't registered or the DB is
    unreachable (callers fall back to on-disk check).
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
        # Fall back to name match — projects registered by a sibling path
        # (e.g. symlink) may not match get_by_path exactly.
        for cand in registry.list_all():
            try:
                if Path(cand.path).resolve() == project_root:
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


def cluster_by_base(records: list[PageRecord]) -> dict[str, list[PageRecord]]:
    """Group pages sharing a base name, ignoring trailing isolation tokens."""
    clusters: dict[str, list[PageRecord]] = defaultdict(list)
    for rec in records:
        base = re.sub(r"(-isolated)+(-\d+)?$", "", rec.stem)
        base = re.sub(r"-\d+$", "", base)
        clusters[base].append(rec)
    return clusters


def render_report(project_name: str, records: list[PageRecord]) -> str:
    total = len(records)
    by_cat: Counter[str] = Counter(r.category for r in records)
    by_depth: Counter[int] = Counter(r.isolated_depth for r in records)

    clusters = cluster_by_base(records)
    big_clusters = sorted(
        ((name, pages) for name, pages in clusters.items() if len(pages) >= 3),
        key=lambda kv: -len(kv[1]),
    )

    zombie_examples = [r for r in records if r.category == "zombie"][:15]
    partial_examples = [r for r in records if r.category == "partial_stale"][:10]

    clean_after = by_cat.get("healthy", 0) + by_cat.get("empty", 0)
    reduction_pct = 0 if total == 0 else 100 * (total - clean_after) // total

    lines: list[str] = []
    lines.append(f"# Wiki bloat audit — {project_name}")
    lines.append("")
    lines.append(f"- Total pages: **{total}**")
    lines.append("")
    lines.append("## Category breakdown")
    lines.append("")
    lines.append("| category | count | % |")
    lines.append("|---|---:|---:|")
    for cat in ("healthy", "partial_stale", "zombie", "empty", "unreadable"):
        n = by_cat.get(cat, 0)
        if n == 0:
            continue
        pct = 0 if total == 0 else 100 * n / total
        lines.append(f"| {cat} | {n} | {pct:.1f}% |")
    lines.append("")
    lines.append(
        f"**Post-cleanup projection**: {clean_after} healthy pages "
        f"(≈ {reduction_pct}% reduction from zombie + partial_stale removal)."
    )
    lines.append("")

    lines.append("## Isolation-suffix depth distribution")
    lines.append("")
    lines.append("Counts pages by number of trailing ``-isolated`` tokens (collision markers).")
    lines.append("")
    lines.append("| depth | count | meaning |")
    lines.append("|---:|---:|---|")
    meanings = {
        0: "no collision marker (clean name)",
        1: "one collision level",
        2: "collision-of-collision",
        3: "triple collision",
    }
    for depth in sorted(by_depth.keys()):
        meaning = meanings.get(depth, f"{depth}× collisions")
        lines.append(f"| {depth} | {by_depth[depth]} | {meaning} |")
    lines.append("")

    lines.append(f"## Biggest base-name clusters (≥3 variants), top 20")
    lines.append("")
    lines.append("| base | variants |")
    lines.append("|---|---:|")
    for base, pages in big_clusters[:20]:
        lines.append(f"| `{base}` | {len(pages)} |")
    lines.append("")

    if zombie_examples:
        lines.append(f"## Zombie examples (random 15)")
        lines.append("")
        lines.append("Pages where every referenced source file has been deleted.")
        lines.append("")
        for r in zombie_examples:
            lines.append(f"- `{r.path.name}` → {len(r.missing_files)} dead refs")
            lines.append(f"  - first ref: `{r.missing_files[0]}`")
        lines.append("")

    if partial_examples:
        lines.append("## Partial-stale examples (random 10)")
        lines.append("")
        lines.append("Pages where some referenced files exist, others are gone.")
        lines.append("")
        for r in partial_examples:
            lines.append(
                f"- `{r.path.name}` → {len(r.existing_files)} alive / {len(r.missing_files)} dead"
            )
        lines.append("")

    lines.append("## Action recommendations")
    lines.append("")
    lines.append("1. **Zombie pages** — safe to bulk-delete on first cleanup pass.")
    lines.append("2. **Partial-stale** — regenerate (drop dead refs, keep live ones).")
    lines.append("3. **Collision suffixes (depth ≥ 2)** — rename using content hash;")
    lines.append("   collapse duplicates that point at the same set of files.")
    lines.append("4. Wire this logic into the reindex pipeline so the cleanup")
    lines.append("   happens automatically on every commit going forward.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True, help="Project root to audit")
    ap.add_argument("--out", default=None, help="Markdown report path")
    ap.add_argument(
        "--delete",
        action="store_true",
        help="Actually remove zombie pages (combine with --dry-run to preview)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="With --delete: list pages that would be removed without touching disk",
    )
    args = ap.parse_args()

    project_root = Path(args.cwd).resolve()
    wiki_dir = project_root / ".hybrid-search" / "wiki"
    if not wiki_dir.is_dir():
        print(f"No wiki directory at {wiki_dir}", file=sys.stderr)
        return 2

    indexed = load_indexed_paths(project_root)
    if indexed is None:
        print(
            "⚠ store DB unreachable — falling back to on-disk check "
            "(this may undercount gitignore-drift zombies)",
            file=sys.stderr,
        )
    else:
        print(f"DB snapshot: {len(indexed)} files indexed", file=sys.stderr)

    pages = sorted(wiki_dir.glob("*.md"))
    records = [audit_page(p, project_root, indexed) for p in pages]

    report = render_report(project_root.name, records)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Report → {args.out}")
    else:
        print(report)

    total = len(records)
    by_cat: Counter[str] = Counter(r.category for r in records)
    print(
        f"\n[summary] total={total}  healthy={by_cat.get('healthy',0)}  "
        f"zombie={by_cat.get('zombie',0)}  partial={by_cat.get('partial_stale',0)}  "
        f"empty={by_cat.get('empty',0)}",
        file=sys.stderr,
    )

    if args.delete:
        zombies = [r for r in records if r.category == "zombie"]
        if not zombies:
            print("No zombies to delete.", file=sys.stderr)
            return 0

        tag = "[dry-run]" if args.dry_run else "[delete]"
        print(f"\n{tag} {len(zombies)} zombie page(s) identified:", file=sys.stderr)
        # Print a handful as sanity
        for r in zombies[:10]:
            print(f"  {tag} {r.path.name}", file=sys.stderr)
        if len(zombies) > 10:
            print(f"  ... and {len(zombies) - 10} more", file=sys.stderr)

        if args.dry_run:
            print(f"\n{tag} nothing removed.", file=sys.stderr)
            return 0

        removed = 0
        failed = 0
        for r in zombies:
            try:
                r.path.unlink()
                removed += 1
            except OSError as exc:
                print(f"  failed to unlink {r.path.name}: {exc}", file=sys.stderr)
                failed += 1
        print(
            f"\n[delete] removed {removed} zombie page(s)"
            + (f" ({failed} failed)" if failed else ""),
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
