"""Document chunking — split markdown/text by headings."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# Reuse CodeChunk structure for consistency
from hybrid_search.index.ast_chunker import CodeChunk, _make_chunk_id, _non_ws_count

LARGE_DOC_CHUNK_THRESHOLD = 4000


def chunk_doc_file(
    file_path: Path,
    project_root: Path,
    project_id: str,
    language: str,
    source: str | None = None,
) -> list[CodeChunk]:
    """Chunk a document file by headings (## level)."""
    if source is None:
        source = file_path.read_text(errors="replace")

    rel_path = str(file_path.relative_to(project_root))

    if language == "markdown":
        return _chunk_markdown(source, rel_path, project_id)
    else:
        # JSON, YAML, TOML — treat as single chunk or split by size
        return _chunk_plain(source, rel_path, project_id, language)


def _chunk_markdown(source: str, rel_path: str, project_id: str) -> list[CodeChunk]:
    """Split markdown by ## headings."""
    # Split on heading lines (## or #)
    sections: list[tuple[str, str, int]] = []  # (heading, content, start_line)
    lines = source.split("\n")
    current_heading = Path(rel_path).stem
    current_lines: list[str] = []
    current_start = 1

    for i, line in enumerate(lines):
        heading_match = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading_match:
            # Save previous section
            if current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    sections.append((current_heading, content, current_start))
            current_heading = heading_match.group(2).strip()
            current_lines = [line]
            current_start = i + 1
        else:
            current_lines.append(line)

    # Save last section
    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            sections.append((current_heading, content, current_start))

    chunks: list[CodeChunk] = []
    for heading, content, start_line in sections:
        end_line = start_line + content.count("\n")
        start_byte = source.index(content) if content in source else 0
        end_byte = start_byte + len(content.encode())

        chunk_id = _make_chunk_id(project_id, rel_path, start_byte, end_byte)
        chunks.append(CodeChunk(
            id=chunk_id,
            project_id=project_id,
            file_path=rel_path,
            language="markdown",
            node_type="section",
            name=heading,
            qualified_name=f"{rel_path}::{heading}",
            content=content,
            embedding_input=f"passage: [section] {heading} in {rel_path}\n{content}",
            start_line=start_line,
            end_line=end_line,
            start_byte=start_byte,
            end_byte=end_byte,
        ))

    return chunks if chunks else [_whole_file_chunk(source, rel_path, project_id, "markdown")]


def _chunk_plain(
    source: str,
    rel_path: str,
    project_id: str,
    language: str,
) -> list[CodeChunk]:
    """For JSON/YAML/TOML — single chunk if small, split by size if large."""
    if _non_ws_count(source) <= LARGE_DOC_CHUNK_THRESHOLD:
        return [_whole_file_chunk(source, rel_path, project_id, language)]

    # Split into ~2000 char segments
    chunks: list[CodeChunk] = []
    lines = source.split("\n")
    buffer: list[str] = []
    buffer_start = 1

    for i, line in enumerate(lines):
        buffer.append(line)
        content = "\n".join(buffer)
        if _non_ws_count(content) >= LARGE_DOC_CHUNK_THRESHOLD // 2:
            name = f"{Path(rel_path).stem}:L{buffer_start}-L{buffer_start + len(buffer)}"
            start_byte = sum(len(l.encode()) + 1 for l in lines[:buffer_start - 1])
            end_byte = start_byte + len(content.encode())
            chunk_id = _make_chunk_id(project_id, rel_path, start_byte, end_byte)
            chunks.append(CodeChunk(
                id=chunk_id,
                project_id=project_id,
                file_path=rel_path,
                language=language,
                node_type="block",
                name=name,
                qualified_name=f"{rel_path}::{name}",
                content=content,
                embedding_input=f"passage: [{language}] {name} in {rel_path}\n{content}",
                start_line=buffer_start,
                end_line=buffer_start + len(buffer),
                start_byte=start_byte,
                end_byte=end_byte,
            ))
            buffer = []
            buffer_start = i + 2

    if buffer:
        content = "\n".join(buffer)
        name = f"{Path(rel_path).stem}:L{buffer_start}-L{buffer_start + len(buffer)}"
        start_byte = sum(len(l.encode()) + 1 for l in lines[:buffer_start - 1])
        end_byte = start_byte + len(content.encode())
        chunk_id = _make_chunk_id(project_id, rel_path, start_byte, end_byte)
        chunks.append(CodeChunk(
            id=chunk_id,
            project_id=project_id,
            file_path=rel_path,
            language=language,
            node_type="block",
            name=name,
            qualified_name=f"{rel_path}::{name}",
            content=content,
            embedding_input=f"passage: [{language}] {name} in {rel_path}\n{content}",
            start_line=buffer_start,
            end_line=buffer_start + len(buffer),
            start_byte=start_byte,
            end_byte=end_byte,
        ))

    return chunks


def _whole_file_chunk(
    source: str,
    rel_path: str,
    project_id: str,
    language: str,
) -> CodeChunk:
    """Create a single chunk for the entire file."""
    name = Path(rel_path).stem
    chunk_id = _make_chunk_id(project_id, rel_path, 0, len(source.encode()))
    return CodeChunk(
        id=chunk_id,
        project_id=project_id,
        file_path=rel_path,
        language=language,
        node_type="document",
        name=name,
        qualified_name=f"{rel_path}::{name}",
        content=source,
        embedding_input=f"passage: [{language}] {name} in {rel_path}\n{source}",
        start_line=1,
        end_line=source.count("\n") + 1,
        start_byte=0,
        end_byte=len(source.encode()),
    )
