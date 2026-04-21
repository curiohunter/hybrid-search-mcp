"""Tests for LLM Wiki Synthesis (Phase 9a-9d) — prepare/finalize/verify architecture."""

import json
from pathlib import Path

import pytest

from hybrid_search.index.synthesizer import (
    ModuleContext,
    SourceChunk,
    SymbolVerificationResult,
    compute_synthesis_hash,
    merge_synthesis_with_structure,
    verify_references,
    verify_symbols,
    should_skip_synthesis,
    prepare_context_file,
    finalize_module,
    _format_source_chunks,
    estimate_tokens,
    collect_module_context,
)
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB
from hybrid_search.storage.wiki import WikiStore


# -- Fixtures --

@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project with some source files."""
    src = tmp_path / "src"
    src.mkdir()
    auth = src / "auth.py"
    auth.write_text("def sign_in():\n    pass\n\ndef sign_out():\n    pass\n")
    login = src / "login.py"
    login.write_text("class LoginPage:\n    pass\n")
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


@pytest.fixture
def seeded_db(db: StoreDB) -> StoreDB:
    """DB with files, chunks, and a wiki page for synthesis testing."""
    conn = db._conn
    db.upsert_file(
        conn,
        FileRecord(
            id="file1", project_id="proj1", relative_path="src/auth.py",
            file_hash="hash_a", file_size=100, file_mtime="1", language="python",
        ),
    )
    db.upsert_file(
        conn,
        FileRecord(
            id="file2", project_id="proj1", relative_path="src/login.py",
            file_hash="hash_b", file_size=200, file_mtime="1", language="python",
        ),
    )
    db.insert_chunks(conn, [
        ChunkRecord(
            id="chunk1", file_id="file1", project_id="proj1",
            name="sign_in", content="def sign_in():\n    pass",
            start_line=1, end_line=2,
        ),
        ChunkRecord(
            id="chunk2", file_id="file1", project_id="proj1",
            name="sign_out", content="def sign_out():\n    pass",
            start_line=4, end_line=5,
        ),
        ChunkRecord(
            id="chunk3", file_id="file2", project_id="proj1",
            name="LoginPage", content="class LoginPage:\n    pass",
            start_line=1, end_line=2,
        ),
    ])
    conn.commit()

    wiki = WikiStore(conn, max_pages=100)
    wiki.compile_page(
        project_id="proj1",
        query="auth system",
        title="auth-system",
        content="# auth-system\n> 2 files\n\n## Files\n- `src/auth.py`\n- `src/login.py`\n\n## Entry Points\n- sign_in",
        tags=["auth"],
        file_dependencies=[
            {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1", "chunk2"]},
            {"file_id": "file2", "file_hash": "hash_b", "chunk_ids": ["chunk3"]},
        ],
    )
    return db


# -- compute_synthesis_hash --

class TestSynthesisHash:
    def test_same_input_same_hash(self):
        h1 = compute_synthesis_hash("wiki content", ["hash_a", "hash_b"])
        h2 = compute_synthesis_hash("wiki content", ["hash_a", "hash_b"])
        assert h1 == h2

    def test_different_input_different_hash(self):
        h1 = compute_synthesis_hash("wiki v1", ["hash_a"])
        h2 = compute_synthesis_hash("wiki v2", ["hash_a"])
        assert h1 != h2

    def test_hash_order_independent(self):
        h1 = compute_synthesis_hash("wiki", ["hash_b", "hash_a"])
        h2 = compute_synthesis_hash("wiki", ["hash_a", "hash_b"])
        assert h1 == h2

    def test_hash_is_16_chars(self):
        h = compute_synthesis_hash("content", ["h1"])
        assert len(h) == 16


# -- verify_references --

class TestVerifyReferences:
    def test_valid_reference(self, tmp_project: Path):
        content = "This function (`src/auth.py:L1`) handles login."
        result = verify_references(content, str(tmp_project))
        assert len(result.verified) == 1
        assert len(result.failed) == 0
        assert "src/auth.py:L1" in result.verified

    def test_invalid_file_reference(self, tmp_project: Path):
        content = "See `src/missing.py:L5` for details."
        result = verify_references(content, str(tmp_project))
        assert len(result.failed) == 1
        assert len(result.verified) == 0

    def test_invalid_line_number(self, tmp_project: Path):
        content = "Check `src/auth.py:L9999` for edge cases."
        result = verify_references(content, str(tmp_project))
        assert len(result.failed) == 1

    def test_failed_ref_removed_inline(self, tmp_project: Path):
        content = "Bad ref `src/missing.py:L1` here."
        result = verify_references(content, str(tmp_project))
        # Line is preserved, only the reference span is removed
        assert "Bad ref" in result.cleaned_content
        assert "here." in result.cleaned_content
        assert "missing.py" not in result.cleaned_content

    def test_no_references(self, tmp_project: Path):
        content = "No references at all."
        result = verify_references(content, str(tmp_project))
        assert len(result.verified) == 0
        assert len(result.failed) == 0
        assert result.cleaned_content == content


# -- merge_synthesis_with_structure --

class TestMergeSynthesis:
    def test_basic_merge(self):
        synthesis = "## Overview\nThis module handles auth.\n\n## Caveats\n- None"
        deterministic = "# auth-system\n> 2 files, 3 chunks\n\n## Files\n- src/auth.py\n- src/login.py"

        merged = merge_synthesis_with_structure(synthesis, deterministic, "auth-system")

        assert merged.startswith("# auth-system")
        assert "synthesized:" in merged
        assert "## Overview" in merged
        assert "<details>" in merged
        assert "## Files" in merged

    def test_strips_duplicate_title_from_synthesis(self):
        synthesis = "# auth-system\n## Overview\nHandles auth."
        deterministic = "# auth-system\n> 2 files\n\n## Files\n- file.py"

        merged = merge_synthesis_with_structure(synthesis, deterministic, "auth-system")
        title_count = sum(1 for line in merged.split("\n") if line.startswith("# "))
        assert title_count == 1

    def test_structural_content_in_details(self):
        synthesis = "## Overview\nAuth module."
        deterministic = "# auth\n> 2 files\n\n## Files\n- a.py\n\n## Call Relationships\n- a -> b"

        merged = merge_synthesis_with_structure(synthesis, deterministic, "auth")
        # Structural sections should be inside <details>
        details_idx = merged.index("<details>")
        assert "## Files" in merged[details_idx:]
        assert "## Call Relationships" in merged[details_idx:]


# -- _format_source_chunks --

class TestFormatSourceChunks:
    def test_basic_formatting(self):
        chunks = [
            SourceChunk(file_path="src/auth.py", name="sign_in", content="def sign_in():\n    pass", start_line=1),
        ]
        result = _format_source_chunks(chunks)
        assert "src/auth.py" in result
        assert "sign_in" in result
        assert "def sign_in" in result

    def test_truncation_with_budget(self):
        chunks = [
            SourceChunk(file_path=f"file{i}.py", name=f"func{i}", content="x" * 1000, start_line=1)
            for i in range(100)
        ]
        result = _format_source_chunks(chunks, max_budget=100)
        assert "truncated" in result


# -- estimate_tokens --

class TestEstimateTokens:
    def test_token_estimate(self):
        ctx = ModuleContext(
            module_name="auth",
            deterministic_wiki="# auth\n> 2 files",
            source_chunks=[
                SourceChunk("auth.py", "sign_in", "def sign_in(): pass", 1),
            ],
            related_summaries=["[[login]]: handles login"],
            file_paths=["auth.py"],
            file_hashes=["hash_a"],
        )
        est = estimate_tokens(ctx)
        assert est["module"] == "auth"
        assert est["input_tokens"] > 0
        assert est["source_chunks"] == 1


# -- collect_module_context --

class TestCollectModuleContext:
    def test_collects_context(self, seeded_db: StoreDB):
        ctx = collect_module_context(seeded_db, "proj1", "auth-system", "/tmp")
        assert ctx is not None
        assert ctx.module_name == "auth-system"
        assert len(ctx.source_chunks) == 3
        assert len(ctx.file_paths) == 2
        assert "hash_a" in ctx.file_hashes

    def test_missing_module_returns_none(self, seeded_db: StoreDB):
        ctx = collect_module_context(seeded_db, "proj1", "nonexistent-module", "/tmp")
        assert ctx is None


# -- prepare_context_file --

class TestPrepareContextFile:
    def test_writes_context_file(self, tmp_path: Path):
        ctx = ModuleContext(
            module_name="auth-system",
            deterministic_wiki="# auth-system\n> 2 files\n## Files\n- auth.py",
            source_chunks=[
                SourceChunk("auth.py", "sign_in", "def sign_in(): pass", 1),
            ],
            related_summaries=["[[login]]: handles login"],
            file_paths=["auth.py"],
            file_hashes=["hash_a"],
        )
        out_path = tmp_path / "_synthesis_input" / "auth-system.md"
        result = prepare_context_file(ctx, out_path)

        assert result.exists()
        content = result.read_text()
        assert "auth-system" in content
        assert "Deterministic Wiki" in content
        assert "Source Code" in content
        assert "def sign_in" in content
        assert "input_hash:" in content

    def test_creates_parent_dirs(self, tmp_path: Path):
        ctx = ModuleContext(
            module_name="test",
            deterministic_wiki="# test",
            source_chunks=[],
            related_summaries=[],
            file_paths=[],
            file_hashes=[],
        )
        deep_path = tmp_path / "a" / "b" / "c" / "test.md"
        result = prepare_context_file(ctx, deep_path)
        assert result.exists()


# -- finalize_module --

class TestFinalizeModule:
    def test_finalize_with_valid_refs(self, seeded_db: StoreDB, tmp_project: Path):
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        synthesis = "## Overview\nAuth handles login (`src/auth.py:L1`).\n\n## Caveats\n- None"

        result = finalize_module(
            seeded_db, "proj1", "auth-system", synthesis,
            str(tmp_project), wiki_dir,
        )

        assert "error" not in result
        assert result["verified_refs"] == 1
        assert result["failed_refs"] == 0

        # Check files were written
        wiki_path = wiki_dir / "auth-system.md"
        assert wiki_path.exists()
        merged = wiki_path.read_text()
        assert "## Overview" in merged
        assert "<details>" in merged

        raw_path = wiki_dir / "_raw" / "auth-system.raw.md"
        assert raw_path.exists()

    def test_finalize_removes_bad_refs(self, seeded_db: StoreDB, tmp_project: Path):
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        synthesis = "## Overview\nGood.\n\n## Caveats\n- Bad ref `src/missing.py:L99`"

        result = finalize_module(
            seeded_db, "proj1", "auth-system", synthesis,
            str(tmp_project), wiki_dir,
        )

        assert result["failed_refs"] == 1
        wiki_content = (wiki_dir / "auth-system.md").read_text()
        # The reference span is removed but surrounding text preserved
        assert "Bad ref" in wiki_content
        assert "missing.py:L99" not in wiki_content

    def test_finalize_updates_db_synthesis_meta(self, seeded_db: StoreDB, tmp_project: Path):
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        synthesis = "## Overview\nAuth module."

        finalize_module(
            seeded_db, "proj1", "auth-system", synthesis,
            str(tmp_project), wiki_dir,
        )

        wiki = WikiStore(seeded_db._conn, max_pages=100)
        page = wiki.lookup_page("proj1", query="auth system")
        assert page is not None
        assert page.synthesis_model == "claude-code"
        assert page.synthesis_hash is not None
        assert page.last_synthesized_at is not None

    def test_finalize_missing_module(self, seeded_db: StoreDB, tmp_project: Path):
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        result = finalize_module(
            seeded_db, "proj1", "nonexistent", "## Overview\nX.",
            str(tmp_project), wiki_dir,
        )
        assert "error" in result


# -- WikiStore synthesis metadata --

class TestWikiStoreSynthesis:
    def test_compile_with_synthesis_meta(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=100)
        wiki.compile_page(
            project_id="proj1",
            query="auth system",
            title="auth-system",
            content="# synthesized content",
            tags=["auth", "synthesized"],
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": []},
            ],
            synthesis_model="claude-code",
            synthesis_hash="abc123",
        )

        page = wiki.lookup_page("proj1", query="auth system")
        assert page is not None
        assert page.synthesis_model == "claude-code"
        assert page.synthesis_hash == "abc123"
        assert page.synthesis_version >= 1
        assert page.last_synthesized_at is not None

    def test_compile_without_synthesis_meta(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=100)
        page = wiki.lookup_page("proj1", query="auth system")
        assert page is not None
        assert page.synthesis_model is None
        assert page.synthesis_version == 0


# -- DB schema migration --

class TestSchemaMigration:
    def test_fresh_db_has_synthesis_columns(self, db: StoreDB):
        cols = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(wiki_pages)").fetchall()
        }
        assert "synthesis_model" in cols
        assert "synthesis_version" in cols
        assert "synthesis_hash" in cols
        assert "last_synthesized_at" in cols

    def test_schema_version_is_current(self, db: StoreDB):
        from hybrid_search.storage.db import SCHEMA_VERSION
        version = db.get_meta("schema_version")
        assert version == SCHEMA_VERSION


# -- Phase 9c: should_skip_synthesis --

class TestShouldSkipSynthesis:
    def test_skip_when_deps_unchanged(self, seeded_db: StoreDB, tmp_project: Path):
        """After finalize, same file hashes should skip."""
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        finalize_module(
            seeded_db, "proj1", "auth-system", "## Overview\nAuth.",
            str(tmp_project), wiki_dir,
        )

        skip, reason = should_skip_synthesis(seeded_db, "proj1", "auth-system", str(tmp_project))
        assert skip is True
        assert "unchanged" in reason

    def test_no_skip_when_never_synthesized(self, seeded_db: StoreDB, tmp_project: Path):
        """Unsynthesized pages should not skip."""
        skip, reason = should_skip_synthesis(seeded_db, "proj1", "auth-system", str(tmp_project))
        assert skip is False
        assert "never synthesized" in reason

    def test_no_skip_when_files_changed(self, seeded_db: StoreDB, tmp_project: Path):
        """After file change, should not skip."""
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        finalize_module(
            seeded_db, "proj1", "auth-system", "## Overview\nAuth.",
            str(tmp_project), wiki_dir,
        )

        # Simulate file hash change
        conn = seeded_db._conn
        conn.execute("UPDATE files SET file_hash = 'hash_changed' WHERE id = 'file1'")
        conn.commit()

        skip, reason = should_skip_synthesis(seeded_db, "proj1", "auth-system", str(tmp_project))
        assert skip is False
        assert "changed" in reason

    def test_no_skip_for_missing_module(self, seeded_db: StoreDB, tmp_project: Path):
        skip, reason = should_skip_synthesis(seeded_db, "proj1", "nonexistent", str(tmp_project))
        assert skip is False


# -- Phase 9c: WikiStore.get_synthesis_hash --

class TestGetSynthesisHash:
    def test_returns_none_when_not_synthesized(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=100)
        page = wiki.lookup_page("proj1", query="auth system")
        assert page is not None
        stored = wiki.get_synthesis_hash(page.id)
        assert stored is None

    def test_returns_hash_after_synthesis(self, seeded_db: StoreDB, tmp_project: Path):
        wiki_dir = tmp_project / ".hybrid-search" / "wiki"
        wiki_dir.mkdir(parents=True)

        finalize_module(
            seeded_db, "proj1", "auth-system", "## Overview\nAuth.",
            str(tmp_project), wiki_dir,
        )

        wiki = WikiStore(seeded_db._conn, max_pages=100)
        page = wiki.lookup_page("proj1", query="auth system")
        stored = wiki.get_synthesis_hash(page.id)
        assert stored is not None
        assert len(stored) == 16


# -- Phase 9c: find_indirectly_affected --

class TestFindIndirectlyAffected:
    def test_finds_linked_pages(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=100)
        # Create a second page
        wiki.compile_page(
            project_id="proj1",
            query="login module",
            title="login-module",
            content="# Login Module\n\nSee [[auth-system]] for auth.",
            tags=["login"],
            file_dependencies=[
                {"file_id": "file2", "file_hash": "hash_b", "chunk_ids": ["chunk3"]},
            ],
        )
        # Create a third page (unlinked)
        wiki.compile_page(
            project_id="proj1",
            query="config module",
            title="config-module",
            content="# Config Module\n\nConfig stuff.",
            tags=["config"],
            file_dependencies=[],
        )

        auth_page = wiki.lookup_page("proj1", query="auth system")
        affected = wiki.find_indirectly_affected(
            "proj1", [auth_page.id], max_hops=1,
        )

        # login-module links to auth-system, so it should be affected
        titles = {a["title"] for a in affected}
        assert "login-module" in titles
        # config-module is unlinked, should NOT be affected
        assert "config-module" not in titles

    def test_empty_when_no_links(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=100)
        auth_page = wiki.lookup_page("proj1", query="auth system")
        affected = wiki.find_indirectly_affected(
            "proj1", [auth_page.id], max_hops=1,
        )
        assert affected == []


# -- Phase 9d: verify_symbols --

class TestVerifySymbols:
    def test_finds_existing_symbols(self, seeded_db: StoreDB):
        content = "Uses `sign_in` and `LoginPage` for auth."
        result = verify_symbols(content, seeded_db, "proj1")
        assert "sign_in" in result.found
        assert "LoginPage" in result.found
        assert len(result.missing) == 0

    def test_detects_missing_symbols(self, seeded_db: StoreDB):
        content = "Uses `NonExistentClass` and `missing_function` for something."
        result = verify_symbols(content, seeded_db, "proj1")
        assert "NonExistentClass" in result.missing
        assert "missing_function" in result.missing

    def test_skips_common_words(self, seeded_db: StoreDB):
        content = "Returns `true` or `false`, uses `self` and `None`."
        result = verify_symbols(content, seeded_db, "proj1")
        # Common words should be skipped entirely
        assert "true" not in result.found
        assert "true" not in result.missing

    def test_skips_file_paths(self, seeded_db: StoreDB):
        content = "See `src/auth.py` for details."
        result = verify_symbols(content, seeded_db, "proj1")
        assert len(result.found) == 0
        assert len(result.missing) == 0

    def test_deduplicates_symbols(self, seeded_db: StoreDB):
        content = "Call `sign_in` then `sign_in` again."
        result = verify_symbols(content, seeded_db, "proj1")
        assert result.found.count("sign_in") == 1


# -- Phase 9d: has_chunk_matching_name --

class TestHasChunkMatchingName:
    def test_matches_partial_qualified_name(self, seeded_db: StoreDB):
        # Insert a chunk with qualified_name
        conn = seeded_db._conn
        conn.execute(
            """INSERT INTO chunks (id, file_id, project_id, name, qualified_name, content, start_line, end_line)
               VALUES ('qchunk', 'file1', 'proj1', 'method', 'MyClass.method', 'code', 1, 2)""",
        )
        conn.commit()

        assert seeded_db.has_chunk_matching_name("method", "proj1") is True
        assert seeded_db.has_chunk_matching_name("MyClass", "proj1") is True
        assert seeded_db.has_chunk_matching_name("ZZZ_nope", "proj1") is False
