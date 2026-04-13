"""AST-based code chunking using TreeSitter.

Supports TypeScript/JavaScript/Python (Phase 1).
Falls back to blank-line chunking for unsupported languages.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter as ts

logger = logging.getLogger(__name__)

# Non-whitespace character threshold for large chunk splitting (cAST paper)
LARGE_CHUNK_THRESHOLD = 4000
LARGE_CHUNK_SPLIT_SIZE = 2000
LARGE_CHUNK_OVERLAP = 500

# Small chunk merge threshold
SMALL_CHUNK_THRESHOLD = 500

# TreeSitter node types to extract per language
CHUNK_NODE_TYPES: dict[str, set[str]] = {
    "typescript": {
        "function_declaration",
        "method_definition",
        "arrow_function",
        "class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "export_statement",
        "lexical_declaration",  # const/let at top level
    },
    "javascript": {
        "function_declaration",
        "method_definition",
        "arrow_function",
        "class_declaration",
        "export_statement",
        "lexical_declaration",
    },
    "python": {
        "function_definition",
        "class_definition",
        "decorated_definition",
    },
}

# Node types that represent "class-like" containers
CLASS_NODE_TYPES = {
    "class_declaration",
    "class_definition",
    "interface_declaration",
}


@dataclass
class CodeChunk:
    id: str
    project_id: str
    file_path: str
    language: str
    node_type: str
    name: str
    qualified_name: str
    content: str
    embedding_input: str
    imports: list[str] = field(default_factory=list)
    docstring: str | None = None
    start_line: int = 0
    end_line: int = 0
    start_byte: int = 0
    end_byte: int = 0
    parent_name: str | None = None
    calls: list[str] = field(default_factory=list)


def chunk_code_file(
    file_path: Path,
    project_root: Path,
    project_id: str,
    language: str,
    source: str | None = None,
) -> list[CodeChunk]:
    """Chunk a code file using TreeSitter AST. Falls back to blank-line chunking."""
    if source is None:
        source = file_path.read_text(errors="replace")

    rel_path = str(file_path.relative_to(project_root))
    ts_lang_obj = _get_ts_language(language)

    if ts_lang_obj is None:
        return _fallback_chunking(source, rel_path, project_id, language)

    try:
        parser = ts.Parser(ts_lang_obj)
        tree = parser.parse(source.encode())
    except Exception:
        logger.warning("TreeSitter parse failed for %s, using fallback", rel_path)
        return _fallback_chunking(source, rel_path, project_id, language)

    node_types = CHUNK_NODE_TYPES.get(language, set())
    imports = _extract_imports(tree.root_node, language, source)
    raw_chunks = _extract_chunks(tree.root_node, node_types, source, rel_path, project_id, language)

    # Post-process: split large, merge small
    processed = _split_large_chunks(raw_chunks, source, rel_path, project_id, language)
    processed = _merge_small_chunks(processed, rel_path, project_id, language)

    # Build embedding input for each chunk
    for chunk in processed:
        chunk.imports = imports
        chunk.embedding_input = _build_embedding_input(chunk)

    return processed


def _get_ts_language(language: str) -> ts.Language | None:
    """Get tree-sitter Language object for supported languages."""
    try:
        if language == "typescript":
            import tree_sitter_typescript
            return ts.Language(tree_sitter_typescript.language_typescript())
        elif language == "javascript":
            import tree_sitter_javascript
            return ts.Language(tree_sitter_javascript.language())
        elif language == "python":
            import tree_sitter_python
            return ts.Language(tree_sitter_python.language())
    except ImportError:
        pass
    return None


def _extract_imports(root_node, ts_lang: str, source: str) -> list[str]:
    """Extract import paths from the AST root."""
    imports: list[str] = []
    for child in root_node.children:
        if ts_lang in ("typescript", "javascript"):
            if child.type == "import_statement":
                # Extract the module path from import string
                for desc in _iter_descendants(child):
                    if desc.type == "string":
                        text = source[desc.start_byte:desc.end_byte].strip("'\"")
                        imports.append(text)
        elif ts_lang == "python":
            if child.type in ("import_statement", "import_from_statement"):
                text = source[child.start_byte:child.end_byte]
                imports.append(text)
    return imports


def _extract_chunks(
    root_node,
    node_types: set[str],
    source: str,
    rel_path: str,
    project_id: str,
    language: str,
) -> list[CodeChunk]:
    """Walk AST and extract chunks for matching node types."""
    chunks: list[CodeChunk] = []
    _walk_node(root_node, node_types, source, rel_path, project_id, language, None, chunks)
    return chunks


def _walk_node(
    node,
    node_types: set[str],
    source: str,
    rel_path: str,
    project_id: str,
    language: str,
    parent_name: str | None,
    results: list[CodeChunk],
) -> None:
    """Recursively walk AST nodes, extracting chunks."""
    if node.type in node_types:
        name = _extract_name(node, source, language)
        node_type = _classify_node_type(node, language)
        content = source[node.start_byte:node.end_byte]
        docstring = _extract_docstring(node, source, language)
        calls = _extract_calls(node, source, language)

        # For classes: extract the header, then recurse into methods
        if node.type in CLASS_NODE_TYPES:
            # Create chunk for class header (without method bodies)
            header_content = _extract_class_header(node, source, language)
            chunk_id = _make_chunk_id(project_id, rel_path, node.start_byte, node.end_byte)
            results.append(CodeChunk(
                id=chunk_id,
                project_id=project_id,
                file_path=rel_path,
                language=language,
                node_type="class",
                name=name,
                qualified_name=f"{rel_path}::{name}",
                content=header_content,
                embedding_input="",
                docstring=docstring,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                parent_name=parent_name,
                calls=calls,
            ))
            # Recurse into class body with this class as parent
            for child in node.children:
                _walk_node(
                    child, node_types, source, rel_path,
                    project_id, language, name, results,
                )
            return

        # For export statements, look inside for the actual declaration
        if node.type == "export_statement":
            for child in node.children:
                if child.type in node_types and child.type != "export_statement":
                    _walk_node(
                        child, node_types, source, rel_path,
                        project_id, language, parent_name, results,
                    )
                    return
            # If no nested declaration, treat the whole export as a chunk
            if not name:
                name = f"export_L{node.start_point[0] + 1}"

        qualified = f"{parent_name}.{name}" if parent_name else f"{rel_path}::{name}"
        chunk_id = _make_chunk_id(project_id, rel_path, node.start_byte, node.end_byte)

        results.append(CodeChunk(
            id=chunk_id,
            project_id=project_id,
            file_path=rel_path,
            language=language,
            node_type=node_type,
            name=name,
            qualified_name=qualified,
            content=content,
            embedding_input="",
            docstring=docstring,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            parent_name=parent_name,
            calls=calls,
        ))
        return

    # Not a matching node type — recurse into children
    for child in node.children:
        _walk_node(child, node_types, source, rel_path, project_id, language, parent_name, results)


def _extract_name(node, source: str, language: str) -> str:
    """Extract the name of a function/class/type from its AST node."""
    # Look for identifier child
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return source[child.start_byte:child.end_byte]

    # For arrow functions assigned to variables: look at parent
    if node.type == "arrow_function" and node.parent:
        for sibling in node.parent.children:
            if sibling.type in ("identifier", "property_identifier"):
                return source[sibling.start_byte:sibling.end_byte]

    # For lexical_declaration (const/let), extract the variable name
    if node.type == "lexical_declaration":
        for child in _iter_descendants(node):
            if child.type in ("identifier", "property_identifier"):
                return source[child.start_byte:child.end_byte]

    return f"anonymous_L{node.start_point[0] + 1}"


def _classify_node_type(node, language: str) -> str:
    """Map AST node type to a simpler classification."""
    type_map = {
        "function_declaration": "function",
        "function_definition": "function",
        "method_definition": "method",
        "arrow_function": "function",
        "class_declaration": "class",
        "class_definition": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "decorated_definition": "function",
        "lexical_declaration": "variable",
        "export_statement": "export",
    }
    return type_map.get(node.type, "other")


def _extract_docstring(node, source: str, language: str) -> str | None:
    """Extract docstring/JSDoc from a node."""
    if language == "python":
        # First statement in body might be a string expression
        for child in node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type == "expression_statement":
                        for expr in stmt.children:
                            if expr.type == "string":
                                text = source[expr.start_byte:expr.end_byte]
                                return text.strip("'\"").strip()
                    break
    elif language in ("typescript", "javascript"):
        # Look for preceding comment node
        if node.parent:
            idx = None
            for i, sibling in enumerate(node.parent.children):
                if sibling == node:
                    idx = i
                    break
            if idx and idx > 0:
                prev = node.parent.children[idx - 1]
                if prev.type == "comment":
                    text = source[prev.start_byte:prev.end_byte]
                    if text.startswith("/**"):
                        return _clean_jsdoc(text)
    return None


def _clean_jsdoc(text: str) -> str:
    """Clean JSDoc comment to plain text."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        line = re.sub(r"^/\*\*\s*", "", line)
        line = re.sub(r"\s*\*/$", "", line)
        line = re.sub(r"^\*\s?", "", line)
        if line and not line.startswith("@"):
            cleaned.append(line)
    return " ".join(cleaned).strip() or None  # type: ignore[return-value]


def _extract_calls(node, source: str, language: str) -> list[str]:
    """Extract function call names from within this node."""
    calls: list[str] = []
    for desc in _iter_descendants(node):
        if desc.type in ("call_expression", "call"):
            # Get the function name part
            func_node = desc.children[0] if desc.children else None
            if func_node:
                func_text = source[func_node.start_byte:func_node.end_byte]
                # Simplify: take the last part of member expressions
                parts = func_text.split(".")
                calls.append(parts[-1] if parts else func_text)
    return calls


def _extract_class_header(node, source: str, language: str) -> str:
    """Extract class header without method bodies — just signature + field declarations."""
    lines = []
    content = source[node.start_byte:node.end_byte]
    for line in content.split("\n")[:5]:  # First few lines usually contain class declaration
        lines.append(line)
    return "\n".join(lines)


def _iter_descendants(node):
    """Iterate all descendant nodes."""
    stack = list(node.children)
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _make_chunk_id(project_id: str, rel_path: str, start_byte: int, end_byte: int) -> str:
    """Create stable chunk ID from project + file + byte range."""
    raw = f"{project_id}:{rel_path}:{start_byte}:{end_byte}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _non_ws_count(text: str) -> int:
    """Count non-whitespace characters."""
    return sum(1 for c in text if not c.isspace())


def _split_large_chunks(
    chunks: list[CodeChunk],
    source: str,
    rel_path: str,
    project_id: str,
    language: str,
) -> list[CodeChunk]:
    """Split chunks larger than LARGE_CHUNK_THRESHOLD non-whitespace chars."""
    result: list[CodeChunk] = []
    for chunk in chunks:
        if _non_ws_count(chunk.content) <= LARGE_CHUNK_THRESHOLD:
            result.append(chunk)
            continue

        # Split by character count with overlap
        content = chunk.content
        parts: list[str] = []
        start = 0
        while start < len(content):
            end = start + LARGE_CHUNK_SPLIT_SIZE * 2  # rough char estimate for non-ws
            parts.append(content[start:end])
            start = end - LARGE_CHUNK_OVERLAP * 2
            if start >= len(content):
                break

        for i, part in enumerate(parts):
            # Approximate line numbers
            prefix_lines = content[:content.index(part) if part in content[start:] else 0].count("\n")
            part_chunk = CodeChunk(
                id=_make_chunk_id(project_id, rel_path, chunk.start_byte + i * 1000, chunk.end_byte),
                project_id=project_id,
                file_path=rel_path,
                language=language,
                node_type=chunk.node_type,
                name=f"{chunk.name}_part{i + 1}",
                qualified_name=f"{chunk.qualified_name}_part{i + 1}",
                content=part,
                embedding_input="",
                docstring=chunk.docstring if i == 0 else None,
                start_line=chunk.start_line + prefix_lines,
                end_line=chunk.start_line + prefix_lines + part.count("\n"),
                start_byte=chunk.start_byte,
                end_byte=chunk.end_byte,
                parent_name=chunk.parent_name,
                calls=chunk.calls if i == 0 else [],
            )
            result.append(part_chunk)

    return result


def _merge_small_chunks(
    chunks: list[CodeChunk],
    rel_path: str,
    project_id: str,
    language: str,
) -> list[CodeChunk]:
    """Merge adjacent small chunks (<500 non-ws chars) up to LARGE_CHUNK_THRESHOLD."""
    if len(chunks) <= 1:
        return chunks

    result: list[CodeChunk] = []
    buffer: list[CodeChunk] = []
    buffer_size = 0

    for chunk in chunks:
        chunk_size = _non_ws_count(chunk.content)

        if chunk_size >= SMALL_CHUNK_THRESHOLD:
            # Flush buffer first
            if buffer:
                result.append(_merge_buffer(buffer, rel_path, project_id, language))
                buffer = []
                buffer_size = 0
            result.append(chunk)
            continue

        # Try to add to buffer
        if buffer_size + chunk_size > LARGE_CHUNK_THRESHOLD:
            result.append(_merge_buffer(buffer, rel_path, project_id, language))
            buffer = []
            buffer_size = 0

        buffer.append(chunk)
        buffer_size += chunk_size

    if buffer:
        result.append(_merge_buffer(buffer, rel_path, project_id, language))

    return result


def _merge_buffer(
    chunks: list[CodeChunk],
    rel_path: str,
    project_id: str,
    language: str,
) -> CodeChunk:
    """Merge a list of small chunks into one."""
    if len(chunks) == 1:
        return chunks[0]

    first = chunks[0]
    last = chunks[-1]
    merged_content = "\n\n".join(c.content for c in chunks)
    names = [c.name for c in chunks if c.name]
    merged_name = "+".join(names[:3])
    if len(names) > 3:
        merged_name += f"+{len(names) - 3}more"

    return CodeChunk(
        id=_make_chunk_id(project_id, rel_path, first.start_byte, last.end_byte),
        project_id=project_id,
        file_path=rel_path,
        language=language,
        node_type="merged",
        name=merged_name,
        qualified_name=f"{rel_path}::{merged_name}",
        content=merged_content,
        embedding_input="",
        docstring=first.docstring,
        start_line=first.start_line,
        end_line=last.end_line,
        start_byte=first.start_byte,
        end_byte=last.end_byte,
        parent_name=first.parent_name,
        calls=[call for c in chunks for call in c.calls],
    )


def _build_embedding_input(chunk: CodeChunk) -> str:
    """Build contextualizedText for embedding (§7 of design doc)."""
    parts: list[str] = []

    # Header: [type] QualifiedName in file_path
    header = f"[{chunk.node_type}] "
    if chunk.parent_name:
        header += f"{chunk.parent_name}.{chunk.name}"
    else:
        header += chunk.name
    header += f" in {chunk.file_path}"
    parts.append(header)

    # Imports
    if chunk.imports:
        imports_str = ", ".join(chunk.imports[:10])
        parts.append(f"imports: {imports_str}")

    # Docstring
    if chunk.docstring:
        parts.append(chunk.docstring)

    # Content
    parts.append(chunk.content)

    return "passage: " + "\n".join(parts)


# -- Fallback chunking for unsupported languages --

def _fallback_chunking(
    source: str,
    rel_path: str,
    project_id: str,
    language: str,
) -> list[CodeChunk]:
    """Blank-line based chunking for unsupported languages."""
    blocks = re.split(r"\n{2,}", source)
    chunks: list[CodeChunk] = []
    offset = 0

    for block in blocks:
        block = block.strip()
        if not block:
            offset += 2
            continue

        start_line = source[:source.index(block, offset) if block in source[offset:] else offset].count("\n") + 1
        end_line = start_line + block.count("\n")
        start_byte = offset
        end_byte = offset + len(block.encode())

        name = f"{Path(rel_path).stem}:L{start_line}-L{end_line}"
        chunk_id = _make_chunk_id(project_id, rel_path, start_byte, end_byte)

        chunk = CodeChunk(
            id=chunk_id,
            project_id=project_id,
            file_path=rel_path,
            language=language,
            node_type="block",
            name=name,
            qualified_name=f"{rel_path}::{name}",
            content=block,
            embedding_input=f"passage: [{language}] {name} in {rel_path}\n{block}",
            start_line=start_line,
            end_line=end_line,
            start_byte=start_byte,
            end_byte=end_byte,
        )
        chunks.append(chunk)
        offset = source.index(block, offset) + len(block) if block in source[offset:] else offset + len(block)

    # Split oversized blocks
    final: list[CodeChunk] = []
    for chunk in chunks:
        if _non_ws_count(chunk.content) > LARGE_CHUNK_THRESHOLD:
            parts = _split_large_chunks([chunk], source, rel_path, project_id, language)
            final.extend(parts)
        else:
            final.append(chunk)

    return _merge_small_chunks(final, rel_path, project_id, language)
