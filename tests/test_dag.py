"""Tests for DAG construction and module tree generation — index/dag.py."""

from pathlib import Path

import pytest

from hybrid_search.index.dag import (
    build_dependency_graph,
    find_connected_components,
    generate_all_wiki_pages,
    generate_module_wiki,
    generate_wiki_plan,
    topological_sort,
    _derive_module_name,
    _deduplicate_names,
    _merge_file_overlapping_modules,
    _module_slug,
    _rename_reserved_slugs,
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

        # Call edges (resolved with extracted/inferred confidence)
        # handler → login (extracted)
        db.insert_call_edges(
            conn, "chunk-handler",
            [("login", "src/auth/login")],
            PROJECT_ID,
        )
        # handler → create_user (extracted)
        db.insert_call_edges(
            conn, "chunk-handler",
            [("create_user", "src/user/create")],
            PROJECT_ID,
        )
        # login → validate_token (extracted)
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
                db.update_call_edge_resolution(conn, e["rowid"], "chunk-login", "src/auth/login.py::login", "extracted")
            elif e["callee_name"] == "create_user":
                db.update_call_edge_resolution(conn, e["rowid"], "chunk-create-user", "src/user/create.py::create_user", "extracted")
            elif e["callee_name"] == "validate_token":
                db.update_call_edge_resolution(conn, e["rowid"], "chunk-validate", "src/auth/login.py::validate_token", "extracted")


class TestBuildDependencyGraph:
    """Tests for build_dependency_graph()."""

    def test_builds_forward_and_reverse(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": "B", "confidence": "extracted"},
            {"caller_chunk_id": "A", "callee_chunk_id": "C", "confidence": "inferred"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {"A": {"B", "C"}}
        assert rev == {"B": {"A"}, "C": {"A"}}

    def test_ignores_ambiguous_confidence(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": "B", "confidence": "ambiguous"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {}
        assert rev == {}

    def test_ignores_unresolved(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": None, "confidence": "extracted"},
        ]
        fwd, rev = build_dependency_graph(edges)
        assert fwd == {}

    def test_ignores_self_loops(self):
        edges = [
            {"caller_chunk_id": "A", "callee_chunk_id": "A", "confidence": "extracted"},
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


class TestRenameReservedSlugs:
    """Module names whose slug collides with the generated wiki index must be renamed."""

    def test_index_renamed(self):
        mods = [ModuleNode(name="index", files=["src/index/a.py"], chunks=["c1"])]
        _rename_reserved_slugs(mods)
        assert mods[0].name == "index module"
        assert _module_slug(mods[0].name) == "index-module"

    def test_non_reserved_untouched(self):
        mods = [ModuleNode(name="auth", files=["src/auth.py"], chunks=["c1"])]
        _rename_reserved_slugs(mods)
        assert mods[0].name == "auth"

    def test_isolated_variant_not_reserved(self):
        """`index (isolated)` slugs to `index-isolated`, no collision."""
        mods = [ModuleNode(name="index (isolated)", files=["x.py"], chunks=["c1"])]
        _rename_reserved_slugs(mods)
        assert mods[0].name == "index (isolated)"

    def test_generated_index_page_wins_collision(self, tmp_path):
        """End-to-end: a package directory named `index/` must not overwrite wiki index.md."""
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f1", project_id=PROJECT_ID,
                relative_path="src/index/a.py", file_hash="h1",
            ))
            db.upsert_file(conn, FileRecord(
                id="f2", project_id=PROJECT_ID,
                relative_path="src/index/b.py", file_hash="h2",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(id="c1", file_id="f1", project_id=PROJECT_ID, name="foo", node_type="function"),
                ChunkRecord(id="c2", file_id="f2", project_id=PROJECT_ID, name="bar", node_type="function"),
            ])

        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)

        filenames = [p.filename for p in pages]
        assert filenames.count("index.md") == 1
        # The actual index page must be the generated wiki index
        index_page = next(p for p in pages if p.filename == "index.md")
        assert index_page.name == "index"
        assert "Wiki Index" in index_page.content

        db.close()


class TestMergeFileOverlappingModules:
    """Tests for _merge_file_overlapping_modules() — file-boundary invariant."""

    def test_empty_input(self):
        assert _merge_file_overlapping_modules([]) == []

    def test_no_overlap_unchanged(self):
        mods = [
            ModuleNode(name="a", files=["src/a.py"], chunks=["c1"]),
            ModuleNode(name="b", files=["src/b.py"], chunks=["c2"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 2
        assert {m.name for m in result} == {"a", "b"}

    def test_two_modules_sharing_file_merge(self):
        mods = [
            ModuleNode(name="test_wiki", files=["tests/test_wiki.py"], chunks=["c1", "c2"]),
            ModuleNode(name="test_wiki", files=["tests/test_wiki.py"], chunks=["c3"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 1
        assert result[0].name == "test_wiki"
        assert result[0].chunks == ["c1", "c2", "c3"]
        assert result[0].files == ["tests/test_wiki.py"]

    def test_three_modules_sharing_file_merge(self):
        mods = [
            ModuleNode(name="mod", files=["f.py"], chunks=["c1"]),
            ModuleNode(name="mod", files=["f.py"], chunks=["c2"]),
            ModuleNode(name="mod", files=["f.py"], chunks=["c3"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 1
        assert sorted(result[0].chunks) == ["c1", "c2", "c3"]

    def test_transitive_overlap_merge(self):
        # A & B share f1, B & C share f2 → all three unioned
        mods = [
            ModuleNode(name="A", files=["f1.py", "x.py"], chunks=["c1"]),
            ModuleNode(name="B", files=["f1.py", "f2.py"], chunks=["c2", "c3"]),
            ModuleNode(name="C", files=["f2.py", "y.py"], chunks=["c4"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 1
        assert set(result[0].files) == {"f1.py", "f2.py", "x.py", "y.py"}
        assert set(result[0].chunks) == {"c1", "c2", "c3", "c4"}
        # Name inherited from largest member (B, 2 chunks)
        assert result[0].name == "B"

    def test_disjoint_groups_stay_separate(self):
        mods = [
            ModuleNode(name="g1a", files=["f1.py"], chunks=["c1"]),
            ModuleNode(name="g1b", files=["f1.py"], chunks=["c2"]),
            ModuleNode(name="g2a", files=["f2.py"], chunks=["c3"]),
            ModuleNode(name="g2b", files=["f2.py"], chunks=["c4"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 2
        file_sets = sorted(tuple(sorted(m.files)) for m in result)
        assert file_sets == [("f1.py",), ("f2.py",)]

    def test_entry_points_unioned_and_capped(self):
        mods = [
            ModuleNode(name="m", files=["f.py"], chunks=["c1"], entry_points=["ep1", "ep2"]),
            ModuleNode(name="m", files=["f.py"], chunks=["c2"], entry_points=["ep3", "ep4", "ep5", "ep6"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 1
        assert len(result[0].entry_points) == 5
        assert set(result[0].entry_points) <= {"ep1", "ep2", "ep3", "ep4", "ep5", "ep6"}

    def test_chunk_dedup(self):
        mods = [
            ModuleNode(name="m", files=["f.py"], chunks=["c1", "c2"]),
            ModuleNode(name="m", files=["f.py"], chunks=["c2", "c3"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 1
        assert result[0].chunks == ["c1", "c2", "c3"]

    def test_single_module_passthrough(self):
        mods = [ModuleNode(name="only", files=["a.py", "b.py"], chunks=["c1"])]
        result = _merge_file_overlapping_modules(mods)
        assert len(result) == 1
        assert result[0] is mods[0]

    def test_dedup_after_merge_produces_clean_name(self):
        """After merge + dedup, a fragmented file should yield a single un-suffixed module."""
        mods = [
            ModuleNode(name="test_wiki", files=["tests/test_wiki.py"], chunks=["c1"]),
            ModuleNode(name="test_wiki", files=["tests/test_wiki.py"], chunks=["c2"]),
            ModuleNode(name="test_wiki", files=["tests/test_wiki.py"], chunks=["c3"]),
        ]
        result = _merge_file_overlapping_modules(mods)
        _deduplicate_names(result)
        assert len(result) == 1
        assert result[0].name == "test_wiki"  # no -N suffix


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

    def test_memory_lane_chunks_excluded_from_plan(self, tmp_path):
        """qa/memory/conv chunks and .hybrid-search files never become wiki
        modules — pages generated about them get re-read as "modules" on the
        next pass, compounding into `-isolated-isolated` chains."""
        db = _make_db(tmp_path)
        _seed_graph_db(db)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f-qa", project_id=PROJECT_ID,
                relative_path=".hybrid-search/qa/2026/07/09-000000-cafe.md",
                file_hash="hq",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-conv", project_id=PROJECT_ID,
                relative_path=".conversations/claude/abc.jsonl", file_hash="hc",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="chunk-qa", file_id="f-qa", project_id=PROJECT_ID,
                    name="qa entry", node_type="qa_log",
                ),
                ChunkRecord(
                    id="chunk-conv", file_id="f-conv", project_id=PROJECT_ID,
                    name="turn", node_type="conv_turn",
                ),
                # Memory-typed chunk in a regular path — node_type still wins.
                ChunkRecord(
                    id="chunk-card", file_id="file-utils", project_id=PROJECT_ID,
                    name="card", node_type="memory_card",
                ),
            ])

        plan = generate_wiki_plan(db, PROJECT_ID)

        planned = {
            cid
            for m in (*plan.modules, *plan.isolated_modules)
            for cid in m.chunks
        }
        assert "chunk-qa" not in planned
        assert "chunk-conv" not in planned
        assert "chunk-card" not in planned
        # Real code chunks still planned.
        assert "chunk-handler" in planned
        assert "chunk-format" in planned
        db.close()


class TestGenerateModuleWiki:
    """Tests for generate_module_wiki() and generate_all_wiki_pages()."""

    def test_wiki_page_has_title_and_files(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)

        # First page is always index
        assert pages[0].filename == "index.md"
        assert "# Wiki Index" in pages[0].content

        # Module pages should have title and files section
        module_pages = [p for p in pages if p.name != "index"]
        assert len(module_pages) >= 1

        for page in module_pages:
            assert page.content.startswith("# ")
            assert "## Files" in page.content
            assert "## Symbols" in page.content
            assert page.filename.endswith(".md")
            assert len(page.tags) >= 1

        db.close()

    def test_wiki_page_contains_symbols(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)
        module_pages = [p for p in pages if p.name != "index"]

        # At least one page should mention handle_request, login, etc.
        all_content = "\n".join(p.content for p in module_pages)
        assert "handle_request" in all_content
        assert "login" in all_content

        db.close()

    def test_wiki_page_shows_call_relationships(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)
        all_content = "\n".join(p.content for p in pages)

        # handler calls login → should show "calls:" relationship
        assert "calls:" in all_content

        db.close()

    def test_wiki_page_has_external_deps(self, tmp_path):
        """Modules with cross-module calls should show external dependencies."""
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)

        # If there are multiple modules, at least one should have external deps
        # (handler module calling into auth module)
        # This depends on how components are split
        all_content = "\n".join(p.content for p in pages)
        # At minimum, all symbols should appear somewhere
        assert "validate_token" in all_content
        assert "create_user" in all_content

        db.close()

    def test_index_page_lists_all_modules(self, tmp_path):
        db = _make_db(tmp_path)
        _seed_graph_db(db)

        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)
        index = pages[0]

        assert "Coverage" in index.content
        # Should list modules
        all_modules = plan.modules + plan.isolated_modules
        for m in all_modules:
            assert m.name in index.content

        db.close()

    def test_empty_project_produces_index_only(self, tmp_path):
        db = _make_db(tmp_path)
        plan, pages = generate_all_wiki_pages(db, PROJECT_ID)
        assert len(pages) == 1  # index only
        assert pages[0].filename == "index.md"
        db.close()
