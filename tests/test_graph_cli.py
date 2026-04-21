"""M5 — graph exploration CLI helpers (god-nodes / shortest-path / subgraph).

Tests exercise the DB query + traversal logic directly, skipping the
entry-point wrapper (which requires ~/.hybrid-search config + registry).
"""

from __future__ import annotations

from pathlib import Path

from hybrid_search.cli import (
    _apply_god_nodes_to_index,
    _bfs_shortest_path,
    _format_god_nodes_section,
    _resolve_chunk_for_graph,
    _WIKI_GOD_END,
    _WIKI_GOD_START,
)
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


# ── annotate-wiki: format + apply helpers ──

_SAMPLE_ROWS: list[dict] = [
    {
        "id": "popular",
        "qualified_name": "mod.popular",
        "name": "popular",
        "node_type": "function",
        "in_degree": 3,
    },
    {
        "id": "mid",
        "qualified_name": "mod.mid",
        "name": "mid",
        "node_type": "function",
        "in_degree": 1,
    },
]


class TestFormatGodNodesSection:
    def test_empty_rows_returns_empty_string(self) -> None:
        assert _format_god_nodes_section([], {}, top=10) == ""

    def test_wraps_content_with_markers(self) -> None:
        section = _format_god_nodes_section(_SAMPLE_ROWS, {}, top=10)
        assert section.startswith(_WIKI_GOD_START)
        assert section.rstrip().endswith(_WIKI_GOD_END)

    def test_includes_symbol_and_in_degree(self) -> None:
        section = _format_god_nodes_section(_SAMPLE_ROWS, {}, top=10)
        assert "mod.popular" in section
        assert "in=3" in section
        assert "type=function" in section

    def test_uses_module_link_when_mapped(self) -> None:
        mapping = {"popular": "Core Search"}
        section = _format_god_nodes_section(_SAMPLE_ROWS, mapping, top=10)
        # Wikilink to [[Core Search]] with slug core-search.md
        assert "[[Core Search]](core-search.md)" in section
        # mid is unmapped
        assert "_(unscoped)_" in section

    def test_top_caps_rows(self) -> None:
        section = _format_god_nodes_section(_SAMPLE_ROWS, {}, top=1)
        assert "mod.popular" in section
        assert "mod.mid" not in section

    def test_heading_reflects_effective_top(self) -> None:
        # top=10 but only 2 rows — heading shows 2, not 10.
        section = _format_god_nodes_section(_SAMPLE_ROWS, {}, top=10)
        assert "Top 2" in section


class TestApplyGodNodesToIndex:
    def _make_section(self) -> str:
        return _format_god_nodes_section(_SAMPLE_ROWS, {}, top=10)

    def test_first_insert_after_h1(self) -> None:
        existing = "# Wiki Index\n\n## Modules (5)\n\n- [tests](tests.md)\n"
        section = self._make_section()
        result = _apply_god_nodes_to_index(existing, section)
        assert _WIKI_GOD_START in result
        # Heading must still precede the modules section.
        assert result.index("# Wiki Index") < result.index(_WIKI_GOD_START)
        assert result.index(_WIKI_GOD_START) < result.index("## Modules (5)")
        # Manual content preserved.
        assert "- [tests](tests.md)" in result

    def test_first_insert_without_h1_prepends(self) -> None:
        existing = "no heading here\n"
        section = self._make_section()
        result = _apply_god_nodes_to_index(existing, section)
        assert result.startswith(section) or result.lstrip().startswith(_WIKI_GOD_START)
        assert "no heading here" in result

    def test_idempotent_reapply(self) -> None:
        existing = "# Wiki Index\n\n## Modules\n\n- [a](a.md)\n"
        section = self._make_section()
        once = _apply_god_nodes_to_index(existing, section)
        twice = _apply_god_nodes_to_index(once, section)
        assert once == twice
        # Exactly one start marker and one end marker.
        assert once.count(_WIKI_GOD_START) == 1
        assert once.count(_WIKI_GOD_END) == 1

    def test_replaces_existing_block(self) -> None:
        old_section = (
            f"{_WIKI_GOD_START}\n## Old God Nodes\n\nstale content\n{_WIKI_GOD_END}"
        )
        existing = f"# Wiki Index\n\n{old_section}\n\n## Modules\n- [a](a.md)\n"
        new_section = self._make_section()
        result = _apply_god_nodes_to_index(existing, new_section)
        assert "stale content" not in result
        assert "mod.popular" in result
        # Manual modules section untouched.
        assert "- [a](a.md)" in result
        assert result.count(_WIKI_GOD_START) == 1

    def test_preserves_manual_content_outside_markers(self) -> None:
        manual = (
            "# Wiki Index\n\n"
            "## 프로젝트 개요\n\n수동으로 작성한 내용입니다.\n\n"
            "## Modules\n- [tests](tests.md)\n"
        )
        result = _apply_god_nodes_to_index(manual, self._make_section())
        assert "수동으로 작성한 내용입니다." in result
        assert "## 프로젝트 개요" in result
        assert "- [tests](tests.md)" in result

    def test_empty_section_strips_existing_block(self) -> None:
        old_section = f"{_WIKI_GOD_START}\nstuff\n{_WIKI_GOD_END}"
        existing = f"# Wiki Index\n\n{old_section}\n\n## Modules\n- [a](a.md)\n"
        result = _apply_god_nodes_to_index(existing, "")
        assert _WIKI_GOD_START not in result
        assert _WIKI_GOD_END not in result
        assert "- [a](a.md)" in result

    def test_empty_section_no_existing_block_is_noop(self) -> None:
        existing = "# Wiki Index\n\n## Modules\n- [a](a.md)\n"
        assert _apply_god_nodes_to_index(existing, "") == existing
