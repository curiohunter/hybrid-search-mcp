"""Tests for call graph resolution — index/callgraph.py."""

from pathlib import Path

from hybrid_search.index.callgraph import (
    COMMON_NAMES,
    _module_matches,
    _resolve_single,
    resolve_call_edges,
)
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


PROJECT_ID = "test-project"


def _make_db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


def _seed_db(db: StoreDB) -> None:
    """Seed DB with files, chunks, and unresolved call edges for testing."""
    with db.transaction() as conn:
        # File 1: auth.py
        db.upsert_file(conn, FileRecord(
            id="file-auth", project_id=PROJECT_ID,
            relative_path="src/auth.py", file_hash="h1",
        ))
        db.insert_chunks(conn, [
            ChunkRecord(
                id="chunk-login", file_id="file-auth", project_id=PROJECT_ID,
                name="login", qualified_name="src/auth.py::login",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-validate", file_id="file-auth", project_id=PROJECT_ID,
                name="validate_token", qualified_name="src/auth.py::validate_token",
                node_type="function",
            ),
        ])

        # File 2: user.py
        db.upsert_file(conn, FileRecord(
            id="file-user", project_id=PROJECT_ID,
            relative_path="src/user.py", file_hash="h2",
        ))
        db.insert_chunks(conn, [
            ChunkRecord(
                id="chunk-create-user", file_id="file-user", project_id=PROJECT_ID,
                name="create_user", qualified_name="src/user.py::create_user",
                node_type="function",
            ),
            ChunkRecord(
                id="chunk-get-user", file_id="file-user", project_id=PROJECT_ID,
                name="get_user", qualified_name="src/user.py::get_user",
                node_type="function",
            ),
        ])

        # File 3: handler.py (calls into auth and user)
        db.upsert_file(conn, FileRecord(
            id="file-handler", project_id=PROJECT_ID,
            relative_path="src/handler.py", file_hash="h3",
        ))
        db.insert_chunks(conn, [
            ChunkRecord(
                id="chunk-handle-request", file_id="file-handler", project_id=PROJECT_ID,
                name="handle_request", qualified_name="src/handler.py::handle_request",
                node_type="function",
            ),
        ])

        # Call edges: handle_request → login, create_user, init (common name)
        db.insert_call_edges(conn, "chunk-handle-request", ["login", "create_user", "init"], PROJECT_ID)


class TestResolveSingle:
    """_resolve_single() 3-tier resolution tests."""

    def _build_indexes(self):
        qname_index = {
            "src/auth.py::login": "chunk-login",
            "src/auth.py::validate_token": "chunk-validate",
            "src/user.py::create_user": "chunk-create-user",
        }
        name_index = {
            "login": [("chunk-login", "src/auth.py::login")],
            "validate_token": [("chunk-validate", "src/auth.py::validate_token")],
            "create_user": [("chunk-create-user", "src/user.py::create_user")],
        }
        file_index = {
            "chunk-login": "file-auth",
            "chunk-validate": "file-auth",
            "chunk-create-user": "file-user",
        }
        return qname_index, name_index, file_index

    def test_high_confidence_with_module(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "login", "src/auth", None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "high"
        assert chunk_id == "chunk-login"

    def test_medium_confidence_single_candidate(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "create_user", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "medium"
        assert chunk_id == "chunk-create-user"

    def test_medium_confidence_dot_qualified(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "src/auth.py::login", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "medium"
        assert chunk_id == "chunk-login"

    def test_common_name_stays_low(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        # Add "init" as a single candidate
        name_index["init"] = [("chunk-init", "src/app.py::init")]
        chunk_id, qname, confidence = _resolve_single(
            "init", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        # "init" is in COMMON_NAMES → low confidence even with single match
        assert confidence == "low"

    def test_unresolved_no_candidates(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "nonexistent_function", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert chunk_id is None

    def test_same_file_preference(self) -> None:
        """When multiple candidates exist, prefer the one in the same file as the caller."""
        qname_index, name_index, file_index = self._build_indexes()
        # Two candidates for "helper"
        name_index["helper"] = [
            ("chunk-helper-auth", "src/auth.py::helper"),
            ("chunk-helper-user", "src/user.py::helper"),
        ]
        file_index["chunk-helper-auth"] = "file-auth"
        file_index["chunk-helper-user"] = "file-user"
        file_index["chunk-caller"] = "file-auth"

        chunk_id, qname, confidence = _resolve_single(
            "helper", None, "chunk-caller", qname_index, name_index, file_index, PROJECT_ID,
        )
        # Should prefer chunk in file-auth (same file as caller)
        assert chunk_id == "chunk-helper-auth"
        assert confidence == "medium"

    def test_multiple_candidates_no_same_file_returns_low(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        name_index["helper"] = [
            ("chunk-helper-auth", "src/auth.py::helper"),
            ("chunk-helper-user", "src/user.py::helper"),
        ]
        file_index["chunk-helper-auth"] = "file-auth"
        file_index["chunk-helper-user"] = "file-user"

        # Caller in a different file entirely
        file_index["chunk-other"] = "file-other"
        chunk_id, qname, confidence = _resolve_single(
            "helper", None, "chunk-other", qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "low"


class TestModuleMatches:
    """_module_matches() tests."""

    def test_exact_match(self) -> None:
        assert _module_matches("./auth", "auth.py::login")

    def test_prefix_stripped(self) -> None:
        assert _module_matches("@/services/auth", "services/auth.ts::login")

    def test_no_match(self) -> None:
        assert not _module_matches("./user", "auth.py::login")


class TestResolveCallEdgesIntegration:
    """Integration test for resolve_call_edges() with real StoreDB."""

    def test_resolves_edges(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _seed_db(db)
        stats = resolve_call_edges(db, PROJECT_ID)
        assert stats["total"] == 3
        # login → medium (single candidate), create_user → medium, init → unresolved (no chunk)
        assert stats["high"] + stats["medium"] + stats["low"] >= 2

    def test_no_edges(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        stats = resolve_call_edges(db, PROJECT_ID)
        assert stats == {"total": 0, "high": 0, "medium": 0, "low": 0, "unresolved": 0}

    def test_idempotent_resolution(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _seed_db(db)
        stats1 = resolve_call_edges(db, PROJECT_ID)
        stats2 = resolve_call_edges(db, PROJECT_ID)
        # Second run should not re-resolve already resolved edges (medium/high are skipped)
        assert stats2["unresolved"] <= stats1["unresolved"]


class TestCommonNames:
    """COMMON_NAMES filter coverage."""

    def test_common_names_contains_expected(self) -> None:
        for name in ["init", "get", "set", "run", "__init__", "toString"]:
            assert name in COMMON_NAMES

    def test_common_names_is_frozen(self) -> None:
        assert isinstance(COMMON_NAMES, frozenset)
