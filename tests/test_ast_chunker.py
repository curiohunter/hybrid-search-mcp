"""Tests for AST-based code chunking — index/ast_chunker.py."""

from pathlib import Path

from hybrid_search.index.ast_chunker import (
    CHUNK_NODE_TYPES,
    CLASS_NODE_TYPES,
    LARGE_CHUNK_THRESHOLD,
    SMALL_CHUNK_THRESHOLD,
    CodeChunk,
    chunk_code_file,
    _classify_node_type,
    _fallback_chunking,
    _make_chunk_id,
    _non_ws_count,
)


PROJECT_ID = "test-project"
PROJECT_ROOT = Path("/fake/root")


class TestChunkCodeFilePython:
    """chunk_code_file() with Python source."""

    PYTHON_SOURCE = '''\
import os
from pathlib import Path


def hello(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}!"


class Calculator:
    """A simple calculator."""

    def add(self, a: int, b: int) -> int:
        return a + b

    def subtract(self, a: int, b: int) -> int:
        return a - b
'''

    def _chunk(self, source: str | None = None) -> list[CodeChunk]:
        src = source if source is not None else self.PYTHON_SOURCE
        fp = PROJECT_ROOT / "example.py"
        return chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, "python", source=src)

    def test_extracts_function_and_class(self) -> None:
        chunks = self._chunk()
        # Small chunks may be merged; check content instead of exact names
        all_content = " ".join(c.content for c in chunks)
        assert "def hello" in all_content
        assert "class Calculator" in all_content

    def test_chunk_has_correct_language(self) -> None:
        chunks = self._chunk()
        assert all(c.language == "python" for c in chunks)

    def test_chunk_has_file_path(self) -> None:
        chunks = self._chunk()
        assert all(c.file_path == "example.py" for c in chunks)

    def test_chunk_has_project_id(self) -> None:
        chunks = self._chunk()
        assert all(c.project_id == PROJECT_ID for c in chunks)

    def test_chunk_has_content(self) -> None:
        chunks = self._chunk()
        # Chunks may be merged; find the chunk containing hello
        hello_chunk = next(c for c in chunks if "def hello" in c.content)
        assert "Hello, {name}!" in hello_chunk.content

    def test_chunk_has_embedding_input(self) -> None:
        chunks = self._chunk()
        for chunk in chunks:
            assert chunk.embedding_input
            assert "passage:" in chunk.embedding_input

    def test_imports_extracted(self) -> None:
        chunks = self._chunk()
        # All chunks share the same imports from the file
        for chunk in chunks:
            assert any("os" in imp for imp in chunk.imports)

    def test_docstring_extracted(self) -> None:
        # Use a large-enough function to avoid merging
        body = "\n".join(f"    line_{i} = {i}" for i in range(60))
        source = f'def hello(name: str) -> str:\n    """Greet someone."""\n{body}\n'
        chunks = self._chunk(source=source)
        hello_chunk = next(c for c in chunks if "def hello" in c.content)
        assert hello_chunk.docstring == "Greet someone."

    def test_qualified_name_includes_file(self) -> None:
        chunks = self._chunk()
        assert all("example.py" in c.qualified_name for c in chunks)

    def test_line_numbers_set(self) -> None:
        chunks = self._chunk()
        for chunk in chunks:
            assert chunk.start_line > 0
            assert chunk.end_line >= chunk.start_line

    def test_empty_file(self) -> None:
        chunks = self._chunk(source="")
        assert chunks == []

    def test_no_functions_or_classes(self) -> None:
        chunks = self._chunk(source="x = 1\ny = 2\n")
        # Should fallback or produce no AST chunks
        # Python top-level assignments are not in CHUNK_NODE_TYPES
        # Fallback chunking may produce something
        assert isinstance(chunks, list)


class TestChunkCodeFileTypeScript:
    """chunk_code_file() with TypeScript source."""

    TS_SOURCE = '''\
import { useState } from 'react';

export function greet(name: string): string {
  return `Hello, ${name}!`;
}

export class UserService {
  private users: string[] = [];

  addUser(name: string): void {
    this.users.push(name);
  }

  getUsers(): string[] {
    return this.users;
  }
}

interface Config {
  apiUrl: string;
  timeout: number;
}
'''

    def _chunk(self) -> list[CodeChunk]:
        fp = PROJECT_ROOT / "service.ts"
        return chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, "typescript", source=self.TS_SOURCE)

    def test_extracts_function(self) -> None:
        chunks = self._chunk()
        all_content = " ".join(c.content for c in chunks)
        assert "function greet" in all_content

    def test_extracts_class(self) -> None:
        chunks = self._chunk()
        all_content = " ".join(c.content for c in chunks)
        assert "class UserService" in all_content

    def test_extracts_interface(self) -> None:
        chunks = self._chunk()
        all_content = " ".join(c.content for c in chunks)
        assert "interface Config" in all_content

    def test_imports_from_react(self) -> None:
        chunks = self._chunk()
        for chunk in chunks:
            assert any("react" in imp for imp in chunk.imports)


class TestChunkCodeFileMultiLang:
    """chunk_code_file() works for Phase 3b languages."""

    def _chunk(self, language: str, source: str, filename: str) -> list[CodeChunk]:
        fp = PROJECT_ROOT / filename
        return chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, language, source=source)

    def test_rust(self) -> None:
        source = "fn main() {\n    println!(\"hello\");\n}\n"
        chunks = self._chunk("rust", source, "main.rs")
        assert any(c.name == "main" for c in chunks)

    def test_go(self) -> None:
        source = "package main\n\nfunc hello() {\n}\n"
        chunks = self._chunk("go", source, "main.go")
        assert any(c.name == "hello" for c in chunks)

    def test_java(self) -> None:
        source = "public class Hello {\n    public void greet() {}\n}\n"
        chunks = self._chunk("java", source, "Hello.java")
        all_content = " ".join(c.content for c in chunks)
        assert "class Hello" in all_content

    def test_c(self) -> None:
        source = "int add(int a, int b) {\n    return a + b;\n}\n"
        chunks = self._chunk("c", source, "math.c")
        assert any(c.name == "add" for c in chunks)

    def test_ruby(self) -> None:
        source = "class Dog\n  def bark\n    puts 'woof'\n  end\nend\n"
        chunks = self._chunk("ruby", source, "dog.rb")
        all_content = " ".join(c.content for c in chunks)
        assert "class Dog" in all_content

    def test_unsupported_language_uses_fallback(self) -> None:
        source = "<html>\n<body>\n<p>Hello</p>\n</body>\n</html>\n"
        chunks = self._chunk("html", source, "index.html")
        # Should use fallback chunking, not crash
        assert isinstance(chunks, list)


class TestRationaleExtraction:
    """M10: NOTE / WHY / TODO / FIXME / HACK / XXX comments get folded into docstring."""

    def _make_chunks(self, source: str, language: str = "python", suffix: str = ".py") -> list[CodeChunk]:
        fp = PROJECT_ROOT / f"rationale_example{suffix}"
        return chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, language, source=source)

    def test_python_note_comment_appended_to_docstring(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(60))  # avoid merging
        source = (
            'def handler():\n'
            '    """Primary request handler."""\n'
            '    # NOTE: retry on 429, legal requirement\n'
            f'{body}\n'
        )
        chunks = self._make_chunks(source)
        chunk = next(c for c in chunks if "def handler" in c.content)
        assert chunk.docstring is not None
        assert "Primary request handler." in chunk.docstring
        assert "NOTE: retry on 429, legal requirement" in chunk.docstring

    def test_python_rationale_without_docstring(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(60))
        source = (
            'def handler():\n'
            '    # WHY: legacy API contract, do not change return type\n'
            f'{body}\n'
        )
        chunks = self._make_chunks(source)
        chunk = next(c for c in chunks if "def handler" in c.content)
        assert chunk.docstring == "WHY: legacy API contract, do not change return type"

    def test_python_multiple_tags_deduplicated_and_ordered(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(60))
        source = (
            'def handler():\n'
            '    # TODO: add timeout\n'
            '    # NOTE: must be idempotent\n'
            '    # TODO: add timeout\n'  # duplicate
            f'{body}\n'
        )
        chunks = self._make_chunks(source)
        chunk = next(c for c in chunks if "def handler" in c.content)
        assert chunk.docstring is not None
        # Each tag kept once, in first-seen order
        lines = [l for l in chunk.docstring.splitlines() if l]
        assert lines.count("TODO: add timeout") == 1
        assert lines.count("NOTE: must be idempotent") == 1
        assert lines.index("TODO: add timeout") < lines.index("NOTE: must be idempotent")

    def test_python_no_rationale_keeps_docstring_only(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(60))
        source = (
            'def handler():\n'
            '    """Just a plain function."""\n'
            f'{body}\n'
        )
        chunks = self._make_chunks(source)
        chunk = next(c for c in chunks if "def handler" in c.content)
        assert chunk.docstring == "Just a plain function."

    def test_python_plain_comment_ignored(self) -> None:
        """Comments without NOTE/WHY/TODO tags should not be included."""
        body = "\n".join(f"    x_{i} = {i}" for i in range(60))
        source = (
            'def handler():\n'
            '    # just a regular comment\n'
            '    # NOTE: the real rationale\n'
            f'{body}\n'
        )
        chunks = self._make_chunks(source)
        chunk = next(c for c in chunks if "def handler" in c.content)
        assert "just a regular comment" not in (chunk.docstring or "")
        assert "NOTE: the real rationale" in (chunk.docstring or "")

    def test_python_case_insensitive_tag(self) -> None:
        body = "\n".join(f"    x_{i} = {i}" for i in range(60))
        source = (
            'def handler():\n'
            '    # note: lowercase tag still captured\n'
            f'{body}\n'
        )
        chunks = self._make_chunks(source)
        chunk = next(c for c in chunks if "def handler" in c.content)
        # Tag normalized to uppercase
        assert chunk.docstring == "NOTE: lowercase tag still captured"

    def test_typescript_note_comment(self) -> None:
        body = "\n".join(f"  const x_{i} = {i};" for i in range(60))
        source = (
            'export function handler(): void {\n'
            '  // NOTE: browser-specific quirk\n'
            f'{body}\n'
            '}\n'
        )
        chunks = self._make_chunks(source, language="typescript", suffix=".ts")
        chunk = next(c for c in chunks if "handler" in c.content)
        assert chunk.docstring is not None
        assert "NOTE: browser-specific quirk" in chunk.docstring

    def test_jsdoc_remarks_captured(self) -> None:
        body = "\n".join(f"  const x_{i} = {i};" for i in range(60))
        source = (
            '/**\n'
            ' * Do the thing.\n'
            ' * @remarks This runs under the legacy auth boundary.\n'
            ' */\n'
            'export function doit(): void {\n'
            f'{body}\n'
            '}\n'
        )
        chunks = self._make_chunks(source, language="typescript", suffix=".ts")
        chunk = next(c for c in chunks if "doit" in c.content)
        assert chunk.docstring is not None
        assert "REMARKS" in chunk.docstring.upper()
        assert "legacy auth boundary" in chunk.docstring


class TestMultiByteHandling:
    """Verify byte offset handling for multi-byte characters (Korean, emoji)."""

    def test_korean_in_python(self) -> None:
        source = 'def 안녕():\n    """한국어 독스트링"""\n    return "안녕하세요"\n'
        fp = PROJECT_ROOT / "korean.py"
        chunks = chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, "python", source=source)
        assert len(chunks) >= 1
        chunk = chunks[0]
        # Content should be correctly extracted (not garbled)
        assert "안녕" in chunk.content
        assert "안녕하세요" in chunk.content


class TestLargeChunkSplitting:
    """Large chunks (>4000 non-ws chars) get split."""

    def test_large_function_gets_split(self) -> None:
        # Create a Python function with >4000 non-whitespace chars
        body_lines = [f"    x_{i} = {i}" for i in range(500)]
        source = "def big_function():\n" + "\n".join(body_lines) + "\n"
        fp = PROJECT_ROOT / "big.py"
        chunks = chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, "python", source=source)
        # Should produce multiple chunks from the split
        assert len(chunks) > 1


class TestSmallChunkMerging:
    """Adjacent small chunks (<500 non-ws chars) get merged."""

    def test_tiny_functions_merge(self) -> None:
        # Multiple tiny functions
        funcs = [f"def f{i}():\n    return {i}\n\n" for i in range(5)]
        source = "\n".join(funcs)
        fp = PROJECT_ROOT / "tiny.py"
        chunks = chunk_code_file(fp, PROJECT_ROOT, PROJECT_ID, "python", source=source)
        # 5 tiny functions each <500 chars should merge into fewer chunks
        assert len(chunks) < 5


class TestFallbackChunking:
    """_fallback_chunking() for unsupported languages."""

    def test_splits_on_blank_lines(self) -> None:
        source = "block one\nline two\n\nblock two\nline four\n"
        chunks = _fallback_chunking(source, "file.txt", PROJECT_ID, "text")
        assert len(chunks) >= 1

    def test_empty_source(self) -> None:
        chunks = _fallback_chunking("", "file.txt", PROJECT_ID, "text")
        assert chunks == []


class TestHelpers:
    """Helper function tests."""

    def test_make_chunk_id_deterministic(self) -> None:
        id1 = _make_chunk_id("proj", "file.py", 0, 100)
        id2 = _make_chunk_id("proj", "file.py", 0, 100)
        assert id1 == id2

    def test_make_chunk_id_different_inputs(self) -> None:
        id1 = _make_chunk_id("proj", "file.py", 0, 100)
        id2 = _make_chunk_id("proj", "file.py", 0, 200)
        assert id1 != id2

    def test_non_ws_count(self) -> None:
        assert _non_ws_count("hello world") == 10
        assert _non_ws_count("  \t\n  ") == 0
        assert _non_ws_count("") == 0
        assert _non_ws_count("abc") == 3

    def test_chunk_node_types_coverage(self) -> None:
        """All expected languages are in CHUNK_NODE_TYPES."""
        expected = {
            "typescript", "javascript", "python", "rust", "go",
            "ruby", "java", "c", "cpp", "swift", "kotlin", "css", "scss", "sql",
        }
        assert set(CHUNK_NODE_TYPES.keys()) == expected

    def test_class_node_types_not_empty(self) -> None:
        assert len(CLASS_NODE_TYPES) > 0
