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
        db.insert_call_edges(conn, "chunk-handle-request", [
            ("login", None), ("create_user", None), ("init", None),
        ], PROJECT_ID)


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

    def test_extracted_confidence_with_module(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "login", "src/auth", None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "extracted"
        assert chunk_id == "chunk-login"

    def test_inferred_confidence_single_candidate(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "create_user", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "inferred"
        assert chunk_id == "chunk-create-user"

    def test_inferred_confidence_dot_qualified(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        chunk_id, qname, confidence = _resolve_single(
            "src/auth.py::login", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "inferred"
        assert chunk_id == "chunk-login"

    def test_common_name_stays_ambiguous(self) -> None:
        qname_index, name_index, file_index = self._build_indexes()
        # Add "init" as a single candidate
        name_index["init"] = [("chunk-init", "src/app.py::init")]
        chunk_id, qname, confidence = _resolve_single(
            "init", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        # "init" is in COMMON_NAMES → ambiguous confidence even with single match
        assert confidence == "ambiguous"

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
        assert confidence == "inferred"

    def test_multiple_candidates_no_same_file_returns_ambiguous(self) -> None:
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
        assert confidence == "ambiguous"


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
        # login → inferred (single candidate), create_user → inferred, init → unresolved (no chunk)
        assert stats["extracted"] + stats["inferred"] + stats["ambiguous"] >= 2

    def test_no_edges(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        stats = resolve_call_edges(db, PROJECT_ID)
        assert stats == {"total": 0, "extracted": 0, "inferred": 0, "ambiguous": 0, "unresolved": 0}

    def test_idempotent_resolution(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        _seed_db(db)
        stats1 = resolve_call_edges(db, PROJECT_ID)
        stats2 = resolve_call_edges(db, PROJECT_ID)
        # Second run should not re-resolve already resolved edges (inferred/extracted are skipped)
        assert stats2["unresolved"] <= stats1["unresolved"]


class TestPassTwoAmbiguousUpgrade:
    """M9 pass 2: caller-file's resolved neighbors upgrade ambiguous → inferred."""

    def _seed_three_files(self, db: StoreDB) -> None:
        """Caller reaches src/service.py via one confident edge and src/other.py via none.
        Both files define `do_task`. Pass 2 should pick service.py's `do_task`.
        """
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f-caller", project_id=PROJECT_ID,
                relative_path="src/caller.py", file_hash="h1",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-service", project_id=PROJECT_ID,
                relative_path="src/service.py", file_hash="h2",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-other", project_id=PROJECT_ID,
                relative_path="src/other.py", file_hash="h3",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="c-run", file_id="f-caller", project_id=PROJECT_ID,
                    name="run", qualified_name="src/caller.py::run",
                    node_type="function",
                ),
                ChunkRecord(
                    id="c-svc-specific", file_id="f-service", project_id=PROJECT_ID,
                    name="specific", qualified_name="src/service.py::specific",
                    node_type="function",
                ),
                ChunkRecord(
                    id="c-svc-task", file_id="f-service", project_id=PROJECT_ID,
                    name="do_task", qualified_name="src/service.py::do_task",
                    node_type="function",
                ),
                ChunkRecord(
                    id="c-other-task", file_id="f-other", project_id=PROJECT_ID,
                    name="do_task", qualified_name="src/other.py::do_task",
                    node_type="function",
                ),
            ])
            # Edge 1: caller → specific (unique name, will resolve inferred)
            # Edge 2: caller → do_task (ambiguous: 2 candidates)
            db.insert_call_edges(conn, "c-run", [
                ("specific", None),
                ("do_task", None),
            ], PROJECT_ID)

    def test_ambiguous_upgrades_to_inferred_via_caller_context(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        self._seed_three_files(db)

        stats = resolve_call_edges(db, PROJECT_ID)

        # Pass 1: specific → inferred (unique), do_task → ambiguous (2 candidates).
        # Pass 2: do_task candidate in f-service matches a file the caller already
        #         reaches confidently via `specific` → upgrade to inferred.
        assert stats["pass2_upgraded"] >= 1
        assert stats["ambiguous"] == 0

        edges = db.get_all_call_edges(PROJECT_ID)
        do_task_edge = next(e for e in edges if e["callee_name"] == "do_task")
        assert do_task_edge["callee_chunk_id"] == "c-svc-task"
        assert do_task_edge["confidence"] == "inferred"

        db.close()

    def test_no_upgrade_when_no_candidate_in_related_files(self, tmp_path: Path) -> None:
        """If none of the ambiguous candidates live in a related file, stay ambiguous."""
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f-a", project_id=PROJECT_ID,
                relative_path="src/a.py", file_hash="h1",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-b", project_id=PROJECT_ID,
                relative_path="src/b.py", file_hash="h2",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-c", project_id=PROJECT_ID,
                relative_path="src/c.py", file_hash="h3",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="c-caller", file_id="f-a", project_id=PROJECT_ID,
                    name="run", qualified_name="src/a.py::run",
                    node_type="function",
                ),
                # 2 `lookup` candidates, neither in f-a
                ChunkRecord(
                    id="c-b", file_id="f-b", project_id=PROJECT_ID,
                    name="lookup", qualified_name="src/b.py::lookup",
                    node_type="function",
                ),
                ChunkRecord(
                    id="c-c", file_id="f-c", project_id=PROJECT_ID,
                    name="lookup", qualified_name="src/c.py::lookup",
                    node_type="function",
                ),
            ])
            db.insert_call_edges(conn, "c-caller", [("lookup", None)], PROJECT_ID)

        stats = resolve_call_edges(db, PROJECT_ID)

        # No confident edges from f-a to either candidate's file → no pass 2 upgrade
        assert stats["pass2_upgraded"] == 0
        assert stats["ambiguous"] == 1
        db.close()

    def test_self_file_targets_excluded_from_context(self, tmp_path: Path) -> None:
        """A confident same-file edge should not populate caller_to_targets — it
        would bias every same-file candidate upward, defeating the point.
        """
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f-caller", project_id=PROJECT_ID,
                relative_path="src/caller.py", file_hash="h1",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-other", project_id=PROJECT_ID,
                relative_path="src/other.py", file_hash="h2",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="c-caller", file_id="f-caller", project_id=PROJECT_ID,
                    name="run", qualified_name="src/caller.py::run",
                    node_type="function",
                ),
                # A unique helper in the caller's own file (confident same-file edge)
                ChunkRecord(
                    id="c-self-helper", file_id="f-caller", project_id=PROJECT_ID,
                    name="selfhelper", qualified_name="src/caller.py::selfhelper",
                    node_type="function",
                ),
                # Two `lookup` candidates: one in caller file, one in other file
                ChunkRecord(
                    id="c-caller-lookup", file_id="f-caller", project_id=PROJECT_ID,
                    name="lookup", qualified_name="src/caller.py::lookup",
                    node_type="function",
                ),
                ChunkRecord(
                    id="c-other-lookup", file_id="f-other", project_id=PROJECT_ID,
                    name="lookup", qualified_name="src/other.py::lookup",
                    node_type="function",
                ),
            ])
            db.insert_call_edges(conn, "c-caller", [
                ("selfhelper", None),  # confident (unique), same-file
                ("lookup", None),      # ambiguous — 2 candidates
            ], PROJECT_ID)

        stats = resolve_call_edges(db, PROJECT_ID)

        # Pass 1 already prefers same-file for lookup → resolves to caller-lookup
        # as inferred via Strategy 3. Pass 2 contributes no new upgrade here
        # (its own file is not added to caller_to_targets).
        edges = db.get_all_call_edges(PROJECT_ID)
        lookup_edge = next(e for e in edges if e["callee_name"] == "lookup")
        assert lookup_edge["callee_chunk_id"] == "c-caller-lookup"
        # pass2_upgraded is 0: same-file resolution was never at "ambiguous"
        assert stats["pass2_upgraded"] == 0
        db.close()


class TestImportCallBinding:
    """Phase 7 Step 1: Import-Call Binding tests."""

    def test_extracted_confidence_with_module_from_import(self, tmp_path: Path) -> None:
        """callee_module이 있으면 extracted confidence로 resolve."""
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="file-auth", project_id=PROJECT_ID,
                relative_path="src/auth.ts", file_hash="h1",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="chunk-login", file_id="file-auth", project_id=PROJECT_ID,
                    name="login", qualified_name="src/auth.ts::login",
                    node_type="function",
                ),
            ])
            db.upsert_file(conn, FileRecord(
                id="file-handler", project_id=PROJECT_ID,
                relative_path="src/handler.ts", file_hash="h2",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="chunk-handler", file_id="file-handler", project_id=PROJECT_ID,
                    name="handleRequest", qualified_name="src/handler.ts::handleRequest",
                    node_type="function",
                ),
            ])
            # Call edge WITH module info (import-call binding)
            db.insert_call_edges(conn, "chunk-handler", [
                ("login", "./auth"),
            ], PROJECT_ID)

        stats = resolve_call_edges(db, PROJECT_ID)
        assert stats["extracted"] == 1

    def test_import_call_binding_ts(self, tmp_path: Path) -> None:
        """TS: import { login } from './auth' → login() call has callee_module='./auth'."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        ts_source = '''
import { login, logout } from "./auth"
import { createUser } from "./user"

export function handleRequest() {
    login()
    createUser()
    unknownFunc()
}
'''
        f = tmp_path / "handler.ts"
        f.write_text(ts_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "typescript")
        # Find handleRequest chunk
        handler_chunk = next(c for c in chunks if c.name == "handleRequest")
        calls_dict = {name: module for name, module in handler_chunk.calls}

        assert calls_dict["login"] == "./auth"
        assert calls_dict["createUser"] == "./user"
        assert calls_dict["unknownFunc"] is None

    def test_import_call_binding_python(self, tmp_path: Path) -> None:
        """Python: from src.auth import login → login() call has callee_module='src.auth'."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        py_source = '''
from src.auth import login
from src.billing import charge

def handle_request():
    login()
    charge()
    unknown_func()
'''
        f = tmp_path / "handler.py"
        f.write_text(py_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "python")
        handler_chunk = next(c for c in chunks if c.name == "handle_request")
        calls_dict = {name: module for name, module in handler_chunk.calls}

        assert calls_dict["login"] == "src.auth"
        assert calls_dict["charge"] == "src.billing"
        assert calls_dict["unknown_func"] is None

    def test_unmatched_call_has_none_module(self, tmp_path: Path) -> None:
        """import에 없는 call은 callee_module=None, 기존 medium/low 로직으로 동작."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        ts_source = '''
import { login } from "./auth"

export function handler() {
    login()
    someLocalFunc()
}
'''
        f = tmp_path / "handler.ts"
        f.write_text(ts_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "typescript")
        handler_chunk = next(c for c in chunks if c.name == "handler")
        calls_dict = {name: module for name, module in handler_chunk.calls}

        assert calls_dict["login"] == "./auth"
        assert calls_dict["someLocalFunc"] is None

    def test_insert_call_edges_with_module(self, tmp_path: Path) -> None:
        """insert_call_edges correctly stores callee_module in DB."""
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f1", project_id=PROJECT_ID,
                relative_path="handler.ts", file_hash="h1",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="c1", file_id="f1", project_id=PROJECT_ID,
                    name="handler", qualified_name="handler.ts::handler",
                ),
            ])
            db.insert_call_edges(conn, "c1", [
                ("login", "./auth"),
                ("localFunc", None),
            ], PROJECT_ID)

        edges = db.get_all_call_edges(PROJECT_ID)
        edge_dict = {e["callee_name"]: e["callee_module"] for e in edges}
        assert edge_dict["login"] == "./auth"
        assert edge_dict["localFunc"] is None


class TestSelfMethodResolution:
    """Phase 7 Step 3: this/self method call resolution."""

    def test_self_method_binding_python(self, tmp_path: Path) -> None:
        """Python: self.validate() in AuthService → call tagged with __self__::AuthService."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        # Use large enough methods to avoid chunk merging
        py_source = '''
class AuthService:
    def validate(self):
        """ validate docstring for padding """
        x = 1
        y = 2
        z = 3
        a = 4
        b = 5
        c = 6
        d = 7
        e = 8
        f = 9
        g = 10
        h = 11
        i = 12
        return True

    def login(self):
        """ login docstring for padding """
        x = 1
        y = 2
        z = 3
        a = 4
        b = 5
        c = 6
        d = 7
        e = 8
        f = 9
        g = 10
        h = 11
        i = 12
        self.validate()
        return True
'''
        f = tmp_path / "auth.py"
        f.write_text(py_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "python")
        # Find any chunk containing self.validate() call
        self_calls = [
            (name, module)
            for c in chunks for name, module in c.calls
            if module and module.startswith("__self__")
        ]
        assert len(self_calls) >= 1
        assert self_calls[0] == ("validate", "__self__::AuthService")

    def test_this_method_binding_ts(self, tmp_path: Path) -> None:
        """TS: this.validate() in AuthService → call tagged with __self__::AuthService."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        ts_source = '''
class AuthService {
    validate() {
        const x = 1;
        const y = 2;
        const z = 3;
        const a = 4;
        const b = 5;
        const c = 6;
        const d = 7;
        const e = 8;
        const f = 9;
        const g = 10;
        return true;
    }

    login() {
        const x = 1;
        const y = 2;
        const z = 3;
        const a = 4;
        const b = 5;
        const c = 6;
        const d = 7;
        const e = 8;
        const f = 9;
        const g = 10;
        this.validate();
        return true;
    }
}
'''
        f = tmp_path / "auth.ts"
        f.write_text(ts_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "typescript")
        self_calls = [
            (name, module)
            for c in chunks for name, module in c.calls
            if module and module.startswith("__self__")
        ]
        assert len(self_calls) >= 1
        assert self_calls[0] == ("validate", "__self__::AuthService")

    def test_self_method_resolves_high(self, tmp_path: Path) -> None:
        """self method call resolves to High confidence via class_members index."""
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="file-auth", project_id=PROJECT_ID,
                relative_path="src/auth.py", file_hash="h1",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="chunk-validate", file_id="file-auth", project_id=PROJECT_ID,
                    name="validate", qualified_name="AuthService.validate",
                    node_type="method", parent_name="AuthService",
                ),
                ChunkRecord(
                    id="chunk-login", file_id="file-auth", project_id=PROJECT_ID,
                    name="login", qualified_name="AuthService.login",
                    node_type="method", parent_name="AuthService",
                ),
            ])
            db.insert_call_edges(conn, "chunk-login", [
                ("validate", "__self__::AuthService"),
            ], PROJECT_ID)

        stats = resolve_call_edges(db, PROJECT_ID)
        assert stats["extracted"] == 1

        # Verify the resolved edge
        edges = db.get_all_call_edges(PROJECT_ID)
        edge = next(e for e in edges if e["callee_name"] == "validate")
        assert edge["callee_chunk_id"] == "chunk-validate"
        assert edge["confidence"] == "extracted"


class TestCommonNameRelaxation:
    """Phase 7 Step 4: COMMON_NAMES with context upgrades confidence."""

    def test_common_name_with_module_upgrades_to_inferred(self) -> None:
        """'init' with callee_module should resolve as inferred, not ambiguous."""
        qname_index = {"src/app.py::init": "chunk-init"}
        name_index = {"init": [("chunk-init", "src/app.py::init")]}
        file_index = {"chunk-init": "file-app"}

        # Without module → ambiguous (existing behavior for common names)
        chunk_id, qname, confidence = _resolve_single(
            "init", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence == "ambiguous"

        # With module (import context) → inferred or extracted (Step 4: context upgrades)
        chunk_id, qname, confidence = _resolve_single(
            "init", "./app", None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert confidence in ("extracted", "inferred")

    def test_common_name_multiple_candidates_with_module(self) -> None:
        """Multiple candidates for common name should still resolve when module context exists."""
        qname_index = {
            "src/a.py::get": "chunk-get-a",
            "src/b.py::get": "chunk-get-b",
        }
        name_index = {
            "get": [
                ("chunk-get-a", "src/a.py::get"),
                ("chunk-get-b", "src/b.py::get"),
            ],
        }
        file_index = {"chunk-get-a": "file-a", "chunk-get-b": "file-b"}

        # Without module → unresolved (common name with multiple candidates)
        chunk_id, qname, confidence = _resolve_single(
            "get", None, None, qname_index, name_index, file_index, PROJECT_ID,
        )
        assert chunk_id is None

        # With module → attempts resolution
        chunk_id, qname, confidence = _resolve_single(
            "get", "./a", None, qname_index, name_index, file_index, PROJECT_ID,
        )
        # Should find a match now (has_context allows common name resolution)
        assert chunk_id is not None


class TestBuiltinFiltering:
    """Phase 7: Built-in / library call filtering to reduce noise."""

    def test_python_builtins_filtered(self, tmp_path: Path) -> None:
        """Python built-in calls (len, print, range) should not appear in calls."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        py_source = '''
from src.auth import login

def process():
    data = list(range(10))
    print(len(data))
    result = login()
    return str(result)
'''
        f = tmp_path / "handler.py"
        f.write_text(py_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "python")
        handler = next(c for c in chunks if c.name == "process")
        call_names = {name for name, _ in handler.calls}

        # Built-ins should be filtered out
        assert "len" not in call_names
        assert "print" not in call_names
        assert "range" not in call_names
        assert "list" not in call_names
        assert "str" not in call_names
        # Project import should remain
        assert "login" in call_names

    def test_ts_builtins_filtered(self, tmp_path: Path) -> None:
        """TS/JS built-in calls should be filtered."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        ts_source = '''
import { processData } from "./data"

export function handler() {
    console.log("start")
    const x = parseInt("42")
    const data = processData()
    return data
}
'''
        f = tmp_path / "handler.ts"
        f.write_text(ts_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "typescript")
        handler = next(c for c in chunks if c.name == "handler")
        call_names = {name for name, _ in handler.calls}

        # Built-ins filtered
        assert "log" not in call_names      # console.log → method builtin
        assert "parseInt" not in call_names  # direct builtin
        # Project import should remain
        assert "processData" in call_names

    def test_react_hooks_filtered(self, tmp_path: Path) -> None:
        """React hooks (useState, useEffect) should be filtered."""
        from hybrid_search.index.ast_chunker import chunk_code_file

        tsx_source = '''
import { fetchUser } from "./api"

export function UserProfile() {
    const state = useState(null)
    useEffect(() => { fetchUser() }, [])
    return null
}
'''
        f = tmp_path / "profile.tsx"
        f.write_text(tsx_source)
        chunks = chunk_code_file(f, tmp_path, "proj1", "typescript")
        # Find the chunk with calls
        all_calls = [(name, mod) for c in chunks for name, mod in c.calls]
        call_names = {name for name, _ in all_calls}

        assert "useState" not in call_names
        assert "useEffect" not in call_names
        assert "fetchUser" in call_names


class TestCommonNames:
    """COMMON_NAMES filter coverage."""

    def test_common_names_contains_expected(self) -> None:
        for name in ["init", "get", "set", "run", "__init__", "toString"]:
            assert name in COMMON_NAMES

    def test_common_names_is_frozen(self) -> None:
        assert isinstance(COMMON_NAMES, frozenset)


class TestConfidenceScorePersistence:
    """M1 — resolver must persist a numeric score alongside the label.

    Score fuels the fusion authority nudge; label drives existing filters.
    """

    def test_each_label_persists_expected_score(self, tmp_path: Path) -> None:
        from hybrid_search.storage.db import CONFIDENCE_SCORES

        db = _make_db(tmp_path)
        with db.transaction() as conn:
            # handler.ts → auth.ts (imported) + user.ts (no import) + handler.ts self-name
            db.upsert_file(conn, FileRecord(
                id="file-auth", project_id=PROJECT_ID,
                relative_path="src/auth.ts", file_hash="h1",
            ))
            db.upsert_file(conn, FileRecord(
                id="file-user", project_id=PROJECT_ID,
                relative_path="src/user.ts", file_hash="h2",
            ))
            db.upsert_file(conn, FileRecord(
                id="file-handler", project_id=PROJECT_ID,
                relative_path="src/handler.ts", file_hash="h3",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(
                    id="chunk-login", file_id="file-auth", project_id=PROJECT_ID,
                    name="login", qualified_name="src/auth.ts::login",
                ),
                ChunkRecord(
                    id="chunk-create-user", file_id="file-user", project_id=PROJECT_ID,
                    name="create_user", qualified_name="src/user.ts::create_user",
                ),
                # "init" is a COMMON_NAME — resolves to low without module context.
                ChunkRecord(
                    id="chunk-init", file_id="file-handler", project_id=PROJECT_ID,
                    name="init", qualified_name="src/handler.ts::init",
                ),
                ChunkRecord(
                    id="chunk-handler", file_id="file-handler", project_id=PROJECT_ID,
                    name="handler", qualified_name="src/handler.ts::handler",
                ),
            ])
            db.insert_call_edges(conn, "chunk-handler", [
                ("login", "./auth"),        # extracted — module + name match
                ("create_user", None),       # inferred — single name-only match
                ("init", None),              # ambiguous — common name w/o context
            ], PROJECT_ID)

        resolve_call_edges(db, PROJECT_ID)

        edges = {e["callee_name"]: e for e in db.get_all_call_edges(PROJECT_ID)}
        assert edges["login"]["confidence"] == "extracted"
        assert edges["login"]["confidence_score"] == CONFIDENCE_SCORES["extracted"]
        assert edges["create_user"]["confidence"] == "inferred"
        assert edges["create_user"]["confidence_score"] == CONFIDENCE_SCORES["inferred"]
        assert edges["init"]["confidence"] == "ambiguous"
        assert edges["init"]["confidence_score"] == CONFIDENCE_SCORES["ambiguous"]

    def test_unresolved_edge_keeps_default_score(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f", project_id=PROJECT_ID,
                relative_path="a.py", file_hash="h",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(id="caller", file_id="f", project_id=PROJECT_ID,
                            name="caller", qualified_name="a.py::caller"),
            ])
            db.insert_call_edges(conn, "caller", [
                ("nonexistent", None),
            ], PROJECT_ID)

        resolve_call_edges(db, PROJECT_ID)

        edges = db.get_all_call_edges(PROJECT_ID)
        assert edges[0]["callee_chunk_id"] is None
        # Default from column definition; no authority signal emitted.
        assert edges[0]["confidence_score"] == 0.0
