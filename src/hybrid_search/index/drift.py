"""Index drift watchdog — Phase 6 L4.

Read-only check for "index is stale relative to disk". Wraps ``scan_project``
so the logic stays identical to what ``maintain`` would detect, but does not
mutate any store. Used by the ``drift`` CLI command and the ``maintain``
skill to decide whether a reindex is worth running.

We deliberately return a plain dataclass (not a MCP tool) so this stays in
the CLI + skill orchestration lane — every MCP tool carries ~1k tokens
permanently in every agent context, and drift-detection is a low-frequency
operation that does not warrant that cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import scan_project
from hybrid_search.storage.db import StoreDB


@dataclass(frozen=True)
class DriftReport:
    project_id: str
    added: list[str]      # relative paths not yet indexed
    changed: list[str]    # relative paths whose content/mtime differs from DB
    deleted: list[str]    # relative paths in DB but no longer on disk
    total_on_disk: int    # files the scanner considers indexable

    @property
    def drift_count(self) -> int:
        return len(self.added) + len(self.changed) + len(self.deleted)

    @property
    def is_drifted(self) -> bool:
        return self.drift_count > 0

    def summary_line(self) -> str:
        if not self.is_drifted:
            return f"in sync ({self.total_on_disk} files indexed)"
        return (
            f"drift {self.drift_count} "
            f"(+{len(self.added)} added, ~{len(self.changed)} changed, "
            f"-{len(self.deleted)} deleted)"
        )


def detect_drift(
    project_id: str,
    project_root: Path,
    db: StoreDB,
    config: IndexingConfig,
) -> DriftReport:
    """Diff disk state against the DB's recorded files. Read-only — the DB
    is not modified. Uses the same walker + ignore spec as the indexer, so
    the count of "in sync" files exactly matches what the indexer would see.
    """
    result = scan_project(project_root, project_id, db, config)
    added_rel = [str(p.relative_to(project_root.resolve())) for p in result.added]
    changed_rel = [str(p.relative_to(project_root.resolve())) for p in result.changed]
    return DriftReport(
        project_id=project_id,
        added=added_rel,
        changed=changed_rel,
        deleted=list(result.deleted),
        total_on_disk=(
            db.get_file_count(project_id) + len(added_rel) - len(result.deleted)
        ),
    )
