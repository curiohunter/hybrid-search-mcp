"""Tests for the Reactive Wiki Layer (Phase 5)."""

from pathlib import Path

import pytest

from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB
from hybrid_search.storage.wiki import WikiStore, normalize_query, _page_id


# -- Fixtures --

@pytest.fixture
def db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


@pytest.fixture
def seeded_db(db: StoreDB) -> StoreDB:
    """DB with 2 files and 3 chunks for dependency testing."""
    conn = db._conn
    db.upsert_file(
        conn,
        FileRecord(
            id="file1", project_id="proj1", relative_path="src/auth.ts",
            file_hash="hash_a", file_size=100, file_mtime="1", language="typescript",
        ),
    )
    db.upsert_file(
        conn,
        FileRecord(
            id="file2", project_id="proj1", relative_path="src/login.ts",
            file_hash="hash_b", file_size=200, file_mtime="1", language="typescript",
        ),
    )
    db.insert_chunks(conn, [
        ChunkRecord(id="chunk1", file_id="file1", project_id="proj1", content="signIn()"),
        ChunkRecord(id="chunk2", file_id="file1", project_id="proj1", content="signOut()"),
        ChunkRecord(id="chunk3", file_id="file2", project_id="proj1", content="LoginPage"),
    ])
    conn.commit()
    return db


@pytest.fixture
def wiki(seeded_db: StoreDB) -> WikiStore:
    return WikiStore(seeded_db._conn, max_pages=100)


# -- normalize_query --

class TestNormalizeQuery:
    def test_basic(self):
        assert normalize_query("How does auth work") == "auth does how work"

    def test_case_insensitive(self):
        assert normalize_query("Auth FLOW") == "auth flow"

    def test_whitespace_collapse(self):
        assert normalize_query("  auth   flow  ") == "auth flow"

    def test_word_order_invariant(self):
        assert normalize_query("auth flow") == normalize_query("flow auth")

    def test_korean(self):
        result = normalize_query("로그인 인증 처리")
        assert "로그인" in result
        assert "인증" in result

    def test_truncation(self):
        long_query = "word " * 100
        assert len(normalize_query(long_query)) <= 200

    def test_empty(self):
        assert normalize_query("") == ""

    def test_single_word(self):
        assert normalize_query("auth") == "auth"


# -- compile + lookup round-trip --

class TestCompileAndLookup:
    def test_compile_returns_page_id(self, wiki: WikiStore):
        result = wiki.compile_page(
            project_id="proj1",
            query="how does auth work",
            title="Auth Flow",
            content="## Auth\nUses JWT tokens.",
            tags=["auth", "security"],
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1"]},
            ],
        )
        assert "page_id" in result
        assert result["query_key"] == "auth does how work"
        assert result["evicted_count"] == 0

    def test_lookup_by_query(self, wiki: WikiStore):
        wiki.compile_page(
            project_id="proj1", query="auth flow", title="Auth",
            content="content", tags=None, file_dependencies=[],
        )
        page = wiki.lookup_page("proj1", query="auth flow")
        assert page is not None
        assert page.title == "Auth"
        assert page.content == "content"
        assert page.access_count == 2  # compile (1) + lookup (1+1=2)

    def test_lookup_word_order_invariant(self, wiki: WikiStore):
        wiki.compile_page(
            project_id="proj1", query="auth flow", title="Auth",
            content="x", tags=None, file_dependencies=[],
        )
        page = wiki.lookup_page("proj1", query="flow auth")
        assert page is not None

    def test_lookup_by_tag(self, wiki: WikiStore):
        wiki.compile_page(
            project_id="proj1", query="auth flow", title="Auth",
            content="x", tags=["auth", "security"], file_dependencies=[],
        )
        page = wiki.lookup_page("proj1", tag="security")
        assert page is not None
        assert page.title == "Auth"

    def test_lookup_nonexistent(self, wiki: WikiStore):
        page = wiki.lookup_page("proj1", query="nonexistent thing")
        assert page is None

    def test_compile_overwrites_existing(self, wiki: WikiStore):
        wiki.compile_page(
            project_id="proj1", query="auth", title="v1",
            content="old", tags=None, file_dependencies=[],
        )
        wiki.compile_page(
            project_id="proj1", query="auth", title="v2",
            content="new", tags=None, file_dependencies=[],
        )
        page = wiki.lookup_page("proj1", query="auth")
        assert page is not None
        assert page.title == "v2"
        assert page.content == "new"
        assert page.version == 2  # 1 (initial) + 1 (overwrite)


# -- staleness detection --

class TestStaleness:
    def test_clean_page_not_stale(self, wiki: WikiStore, seeded_db: StoreDB):
        wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=None,
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1"]},
            ],
        )
        results = wiki.check_staleness("proj1")
        assert len(results) == 1
        assert results[0]["stale"] is False

    def test_stale_after_file_change(self, wiki: WikiStore, seeded_db: StoreDB):
        wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=None,
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1"]},
            ],
        )
        # Simulate file change
        seeded_db._conn.execute(
            "UPDATE files SET file_hash = 'hash_a_v2' WHERE id = 'file1'"
        )
        results = wiki.check_staleness("proj1")
        assert results[0]["stale"] is True
        assert len(results[0]["changed_files"]) == 1

    def test_stale_changed_files_detail(self, wiki: WikiStore, seeded_db: StoreDB):
        wiki.compile_page(
            project_id="proj1", query="auth login", title="Auth",
            content="x", tags=None,
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1"]},
                {"file_id": "file2", "file_hash": "hash_b", "chunk_ids": ["chunk3"]},
            ],
        )
        # Only change file2
        seeded_db._conn.execute(
            "UPDATE files SET file_hash = 'hash_b_v2' WHERE id = 'file2'"
        )
        results = wiki.check_staleness("proj1")
        assert results[0]["stale"] is True
        assert "src/login.ts" in results[0]["changed_files"]
        assert len(results[0]["changed_files"]) == 1

    def test_check_specific_page(self, wiki: WikiStore, seeded_db: StoreDB):
        result = wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=None, file_dependencies=[],
        )
        results = wiki.check_staleness("proj1", page_id=result["page_id"])
        assert len(results) == 1


# -- refresh --

class TestRefresh:
    def test_refresh_bumps_version(self, wiki: WikiStore):
        result = wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="v1", tags=None, file_dependencies=[],
        )
        refreshed = wiki.refresh_page(result["page_id"], content="v2")
        assert refreshed is not None
        assert refreshed["version"] == 2

    def test_refresh_re_snapshots_hashes(self, wiki: WikiStore, seeded_db: StoreDB):
        result = wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="v1", tags=None,
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1"]},
            ],
        )
        # Change file hash
        seeded_db._conn.execute(
            "UPDATE files SET file_hash = 'hash_a_v2' WHERE id = 'file1'"
        )
        # Refresh without new deps (re-snapshots existing)
        wiki.refresh_page(result["page_id"], content="v2")

        # Should no longer be stale
        staleness = wiki.check_staleness("proj1", page_id=result["page_id"])
        assert staleness[0]["stale"] is False

    def test_refresh_nonexistent(self, wiki: WikiStore):
        result = wiki.refresh_page("nonexistent", content="x")
        assert result is None


# -- LRU eviction --

class TestEviction:
    def test_lru_eviction(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=3)
        for i in range(4):
            wiki.compile_page(
                project_id="proj1", query=f"query {i}", title=f"Page {i}",
                content=f"content {i}", tags=None, file_dependencies=[],
            )
        pages = wiki.list_pages("proj1")
        assert len(pages) == 3

    def test_lru_eviction_respects_access(self, seeded_db: StoreDB):
        wiki = WikiStore(seeded_db._conn, max_pages=3)
        # Create 3 pages
        for i in range(3):
            wiki.compile_page(
                project_id="proj1", query=f"query {i}", title=f"Page {i}",
                content=f"content {i}", tags=None, file_dependencies=[],
            )
        # Access page 0 (makes it most recently accessed)
        wiki.lookup_page("proj1", query="query 0")
        # Add page 3 → should evict page 1 (least recently accessed)
        wiki.compile_page(
            project_id="proj1", query="query 3", title="Page 3",
            content="content 3", tags=None, file_dependencies=[],
        )
        pages = wiki.list_pages("proj1")
        titles = {p["title"] for p in pages}
        assert "Page 0" in titles  # was accessed, survived
        assert "Page 1" not in titles  # evicted


# -- delete --

class TestDelete:
    def test_delete_page(self, wiki: WikiStore):
        result = wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=None, file_dependencies=[],
        )
        assert wiki.delete_page(result["page_id"]) is True
        page = wiki.lookup_page("proj1", query="auth")
        assert page is None

    def test_delete_nonexistent(self, wiki: WikiStore):
        assert wiki.delete_page("nonexistent") is False


# -- dependency cascade --

class TestCascade:
    def test_file_delete_cascades_to_dependencies(self, wiki: WikiStore, seeded_db: StoreDB):
        wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=None,
            file_dependencies=[
                {"file_id": "file1", "file_hash": "hash_a", "chunk_ids": ["chunk1"]},
            ],
        )
        # Delete the file — FK CASCADE should remove wiki_dependencies
        seeded_db._conn.execute("DELETE FROM files WHERE id = 'file1'")
        # Page should still exist but have no dependencies
        deps = seeded_db._conn.execute(
            "SELECT COUNT(*) as cnt FROM wiki_dependencies"
        ).fetchone()
        assert deps["cnt"] == 0


# -- list_pages --

class TestListPages:
    def test_list_pages(self, wiki: WikiStore):
        wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=["auth"], file_dependencies=[],
        )
        wiki.compile_page(
            project_id="proj1", query="login", title="Login",
            content="y", tags=["auth"], file_dependencies=[],
        )
        pages = wiki.list_pages("proj1")
        assert len(pages) == 2
        assert all("page_id" in p for p in pages)

    def test_access_count_increments(self, wiki: WikiStore):
        wiki.compile_page(
            project_id="proj1", query="auth", title="Auth",
            content="x", tags=None, file_dependencies=[],
        )
        wiki.lookup_page("proj1", query="auth")
        wiki.lookup_page("proj1", query="auth")
        page = wiki.lookup_page("proj1", query="auth")
        assert page is not None
        assert page.access_count == 4  # 1 (initial) + 3 lookups
