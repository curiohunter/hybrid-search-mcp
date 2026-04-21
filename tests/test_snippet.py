"""Tests for hit-aware snippet generation."""

from __future__ import annotations

from hybrid_search.search.snippet import (
    CONTEXT_LINES,
    DOCSTRING_FALLBACK_CHARS,
    SNIPPET_MAX_CHARS,
    _query_tokens,
    make_snippet,
)


class TestQueryTokens:
    def test_english_lowercased_min_three(self):
        assert _query_tokens("StoreDB upsert is fast") == ["storedb", "upsert", "fast"]

    def test_short_english_dropped(self):
        assert _query_tokens("a id of") == []

    def test_korean_min_two(self):
        assert _query_tokens("스키마 마이그레이션 가") == ["스키마", "마이그레이션"]

    def test_mixed_korean_english(self):
        toks = _query_tokens("upsert 파일 chunk")
        assert "upsert" in toks
        assert "파일" in toks
        assert "chunk" in toks

    def test_underscore_kept(self):
        assert _query_tokens("get_god_nodes") == ["get_god_nodes"]

    def test_dot_qualified_split(self):
        assert _query_tokens("StoreDB.upsert_file") == ["storedb", "upsert_file"]

    def test_empty_or_none(self):
        assert _query_tokens("") == []
        assert _query_tokens(None) == []


class TestHitCenteredSnippet:
    def _make_content(self, n: int = 30, hit_line: int = 15, hit_token: str = "TARGET"):
        lines = [f"line_{i:02d} filler" for i in range(n)]
        lines[hit_line] = f"line_{hit_line:02d} contains {hit_token} keyword"
        return "\n".join(lines)

    def test_centers_window_on_hit(self):
        content = self._make_content(hit_line=15)
        snippet = make_snippet(None, content, "TARGET")
        assert "TARGET" in snippet
        assert "line_15" in snippet
        assert "line_10" in snippet
        assert "line_20" in snippet
        assert "line_09" not in snippet
        assert "line_21" not in snippet

    def test_hit_at_start_clamps(self):
        content = self._make_content(hit_line=0)
        snippet = make_snippet(None, content, "TARGET")
        assert "line_00" in snippet
        assert "line_05" in snippet
        assert "line_06" not in snippet

    def test_hit_at_end_clamps(self):
        content = self._make_content(n=20, hit_line=19)
        snippet = make_snippet(None, content, "TARGET")
        assert "line_19" in snippet
        assert "line_14" in snippet

    def test_no_hit_falls_back_to_docstring(self):
        content = self._make_content(hit_line=15, hit_token="OTHER")
        docstring = "Docstring describing function."
        snippet = make_snippet(docstring, content, "TARGET")
        assert snippet == docstring

    def test_no_hit_no_docstring_falls_back_to_head(self):
        content = self._make_content(hit_line=15, hit_token="OTHER")
        snippet = make_snippet(None, content, "TARGET")
        assert "line_00" in snippet
        assert "line_15" not in snippet

    def test_korean_query_hits(self):
        content = "def foo():\n    # 한국어 주석 매칭 테스트\n    pass\n" + "\n".join(
            f"line {i}" for i in range(20)
        )
        snippet = make_snippet(None, content, "한국어")
        assert "한국어" in snippet

    def test_case_insensitive(self):
        content = "alpha\nbeta\nGamma TARGETword\ndelta\nepsilon"
        snippet = make_snippet(None, content, "targetword")
        assert "Gamma TARGETword" in snippet


class TestLengthCap:
    def test_hit_window_capped(self):
        long_line = "x" * 1000
        content = "\n".join([long_line, "TARGET", long_line])
        snippet = make_snippet(None, content, "TARGET")
        assert len(snippet) <= SNIPPET_MAX_CHARS

    def test_docstring_capped(self):
        snippet = make_snippet("y" * 5000, None, "anything")
        assert len(snippet) == DOCSTRING_FALLBACK_CHARS

    def test_head_fallback_capped(self):
        content = "\n".join("z" * 100 for _ in range(50))
        snippet = make_snippet(None, content, None)
        assert len(snippet) <= SNIPPET_MAX_CHARS


class TestFallbacks:
    def test_no_query_uses_docstring(self):
        snippet = make_snippet("hello docstring", "body line\nmore", None)
        assert snippet == "hello docstring"

    def test_no_query_no_docstring_uses_head(self):
        content = "\n".join(f"line_{i}" for i in range(20))
        snippet = make_snippet(None, content, None)
        assert "line_0" in snippet
        assert "line_15" not in snippet

    def test_empty_everything(self):
        assert make_snippet(None, None, None) == ""
        assert make_snippet(None, "", "query") == ""

    def test_all_short_query_falls_back(self):
        content = "alpha\nbeta\nTARGET line\ngamma"
        snippet = make_snippet("doc", content, "is of a")
        assert snippet == "doc"

    def test_context_lines_constant_sane(self):
        assert CONTEXT_LINES >= 3
        assert SNIPPET_MAX_CHARS >= 200
