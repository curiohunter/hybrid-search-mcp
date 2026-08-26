"""Module discovery describes the code, not the memory layer's own output.

qa logs and memory cards are markdown living inside the project tree, so
the doc-mention pass union-found them into whatever module they happened
to name. On a live project that produced one module holding 2,442 files —
1,869 of them qa logs — whose 100,476-char summary (median: 334) then won
three unrelated gold queries on volume alone. Across that index, 2,590 of
4,563 file-to-module assignments were memory-layer output.

The wiki DAG already refused this content for the same reason. Discovery
did not.
"""

from __future__ import annotations

from pathlib import Path

from hybrid_search.index.modules import discover_modules
from hybrid_search.memory_lane import is_memory_lane_path
from hybrid_search.storage.db import FileRecord, StoreDB

PROJECT_ID = "p"


def _seed(db: StoreDB, root: Path, rel: str, body: str = "") -> None:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body)
    with db.transaction() as conn:
        db.upsert_file(
            conn,
            FileRecord(
                id=f"f_{abs(hash(rel)):x}", project_id=PROJECT_ID,
                relative_path=rel, file_hash="h",
            ),
        )


class TestMemoryLaneIsNotArchitecture:
    def _project(self, tmp_path):
        db = StoreDB(tmp_path / "store.db")
        root = tmp_path / "repo"
        _seed(db, root, "billing/charge.ts", "export const charge = 1\n")
        _seed(db, root, "billing/refund.ts", "export const refund = 1\n")
        # Memory-layer output that name-drops the module, exactly as a
        # recorded Q&A about billing would.
        for i in range(6):
            _seed(db, root, f".hybrid-search/qa/2026/06/{i}.md",
                  "# Q\n\nHow does `billing/charge.ts` work?\n")
        _seed(db, root, ".hybrid-search/memory/cards/2026/04/c.md",
              "billing/refund.ts decision\n")
        return db, root

    def test_memory_files_are_not_assigned_to_modules(self, tmp_path):
        db, root = self._project(tmp_path)
        discover_modules(db, PROJECT_ID, root)
        with db.transaction() as conn:
            rows = conn.execute(
                "SELECT f.relative_path FROM file_modules fm "
                "JOIN files f ON f.id = fm.file_id"
            ).fetchall()
        assigned = [r[0] for r in rows]
        assert assigned, "real source must still be grouped"
        assert not [p for p in assigned if is_memory_lane_path(p)]
        db.close()

    def test_real_source_is_still_grouped(self, tmp_path):
        db, root = self._project(tmp_path)
        stats = discover_modules(db, PROJECT_ID, root)
        assert stats["files_assigned"] >= 2
        db.close()

    def test_a_project_that_is_only_memory_yields_no_modules(self, tmp_path):
        """Nothing to describe — and certainly not a module made of qa logs."""
        db = StoreDB(tmp_path / "store.db")
        root = tmp_path / "repo"
        for i in range(4):
            _seed(db, root, f".hybrid-search/qa/{i}.md", "# Q\n\nnotes\n")
        stats = discover_modules(db, PROJECT_ID, root)
        assert stats == {"modules": 0, "files_assigned": 0}
        db.close()


class TestOneDefinition:
    def test_every_pass_shares_the_same_prefix_list(self):
        """Four copies drifted; the fourth was simply missing."""
        from hybrid_search import cli, wiki_cleanup
        from hybrid_search.memory_lane import MEMORY_LANE_PREFIXES

        assert wiki_cleanup._MEMORY_LANE_PREFIXES is MEMORY_LANE_PREFIXES
        assert cli._MEMORY_LANE_PREFIXES is MEMORY_LANE_PREFIXES
