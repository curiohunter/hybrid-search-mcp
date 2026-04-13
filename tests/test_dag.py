"""Tests for DAG construction and module tree generation — index/dag.py."""

from pathlib import Path

import pytest

from hybrid_search.index.dag import (
    build_dependency_graph,
    find_connected_components,
    generate_wiki_plan,
    topological_sort,
    _derive_module_name,
    _deduplicate_names,
    ModuleNode,
)
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


PROJECT_ID = "test-project"


def _make_db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


def _seed_graph_db(db: StoreDB) -> None:
    """Seed DB with a small project graph:

    handler.py::handle_request → auth.py::login → auth.py::validate_token
                                → user.py::create_user
    utils.py::format_date  (isolated, no edges)
    utils.py::parse_input  (isolated, no edges)
    """
    with db.transaction() as conn:
        # Files
        db.upsert_file(conn, FileRecord(
            id="file-auth", project_id=PROJECT_ID,
            relative_path="src/auth/login.py", file_hash="h1",
        ))
        db.upsert_file(conn, FileRecord(
            id="file-user", project_id=PROJECT_ID,
            relative_path="src/user/create.py", file_hash="h2",
        ))
        db.upsert_file(conn, FileRecord(
            id="file-handler", project_id=PROJECT_ID,
            relative_path="src/api/handler.py", file_hash="h3",
        ))
        db.upsert_file(conn, FileRecord(
            id="file-utils", project_id=PROJECT_ID,
            relative_path="src/utils/helpers.py", file_hash="h4",
        ))

        # Chunks
        db.insert_chunks(conn, [
            ChunkRecord(
                id="chunk-login", file_id="file-auth", project_id=PROJECT_ID,
                name="login", qualified_name="src/auth/login.py::login",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-validate", file_id="file-auth", project_id=PROJECT_ID,
                name="validate_token", qualified_name="src/auth/login.py::validate_token",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-create-user", file_id="file-user", project_id=PROJECT_ID,
                name="create_user", qualified_name="src/user/create.py::create_user",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-handler", file_id="file-handler", project_id=PROJECT_ID,
                name="handle_request", qualified_name="src/api/handler.py::handle_request",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-format", file_id="file-utils", project_id=PROJECT_ID,
                name="format_date", qualified_name="src/utils/helpers.py::format_date",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-parse", file_id="file-utils", project_id=PROJECT_ID,
                name="parse_input", qualified_name="src/utils/helpers.py::parse_input",
                node_type="function",
            ),
        ])

        # Call edges (resolved with high/medium confidence)
        # handler → login (high)
        db.insert_call_edges(
            conn, "chunk-handler",
            [("login", "src/auth/login")],
            PROJECT_ID,
        )
        # handler → create_user (high)
        db.insert_call_edges(
            conn, "chunk-handler",
            [("create_user", "src/user/create")],
            PROJECT_ID,
        )
        # login → validate_token (high)
        db.insert_call_edges(
            conn, "chunk-login",
            [("validate_token", "src/auth/login")],
            PROJECT_ID,
        )

    # Resolve edges to simulate Phase 7 output
    edges = db.get_all_call_edges(PROJECT_ID)
    with db.transaction() as conn:
        for e in edges:
            if e["callee_name"] == "login":
                db.update_call_edge_resolution(conn, e["rowid"], "chunk-login", "src/auth/login.py::login", "high")
            elif e["callee_name"] == "create_user":
                db.update_call_edge_resolution(conn, e["rowid"], "chunk-create-user", "src/user/create.py::create_user", "high")
            elif e["callee_name"] == "validate_token":
                db.update_call_edge_resolution(conn, e["rowid"], "chunk-validate", "src/auth/login.py::validate_token", "high")


class TestBuildDependencyGraph:
    """Tests for build_dependency_graph()."""

    def test_builds_forward_and_reverse(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": "B", "confidence": "high"},
            {"caller_chunk_id": "A", "callee_chunk_id": "C", "confidence": "medium"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {"A": {"B", "C"}}
        assert rev == {"B": {"A"}, "C": {"A"}}

    def test_ignores_low_confidence(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": "B", "confidence": "low"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {}
        assert rev == {}

    def test_ignores_unresolved(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": None, "confidence": "high"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {}

    def test_ignores_self_loops(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": "A", "confidence": "high"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {}

    def test_empty_edges(self):
        fwd, rev = build_dependency_graph([])
        assert fwd == {}
        assert rev == {}


class TestFindConnectedComponents:
    """Tests for find_connected_components()."""

    def test_single_component(self):
        fwd = {"A": {"B"}, "B": {"C"}}
        rev = {"B": {"A"}, "C": {"B"}}
        all_ids = {"A", "B", "C"}
        comps = find_connected_components(fwd, rev, all_ids)
        assert len(comps) == 1
        assert comps[0] == {"A", "B", "C"}

    def test_two_components(self):
        fwd = {"A": {"B"}, "C": {"D"}}
        rev = {"B": {"A"}, "D": {"C"}}
        all_ids = {"A", "B", "C", "D", "E"}  # E is isolated
        comps = find_connected_components(fwd, rev, all_ids)
        assert len(comps) == 2
        comp_sizes = sorted([len(c) for c in comps], reverse=True)
        assert comp_sizes == [2, 2]

    def test_isolated_nodes_excluded(self):
        fwd = {"A": {"B"}}
        rev = {"B": {"A"}}
        all_ids = {"A", "B", "X", "Y"}
        comps = find_connected_components(fwd, rev, all_ids)
        assert len(comps) == 1
        assert "X" not in comps[0]
        assert "Y" not in comps[0]


class TestTopologicalSort:
    """Tests for topological_sort()."""

    def test_linear_chain(self):
        # A → B → C (A calls B, B calls C)
        fwd = {"A": {"B"}, "B": {"C"}}
        comp = {"A", "B", "C"}
        result = topological_sort(fwd, comp)
        # Bottom-up: C first, A last
        assert result.index("C") < result.index("B")
        assert result.index("B") < result.index("A")

    def test_fan_out(self):
        # A → B, A → C
        fwd = {"A": {"B", "C"}}
        comp = {"A", "B", "C"}
        result = topological_sort(fwd, comp)
        # A should be last (entry point)
        assert result[-1] == "A"

    def test_handles_cycle(self):
        # A → B → A (cycle)
        fwd = {"A": {"B"}, "B": {"A"}}
        comp = {"A", "B"}
        result = topological_sort(fwd, comp)
        assert set(result) == {"A", "B"}

    def test_single_node(self):
        fwd = {}
        comp = {"A"}
        result = topological_sort(fwd, comp)
        assert result == ["A"]


class TestDeriveModuleName:
    """Tests for _derive_module_name()."""

    def test_single_file(self):
        name = _derive_module_name(["src/auth/login.py"], [])
        assert name == "login"

    def test_shared_directory(self):
        files = ["src/auth/login.py", "src/auth/token.py", "src/auth/middleware.py"]
        name = _derive_module_name(files, [])
        assert name == "auth"

    def test_generic_prefix_skipped(self):
        files = ["src/billing/charge.py", "src/billing/invoice.py"]
        name = _derive_module_name(files, [])
        assert name == "billing"

    def test_no_common_prefix(self):
        files = ["services/auth.py", "hooks/useAuth.ts", "api/login.py"]
        name = _derive_module_name(files, [])
        # Should pick most common parent
        assert isinstance(name, str)
        assert len(name) > 0


class TestDeduplicateNames:
    """Tests for _deduplicate_names()."""

    def test_no_duplicates(self):
        modules = [ModuleNode(name="auth"), ModuleNode(name="billing")]
        _deduplicate_names(modules)
        assert [m.name for m in modules] == ["auth", "billing"]

    def test_duplicates_get_suffix(self):
        modules = [ModuleNode(name="utils"), ModuleNode(name="utils"), ModuleNode(name="auth")]
        _deduplicate_names(modules)
        names = [m.name for m in modules]
        assert names[0] == "utils-1"
        assert names[1] == "utils-2"
        assert names[2] == "auth"


class TestGenerateWikiPlan:
    """Integration tests for generate_wiki_plan()."""

    def test_produces_modules_from_seeded_graph(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan = generate_wiki_plan(db, PROJECT_ID)

        # Should have at least 1 graph-based module
        assert len(plan.modules) >= 1
        assert plan.total_chunks == 6  # 4 graph + 2 isolated

        # The 4 connected chunks should be in modules
        module_chunks = {cid for m in plan.modules for cid in m.chunks}
        assert "chunk-handler" in module_chunks
        assert "chunk-login" in module_chunks
        assert "chunk-validate" in module_chunks
        assert "chunk-create-user" in module_chunks

        db.close()

    def test_isolated_nodes_grouped_by_directory(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan = generate_wiki_plan(db, PROJECT_ID)

        # format_date and parse_input are isolated, same directory
        isolated_chunks = {cid for m in plan.isolated_modules for cid in m.chunks}
        assert "chunk-format" in isolated_chunks
        assert "chunk-parse" in isolated_chunks

        db.close()

    def test_coverage_calculation(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan = generate_wiki_plan(db, PROJECT_ID)

        # 4 in graph + 2 isolated = 6 total, all covered
        assert plan.coverage == 1.0

        db.close()

    def test_entry_points_identified(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan = generate_wiki_plan(db, PROJECT_ID)

        # handle_request has zero in-degree (nobody calls it)
        all_entry_points = [ep for m in plan.modules for ep in m.entry_points]
        assert any("handle_request" in ep for ep in all_entry_points)

        db.close()

    def test_empty_project(self, tmp_path):
        db = _make_db(tmp_path)
        plan = generate_wiki_plan(db, PROJECT_ID)
        assert plan.modules == []
        assert plan.isolated_modules == []
        assert plan.total_chunks == 0
        db.close()

    def test_no_edges_all_isolated(self, tmp_path):
        """Project with chunks but no call edges → all isolated."""
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f1", project_id=PROJECT_ID,
                relative_path="src/a.py", file_hash="h1",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(id="c1", file_id="f1", project_id=PROJECT_ID, name="foo", node_type="function"),
                ChunkRecord(id="c2", file_id="f1", project_id=PROJECT_ID, name="bar", node_type="function"),
            ])

        plan = generate_wiki_plan(db, PROJECT_ID)
        assert len(plan.modules) == 0
        assert len(plan.isolated_modules) >= 1
        db.close()
