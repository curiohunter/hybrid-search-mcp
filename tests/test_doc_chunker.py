"""Tests for document chunking — index/doc_chunker.py."""

from pathlib import Path

from hybrid_search.index.ast_chunker import CodeChunk
from hybrid_search.index.doc_chunker import (
    LARGE_DOC_CHUNK_THRESHOLD,
    chunk_doc_file,
)


PROJECT_ID = "test-project"
PROJECT_ROOT = Path("/fake/root")


class TestMarkdownChunking:
    """Markdown files split by headings."""

    def _chunk(self, source: str, filename: str = "readme.md") -> list[CodeChunk]:
        fp = PROJECT_ROOT / filename
        return chunk_doc_file(fp, PROJECT_ROOT, PROJECT_ID, "markdown", source=source)

    def test_splits_on_headings(self) -> None:
        source = "# Title\n\nIntro text.\n\n## Section A\n\nContent A.\n\n## Section B\n\nContent B.\n"
        chunks = self._chunk(source)
        names = [c.name for c in chunks]
        assert "Title" in names
        assert "Section A" in names
        assert "Section B" in names

    def test_heading_levels(self) -> None:
        source = "### Deep Heading\n\nContent here.\n"
        chunks = self._chunk(source)
        assert any(c.name == "Deep Heading" for c in chunks)

    def test_single_heading(self) -> None:
        source = "## Only Section\n\nSome content.\n"
        chunks = self._chunk(source)
        assert len(chunks) == 1
        assert chunks[0].name == "Only Section"

    def test_no_headings_falls_back_to_whole_file(self) -> None:
        source = "Just some text without any headings.\nMore text.\n"
        chunks = self._chunk(source)
        assert len(chunks) >= 1
        # Without headings, the file stem is the name
        assert chunks[0].node_type in ("section", "document")

    def test_empty_markdown(self) -> None:
        chunks = self._chunk("")
        # Empty source → whole file chunk
        assert len(chunks) == 1
        assert chunks[0].node_type == "document"

    def test_chunk_metadata(self) -> None:
        source = "## My Section\n\nHello world.\n"
        chunks = self._chunk(source)
        chunk = chunks[0]
        assert chunk.language == "markdown"
        assert chunk.project_id == PROJECT_ID
        assert chunk.file_path == "readme.md"
        assert chunk.node_type == "section"
        assert "passage:" in chunk.embedding_input

    def test_content_before_first_heading(self) -> None:
        source = "Preamble text.\n\n## First Section\n\nContent.\n"
        chunks = self._chunk(source)
        # Preamble should be captured as a section with file stem name
        assert len(chunks) >= 2


class TestPlainDocChunking:
    """JSON/YAML/TOML files — single chunk or size-based split."""

    def _chunk(self, source: str, language: str, filename: str) -> list[CodeChunk]:
        fp = PROJECT_ROOT / filename
        return chunk_doc_file(fp, PROJECT_ROOT, PROJECT_ID, language, source=source)

    def test_small_json_single_chunk(self) -> None:
        source = '{"key": "value", "num": 42}'
        chunks = self._chunk(source, "json", "config.json")
        assert len(chunks) == 1
        assert chunks[0].node_type == "document"
        assert chunks[0].name == "config"

    def test_small_yaml_single_chunk(self) -> None:
        source = "key: value\nnum: 42\n"
        chunks = self._chunk(source, "yaml", "config.yaml")
        assert len(chunks) == 1

    def test_large_json_splits(self) -> None:
        # Create JSON large enough to split (>4000 non-ws chars)
        lines = [f'  "key_{i}": "{"x" * 50}"' for i in range(200)]
        source = "{\n" + ",\n".join(lines) + "\n}"
        chunks = self._chunk(source, "json", "big.json")
        assert len(chunks) > 1

    def test_toml_single_chunk(self) -> None:
        source = "[section]\nkey = 'value'\n"
        chunks = self._chunk(source, "toml", "config.toml")
        assert len(chunks) == 1

    def test_qualified_name_format(self) -> None:
        source = '{"a": 1}'
        chunks = self._chunk(source, "json", "data.json")
        assert chunks[0].qualified_name == "data.json::data"


class TestQALogChunking:
    """Memory Layer — .hybrid-search/qa/** files get one whole-file chunk
    tagged node_type=qa_log instead of being split on ## headings."""

    def _chunk(self, source: str, rel: str) -> list[CodeChunk]:
        return chunk_doc_file(
            PROJECT_ROOT / rel, PROJECT_ROOT, PROJECT_ID, "markdown", source=source
        )

    def test_qa_log_path_gets_single_chunk(self) -> None:
        # Writer emits multiple ## headings per entry ("Top results" etc.) —
        # they must *not* be split apart so the query and hits stay together.
        source = (
            "---\nquery: \"hello\"\n---\n\n# Q: hello\n\n"
            "## Top results\n\n### 1. `a.py`\n- chunk_id: `c1`\n\n> hi\n"
        )
        chunks = self._chunk(source, ".hybrid-search/qa/2026/04/21-000000-deadbeef.md")
        assert len(chunks) == 1

    def test_qa_log_node_type(self) -> None:
        chunks = self._chunk(
            "---\nquery: \"x\"\n---\n\n# Q: x\n",
            ".hybrid-search/qa/2026/04/21-111111-aaaabbbb.md",
        )
        assert chunks[0].node_type == "qa_log"

    def test_qa_log_embedding_input_mentions_tag(self) -> None:
        chunks = self._chunk(
            "---\nquery: \"y\"\n---\n\n# Q: y\n",
            ".hybrid-search/qa/2026/04/21-222222-ccccdddd.md",
        )
        assert "[qa_log]" in chunks[0].embedding_input

    def test_non_qa_markdown_still_splits(self) -> None:
        # Regression: only .hybrid-search/qa/ triggers the bypass.
        source = "# Title\n\nIntro.\n\n## Section\n\nBody.\n"
        chunks = self._chunk(source, "docs/normal.md")
        assert any(c.node_type == "section" for c in chunks)
        assert all(c.node_type != "qa_log" for c in chunks)
