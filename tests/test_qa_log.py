"""Tests for the Q&A log memory layer (MVP)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hybrid_search.memory import qa_log


# --- Fakes ------------------------------------------------------------------

@dataclass
class _FakeResult:
    chunk_id: str = "c1"
    file_path: str = "src/foo.py"
    project: str = "demo"
    name: str | None = "do_thing"
    qualified_name: str | None = "foo.do_thing"
    node_type: str | None = "function"
    start_line: int | None = 10
    end_line: int | None = 20
    snippet: str = "def do_thing():\n    return 1"
    rrf_score: float = 0.5
    bm25_rank: int | None = 1
    vector_rank: int | None = 2
    content: str | None = None


@dataclass
class _FakeResponse:
    results: list[_FakeResult] = field(default_factory=list)
    query_type: str = "KOREAN_NL"
    effective_bm25_weight: float = 0.15
    query_time_ms: float = 12.3
    total_chunks_searched: int = 42
    reranked: bool = False
    skipped_projects: list[str] = field(default_factory=list)


@dataclass
class _FakeProjectInfo:
    path: str
    id: str = "pid1"


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(qa_log.ENV_TOGGLE, raising=False)
    yield


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")


def _mark_project(root: Path) -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return root


# --- Toggle -----------------------------------------------------------------

class TestToggle:
    def test_enabled_by_default(self):
        # Memory Layer is on out-of-the-box; users opt out explicitly.
        assert qa_log.is_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", ""])
    def test_enabled_when_not_opted_out(self, monkeypatch, val):
        monkeypatch.setenv(qa_log.ENV_TOGGLE, val)
        assert qa_log.is_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off"])
    def test_disabled_on_opt_out(self, monkeypatch, val):
        monkeypatch.setenv(qa_log.ENV_TOGGLE, val)
        assert qa_log.is_enabled() is False


# --- Path resolution --------------------------------------------------------

class TestResolveProjectRoot:
    def test_cwd_inside_registered_project(self, tmp_path):
        proj = tmp_path / "proj"
        sub = proj / "src" / "inner"
        sub.mkdir(parents=True)
        infos = [_FakeProjectInfo(path=str(proj))]
        got = qa_log._resolve_project_root(str(sub), infos)
        assert got == proj.resolve()

    def test_cwd_with_no_matching_project_uses_git_root(self, tmp_path):
        other = _mark_project(tmp_path / "other")
        sub = other / "docs" / "학습"
        sub.mkdir(parents=True)
        got = qa_log._resolve_project_root(str(sub), [_FakeProjectInfo(path=str(tmp_path / "x"))])
        assert got == other.resolve()

    def test_cwd_without_project_marker_returns_none(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        got = qa_log._resolve_project_root(str(other), [_FakeProjectInfo(path=str(tmp_path / "x"))])
        assert got is None

    def test_cwd_with_existing_memory_root_uses_that_root(self, tmp_path):
        root = tmp_path / "project"
        sub = root / "nested"
        (root / ".hybrid-search").mkdir(parents=True)
        sub.mkdir()
        got = qa_log._resolve_project_root(str(sub), None)
        assert got == root.resolve()

    def test_none_cwd_returns_none(self):
        assert qa_log._resolve_project_root(None, None) is None


# --- Path layout ------------------------------------------------------------

class TestPathLayout:
    def test_yyyy_mm_subdirs(self, tmp_path):
        from datetime import datetime, timezone
        ts = datetime(2026, 4, 21, 15, 30, 45, tzinfo=timezone.utc)
        p = qa_log._build_path(tmp_path, ts, "abcd1234")
        rel = p.relative_to(tmp_path)
        assert rel.parts[:3] == (".hybrid-search", "qa", "2026")
        assert rel.parts[3] == "04"
        assert rel.name == "21-153045-abcd1234.md"


# --- record() end-to-end ----------------------------------------------------

class TestRecord:
    def test_opt_out_creates_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv(qa_log.ENV_TOGGLE, "0")
        response = _FakeResponse(results=[_FakeResult()])
        path = qa_log.record(
            query="foo",
            response=response,
            cwd=str(tmp_path),
            async_write=False,
        )
        assert path is None
        assert not (tmp_path / ".hybrid-search" / "qa").exists()

    def test_sensitive_query_not_persisted(self, tmp_path):
        # Default-on, but secret-shaped queries never hit disk.
        response = _FakeResponse(results=[_FakeResult()])
        for secret in ("my github token is ghp_" + "A" * 40, "api_key=sk-proj-abcdef12345", "password: hunter2"):
            path = qa_log.record(
                query=secret,
                response=response,
                cwd=str(tmp_path),
                async_write=False,
            )
            assert path is None, f"leaked: {secret!r}"

    def test_record_turn_stores_bounded_answer_excerpt(self, tmp_path, enabled):
        _mark_project(tmp_path)
        answer = "첫 문장입니다. " + ("상세 설명 " * 400)
        path = qa_log.record_turn(
            query="why does memory improve",
            cwd=str(tmp_path),
            answer_chars=len(answer),
            answer_excerpt=answer,
            trigger="stop_hook",
        )
        assert path is not None
        content = path.read_text(encoding="utf-8")
        assert "answer_excerpt_chars:" in content
        assert "## Answer excerpt" in content
        assert "첫 문장입니다" in content
        assert len(content) < len(answer) + 1000

    def test_sensitive_answer_excerpt_is_omitted(self, tmp_path, enabled):
        _mark_project(tmp_path)
        path = qa_log.record_turn(
            query="summarize deployment",
            cwd=str(tmp_path),
            answer_chars=50,
            answer_excerpt="Use api_key=sk-proj-abcdef12345 for the service.",
            trigger="stop_hook",
        )
        assert path is not None
        content = path.read_text(encoding="utf-8")
        assert "## Answer excerpt" not in content
        assert "sk-proj" not in content

    def test_on_writes_file(self, tmp_path, enabled):
        _mark_project(tmp_path)
        response = _FakeResponse(results=[_FakeResult()])
        path = qa_log.record(
            query="검색 쿼리",
            response=response,
            cwd=str(tmp_path),
            async_write=False,
        )
        assert path is not None
        assert path.exists()
        assert path.parent.parts[-3] == "qa" or path.parent.parents[0].name == "qa"

    def test_frontmatter_fields_present(self, tmp_path, enabled):
        _mark_project(tmp_path)
        response = _FakeResponse(
            results=[_FakeResult(snippet="hello world")],
            query_type="KOREAN_NL",
            effective_bm25_weight=0.15,
            query_time_ms=8.5,
            total_chunks_searched=99,
        )
        path = qa_log.record(
            query="how does auth work",
            response=response,
            cwd=str(tmp_path),
            async_write=False,
        )
        assert path is not None
        content = path.read_text(encoding="utf-8")

        # YAML frontmatter boundaries
        assert content.startswith("---\n")
        head, _, _ = content.partition("\n---\n")
        # Required fields
        for field_name in (
            "query:",
            "query_type:",
            "effective_bm25_weight:",
            "query_time_ms:",
            "total_chunks_searched:",
            "timestamp:",
            "result_count:",
        ):
            assert field_name in head, f"missing {field_name} in frontmatter"

        # Body includes result snippet + chunk_id
        assert "hello world" in content
        assert "c1" in content

    def test_write_failure_is_swallowed(self, tmp_path, enabled, monkeypatch):
        """A crash during persistence must not propagate."""
        _mark_project(tmp_path)
        def _boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(qa_log, "_write_atomic", _boom)
        # Should return None, NOT raise
        path = qa_log.record(
            query="x",
            response=_FakeResponse(results=[_FakeResult()]),
            cwd=str(tmp_path),
            async_write=False,
        )
        assert path is None

    def test_no_cwd_no_write(self, tmp_path, enabled):
        path = qa_log.record(
            query="x",
            response=_FakeResponse(),
            cwd=None,
            async_write=False,
        )
        assert path is None

    def test_empty_results_still_writes(self, tmp_path, enabled):
        _mark_project(tmp_path)
        path = qa_log.record(
            query="no hits",
            response=_FakeResponse(results=[]),
            cwd=str(tmp_path),
            async_write=False,
        )
        assert path is not None
        assert "_(no results)_" in path.read_text(encoding="utf-8")

    def test_caps_result_count(self, tmp_path, enabled):
        _mark_project(tmp_path)
        many = [_FakeResult(chunk_id=f"c{i}") for i in range(50)]
        response = _FakeResponse(results=many)
        path = qa_log.record(
            query="many",
            response=response,
            cwd=str(tmp_path),
            async_write=False,
        )
        assert path is not None
        text = path.read_text(encoding="utf-8")
        # Only first _MAX_RESULTS should be serialized
        assert "c0" in text
        assert f"c{qa_log._MAX_RESULTS - 1}" in text
        assert f"c{qa_log._MAX_RESULTS}" not in text


# --- Integration: handle_hybrid_search ------------------------------------

class TestHandlerIntegration:
    """Verify the MCP handler calls qa_log.record without blocking on failures."""

    def test_handler_records_when_enabled(self, tmp_path, monkeypatch):
        from hybrid_search.tools import hybrid_search as tool

        monkeypatch.setenv(qa_log.ENV_TOGGLE, "1")

        class _FakeOrchestrator:
            class _Registry:
                def list_all(self):
                    return []
            _registry = _Registry()

            def hybrid_search(self, **_kw):
                return _FakeResponse(results=[_FakeResult()])

        result = tool.handle_hybrid_search(
            orchestrator=_FakeOrchestrator(),
            query="hello",
            cwd=str(tmp_path),
        )
        # Handler response shape is preserved
        assert "results" in result
        assert "query_type" in result

    def test_handler_survives_qa_log_crash(self, tmp_path, monkeypatch):
        from hybrid_search.tools import hybrid_search as tool

        def _boom(**_kw):
            raise RuntimeError("qa log exploded")

        monkeypatch.setattr(qa_log, "record", _boom)

        class _FakeOrchestrator:
            _registry = None

            def hybrid_search(self, **_kw):
                return _FakeResponse(results=[])

        # Even though qa_log.record raises, the response should still come back.
        result = tool.handle_hybrid_search(
            orchestrator=_FakeOrchestrator(),
            query="hello",
            cwd=str(tmp_path),
        )
        assert "results" in result
