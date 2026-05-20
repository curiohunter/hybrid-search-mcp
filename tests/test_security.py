"""Q4 — security sanitizers + MCP trust-boundary integration."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hybrid_search.search.orchestrator import HybridResult, HybridSearchResponse
from hybrid_search.security import (
    clamp_float,
    clamp_int,
    sanitize_cwd,
    sanitize_file_pattern,
    sanitize_node_types,
    sanitize_query,
    sanitize_snippet,
    validate_project_name,
    validate_project_path,
)
from hybrid_search.tools.hybrid_search import handle_hybrid_search


# ---------------------------------------------------------------------------
# sanitize_query
# ---------------------------------------------------------------------------


class TestSanitizeQuery:
    def test_strips_control_chars(self) -> None:
        # NUL, BEL, ESC, DEL — must disappear
        assert sanitize_query("he\x00ll\x07o\x1b\x7f") == "hello"

    def test_preserves_tabs_newlines_cr(self) -> None:
        """\\t \\n \\r survive — common in multi-line code searches."""
        text = "line1\nline2\r\n\tindented"
        assert sanitize_query(text) == text

    def test_preserves_unicode(self) -> None:
        assert sanitize_query("검색 쿼리 한국어") == "검색 쿼리 한국어"

    def test_length_cap(self) -> None:
        assert sanitize_query("x" * 3000, max_len=100) == "x" * 100

    def test_type_error_on_non_str(self) -> None:
        with pytest.raises(TypeError):
            sanitize_query(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# sanitize_snippet
# ---------------------------------------------------------------------------


class TestSanitizeSnippet:
    def test_none_becomes_empty(self) -> None:
        assert sanitize_snippet(None) == ""

    def test_strips_ansi_escape_bytes(self) -> None:
        """ESC (0x1b) must be removed so downstream terminals don't interpret."""
        assert sanitize_snippet("clean\x1b[31mred\x1b[0m") == "clean[31mred[0m"

    def test_non_str_coerced(self) -> None:
        assert sanitize_snippet(42) == "42"

    def test_length_cap(self) -> None:
        assert sanitize_snippet("a" * 20000, max_len=128) == "a" * 128


# ---------------------------------------------------------------------------
# sanitize_file_pattern / sanitize_node_types / sanitize_cwd
# ---------------------------------------------------------------------------


class TestSanitizeFilePattern:
    def test_none_passthrough(self) -> None:
        assert sanitize_file_pattern(None) is None

    def test_control_chars_stripped(self) -> None:
        assert sanitize_file_pattern("src/**/*.\x00ts") == "src/**/*.ts"

    def test_length_cap(self) -> None:
        assert sanitize_file_pattern("a" * 500, max_len=10) == "a" * 10

    def test_type_error(self) -> None:
        with pytest.raises(TypeError):
            sanitize_file_pattern(42)  # type: ignore[arg-type]


class TestSanitizeNodeTypes:
    def test_none_passthrough(self) -> None:
        assert sanitize_node_types(None) is None

    def test_drops_non_str_entries(self) -> None:
        assert sanitize_node_types(["function", 42, None, "class"]) == ["function", "class"]

    def test_strips_and_trims(self) -> None:
        assert sanitize_node_types(["  \x00method  "]) == ["method"]

    def test_drops_empty_after_clean(self) -> None:
        assert sanitize_node_types(["\x00", "  ", "class"]) == ["class"]

    def test_max_items(self) -> None:
        out = sanitize_node_types(["x"] * 100, max_items=5)
        assert out == ["x"] * 5

    def test_type_error_on_non_list(self) -> None:
        with pytest.raises(TypeError):
            sanitize_node_types("not-a-list")  # type: ignore[arg-type]


class TestSanitizeCwd:
    def test_none_passthrough(self) -> None:
        assert sanitize_cwd(None) is None

    def test_strips_control(self) -> None:
        assert sanitize_cwd("/repo/src\x00") == "/repo/src"


# ---------------------------------------------------------------------------
# validate_project_name
# ---------------------------------------------------------------------------


class TestValidateProjectName:
    def test_none_passthrough(self) -> None:
        assert validate_project_name(None) is None

    @pytest.mark.parametrize("name", [
        "hybrid-search-mcp",
        "proj_1.2",
        "A",
        "x" * 64,
    ])
    def test_accepts_valid(self, name: str) -> None:
        assert validate_project_name(name) == name

    @pytest.mark.parametrize("name", [
        "",                              # empty
        ".hidden",                       # leading dot
        "has/slash",                     # path separator
        "has\\backslash",
        "has space",
        "has\x00null",
        "x" * 65,                        # too long
        "../escape",
    ])
    def test_rejects_invalid(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_project_name(name)


# ---------------------------------------------------------------------------
# validate_project_path
# ---------------------------------------------------------------------------


class TestValidateProjectPath:
    def test_accepts_relative_inside(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "file.py").write_text("x")
        resolved = validate_project_path("sub/file.py", tmp_path)
        assert resolved == (tmp_path / "sub" / "file.py").resolve()

    def test_rejects_parent_escape(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            validate_project_path("../../etc/passwd", tmp_path)

    def test_rejects_absolute_outside(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            validate_project_path("/etc/passwd", tmp_path)


# ---------------------------------------------------------------------------
# clamp_int / clamp_float
# ---------------------------------------------------------------------------


class TestClampInt:
    def test_in_range(self) -> None:
        assert clamp_int(5, 1, 10) == 5

    def test_clamps_low(self) -> None:
        assert clamp_int(-3, 1, 10) == 1

    def test_clamps_high(self) -> None:
        assert clamp_int(999, 1, 10) == 10

    def test_rejects_bool(self) -> None:
        with pytest.raises(TypeError):
            clamp_int(True, 0, 10)  # type: ignore[arg-type]

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            clamp_int(3.14, 0, 10)  # type: ignore[arg-type]


class TestClampFloat:
    def test_in_range(self) -> None:
        assert clamp_float(0.5, 0.0, 1.0) == 0.5

    def test_int_coerced(self) -> None:
        assert clamp_float(1, 0.0, 1.0) == 1.0

    def test_clamps_low_and_high(self) -> None:
        assert clamp_float(-0.3, 0.0, 1.0) == 0.0
        assert clamp_float(2.5, 0.0, 1.0) == 1.0

    def test_rejects_nan(self) -> None:
        with pytest.raises(ValueError):
            clamp_float(math.nan, 0.0, 1.0)

    def test_rejects_bool(self) -> None:
        with pytest.raises(TypeError):
            clamp_float(True, 0.0, 1.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# handle_hybrid_search — integration: inputs sanitized, outputs sanitized
# ---------------------------------------------------------------------------


def _mock_response(
    content: str = "def foo():\n    pass",
    snippet: str = "def foo",
) -> HybridSearchResponse:
    return HybridSearchResponse(
        results=[
            HybridResult(
                chunk_id="c1",
                rrf_score=0.9,
                bm25_rank=1,
                vector_rank=2,
                file_path="src/foo.py",
                project="demo",
                name="foo",
                qualified_name="demo.foo",
                node_type="function",
                start_line=1,
                end_line=2,
                content=content,
                snippet=snippet,
            ),
        ],
        query_type="ENGLISH_NL",
        effective_bm25_weight=0.4,
        query_time_ms=12.5,
        total_chunks_searched=100,
        skipped_projects=[],
        reranked=False,
    )


class TestHandleHybridSearchIntegration:
    def test_sanitizes_query_before_orchestrator(self) -> None:
        orch = MagicMock()
        orch.hybrid_search.return_value = _mock_response()
        handle_hybrid_search(orch, query="hello\x00world\x07")
        kwargs = orch.hybrid_search.call_args.kwargs
        assert kwargs["query"] == "helloworld"

    def test_clamps_limit_above_max(self) -> None:
        orch = MagicMock()
        orch.hybrid_search.return_value = _mock_response()
        handle_hybrid_search(orch, query="x", limit=500)
        assert orch.hybrid_search.call_args.kwargs["limit"] == 50

    def test_clamps_bm25_weight(self) -> None:
        orch = MagicMock()
        orch.hybrid_search.return_value = _mock_response()
        handle_hybrid_search(orch, query="x", bm25_weight=2.5)
        assert orch.hybrid_search.call_args.kwargs["bm25_weight"] == 1.0

    def test_rejects_invalid_project_name(self) -> None:
        orch = MagicMock()
        with pytest.raises(ValueError):
            handle_hybrid_search(orch, query="x", project="../etc")
        orch.hybrid_search.assert_not_called()

    def test_drops_non_str_node_type_entries(self) -> None:
        orch = MagicMock()
        orch.hybrid_search.return_value = _mock_response()
        handle_hybrid_search(orch, query="x", node_types=["function", 1, "class"])  # type: ignore[list-item]
        assert orch.hybrid_search.call_args.kwargs["node_types"] == ["function", "class"]

    def test_sanitizes_content_and_snippet_in_output(self) -> None:
        orch = MagicMock()
        orch.hybrid_search.return_value = _mock_response(
            content="def foo():\x1b[31m\n    pass\x00",
            snippet="def\x07 foo",
        )
        out = handle_hybrid_search(orch, query="x")
        r = out["results"][0]
        assert "\x00" not in r["content"]
        assert "\x1b" not in r["content"]
        assert "\x07" not in r["snippet"]

    def test_preserves_in_flight_metadata_in_output(self) -> None:
        orch = MagicMock()
        resp = _mock_response(content="dirty", snippet="[in-flight] src/foo.py\ndirty")
        resp.results[0].node_type = "in_flight_file"
        resp.results[0].trust_meta = "[in-flight dirty worktree; not indexed]"
        orch.hybrid_search.return_value = resp

        out = handle_hybrid_search(orch, query="x")
        r = out["results"][0]

        assert r["node_type"] == "in_flight_file"
        assert r["trust_meta"] == "[in-flight dirty worktree; not indexed]"

    def test_rerank_hint_uses_sanitized_query(self) -> None:
        orch = MagicMock()
        resp = _mock_response()
        # Force the rerank path: len(results) > limit
        resp.results = resp.results * 3
        resp.reranked = True
        orch.hybrid_search.return_value = resp
        out = handle_hybrid_search(orch, query="he\x00llo", limit=1)
        assert "rerank_hint" in out
        assert "\x00" not in out["rerank_hint"]
        assert '"hello"' in out["rerank_hint"]
