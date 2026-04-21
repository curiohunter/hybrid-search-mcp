"""M5 — graph exploration CLI helpers (god-nodes / shortest-path / subgraph).

Tests exercise the DB query + traversal logic directly, skipping the
entry-point wrapper (which requires ~/.hybrid-search config + registry).
"""

from __future__ import annotations

from pathlib import Path

from hybrid_search.cli import _bfs_shortest_path, _resolve_chunk_for_graph
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


def _seed_project(db: StoreDB, project_id: str = "p1") -> None:
    """Seed a small call graph:

        caller_a ─┐
                  ├─→ popular (called by 3)
        caller_b ─┤
                  │
        caller_c ─┘

        caller_a ──→ mid ──→ leaf
        isolated (no edges)
    """
    with db.transaction() as conn:
        db.upsert_file(conn, FileRecord(
            id="f1", project_id=project_id,
            relative_path="src/a.py", file_hash="h",
        ))
        db.insert_chunks(conn, [
            ChunkRecord(
                id="popular", file_id="f1", project_id=project_id,
                name="popular", qualified_name="mod.popular",
                node_type="function", start_line=10, end_line=20,
            ),
            ChunkRecord(
                id="mid", file_id="f1", project_id=project_id,
                name="mid", qualified_name="mod.mid",
                node_type="function", start_line=30, end_line=40,
            ),
            ChunkRecord(
                id="leaf", file_id="f1", project_id=project_id,
                name="leaf", qualified_name="mod.leaf",
                node_type="function", start_line=50, end_line=60,
            ),
            ChunkRecord(
                id="caller_a", file_id="f1", project_id=project_id,
                name="caller_a", qualified_name="mod.caller_a",
                node_type="function", start_line=70, end_line=80,
            ),
            ChunkRecord(
                id="caller_b", file_id="f1", project_id=project_id,
                name="caller_b", qualified_name="mod.caller_b",
                node_type="function", start_line=90, end_line=100,
            ),
            ChunkRecord(
                id="caller_c", file_id="f1", project_id=project_id,
                name="caller_c", qualified_name="mod.caller_c",
                node_type="function", start_line=110, end_line=120,
            ),
            ChunkRecord(
                id="isolated", file_id="f1", project_id=project_id,
                name="isolated", qualified_name="mod.isolated",
                node_type="function", start_line=130, end_line=140,
            ),
        ])
        for cid, conf, score in [
            ("caller_a", "extracted", 1.0),
            ("caller_b", "extracted", 1.0),
            ("caller_c", "inferred", 0.8),
        ]:
            conn.execute(
                """INSERT INTO call_edges
                   (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                    confidence, confidence_score)
                   VALUES (?, 'popular', 'popular', ?, ?, ?)""",
                (cid, project_id, conf, score),
            )
        # caller_a → mid → leaf (forward chain for shortest-path)
        conn.execute(
            """INSERT INTO call_edges
               (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                confidence, confidence_score)
               VALUES ('caller_a', 'mid', 'mid', ?, 'extracted', 1.0)""",
            (project_id,),
        )
        conn.execute(
            """INSERT INTO call_edges
               (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                confidence, confidence_score)
               VALUES ('mid', 'leaf', 'leaf', ?, 'extracted', 1.0)""",
            (project_id,),
        )
        # Ambiguous edge to mid — filtered out at inferred+ default.
        conn.execute(
            """INSERT INTO call_edges
               (caller_chunk_id, callee_name, callee_chunk_id, project_id,
                confidence, confidence_score)
               VALUES ('caller_b', 'mid', 'mid', ?, 'ambiguous', 0.3)""",
            (project_id,),
        )


class TestGodNodes:
    def test_returns_top_n_by_in_degree(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)

        rows = db.get_god_nodes("p1", limit=10, min_confidence="inferred")

        # popular has in_degree=3 (all three callers at ≥ inferred).
        # mid has in_degree=1 at inferred+ (caller_a extracted; caller_b ambiguous filtered).
        # leaf has in_degree=1.
        top = rows[0]
        assert top["id"] == "popular"
        assert top["in_degree"] == 3
        assert top["max_score"] == 1.0
        assert top["qualified_name"] == "mod.popular"
        assert top["relative_path"] == "src/a.py"

    def test_min_confidence_filters_edges(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)

        # At extracted-only: caller_c (inferred) drops from popular's callers.
        rows = db.get_god_nodes("p1", limit=10, min_confidence="extracted")
        popular = next(r for r in rows if r["id"] == "popular")
        assert popular["in_degree"] == 2

        # At ambiguous (include all): mid gains caller_b as well.
        rows = db.get_god_nodes("p1", limit=10, min_confidence="ambiguous")
        mid = next(r for r in rows if r["id"] == "mid")
        assert mid["in_degree"] == 2

    def test_limit_respected(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        rows = db.get_god_nodes("p1", limit=2, min_confidence="inferred")
        assert len(rows) == 2

    def test_isolated_chunk_excluded(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        rows = db.get_god_nodes("p1", limit=100, min_confidence="inferred")
        assert not any(r["id"] == "isolated" for r in rows)


class TestBfsShortestPath:
    def test_forward_direct_neighbor(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        path = _bfs_shortest_path(db, "p1", "caller_a", "mid", "inferred")
        assert path == ["caller_a", "mid"]

    def test_forward_two_hop(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        path = _bfs_shortest_path(db, "p1", "caller_a", "leaf", "inferred")
        assert path == ["caller_a", "mid", "leaf"]

    def test_self_is_trivial_path(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        assert _bfs_shortest_path(db, "p1", "popular", "popular", "inferred") == ["popular"]

    def test_no_path_returns_none(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        # leaf has no outgoing edges → no forward path to caller_a.
        assert _bfs_shortest_path(db, "p1", "leaf", "caller_a", "inferred") is None

    def test_min_confidence_filter_blocks_edge(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        # caller_b → mid is ambiguous; at extracted-only it drops out.
        assert _bfs_shortest_path(db, "p1", "caller_b", "mid", "extracted") is None


class TestResolveChunkForGraph:
    def test_resolves_by_raw_chunk_id(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        assert _resolve_chunk_for_graph(db, "p1", "popular") == "popular"

    def test_resolves_by_qualified_name(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        assert _resolve_chunk_for_graph(db, "p1", "mod.leaf") == "leaf"

    def test_resolves_by_bare_name(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        assert _resolve_chunk_for_graph(db, "p1", "mid") == "mid"

    def test_unknown_returns_none(self, tmp_path: Path) -> None:
        db = StoreDB(tmp_path / "store.db")
        _seed_project(db)
        assert _resolve_chunk_for_graph(db, "p1", "does_not_exist_xyz") is None
