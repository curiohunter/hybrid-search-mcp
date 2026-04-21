"""AST-based code chunking using TreeSitter.

Supports TypeScript/JavaScript/Python (Phase 1) + Rust/Go/Ruby/Java/C/C++/
Swift/Kotlin/CSS/SQL (Phase 3b).
Falls back to blank-line chunking for unsupported languages (e.g. HTML).

IMPORTANT: tree-sitter returns byte offsets in UTF-8. When extracting text
with node.start_byte / node.end_byte, always index into the *bytes* object
(source_bytes), not the Python str. Indexing into str with byte offsets
produces garbage when the source contains multi-byte characters (e.g., Korean,
em-dash).
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
    # --- Phase 3b languages ---
    "rust": {
        "function_item",
        "struct_item",
        "enum_item",
        "trait_item",
        "impl_item",
        "mod_item",
        "type_item",
    },
    "go": {
        "function_declaration",
        "method_declaration",
        "type_declaration",
    },
    "ruby": {
        "method",
        "class",
        "module",
        "singleton_method",
    },
    "java": {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "method_declaration",
        "constructor_declaration",
    },
    "c": {
        "function_definition",
        "struct_specifier",
        "type_definition",
    },
    "cpp": {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
        "template_declaration",
    },
    "swift": {
        "function_declaration",
        "class_declaration",
        "protocol_declaration",
    },
    "kotlin": {
        "function_declaration",
        "class_declaration",
    },
    "css": {
        "rule_set",
        "media_statement",
        "keyframes_statement",
    },
    "scss": {
        "rule_set",
        "media_statement",
        "keyframes_statement",
    },
    "sql": {
        "statement",
    },
}

# Node types that represent "class-like" containers
CLASS_NODE_TYPES = {
    "class_declaration",
    "class_definition",
    "interface_declaration",
    # Phase 3b
    "trait_item",       # Rust
    "impl_item",        # Rust
    "class",            # Ruby
    "module",           # Ruby
    "class_specifier",  # C++
    "namespace_definition",  # C++
    "protocol_declaration",  # Swift
}


def _node_text(source_bytes: bytes, node) -> str:
    """Extract node text using byte offsets (correct for multi-byte sources)."""
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


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
    calls: list[tuple[str, str | None]] = field(default_factory=list)


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

    source_bytes = source.encode("utf-8")

    try:
        parser = ts.Parser(ts_lang_obj)
        tree = parser.parse(source_bytes)
    except Exception:
        logger.warning("TreeSitter parse failed for %s, using fallback", rel_path)
        return _fallback_chunking(source, rel_path, project_id, language)

    node_types = CHUNK_NODE_TYPES.get(language, set())
    imports = _extract_imports(tree.root_node, language, source_bytes)
    import_map = _extract_import_map(tree.root_node, language, source_bytes)
    raw_chunks = _extract_chunks(tree.root_node, node_types, source_bytes, rel_path, project_id, language, import_map)

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
        elif language == "rust":
            import tree_sitter_rust
            return ts.Language(tree_sitter_rust.language())
        elif language == "go":
            import tree_sitter_go
            return ts.Language(tree_sitter_go.language())
        elif language == "ruby":
            import tree_sitter_ruby
            return ts.Language(tree_sitter_ruby.language())
        elif language == "java":
            import tree_sitter_java
            return ts.Language(tree_sitter_java.language())
        elif language == "c":
            import tree_sitter_c
            return ts.Language(tree_sitter_c.language())
        elif language == "cpp":
            import tree_sitter_cpp
            return ts.Language(tree_sitter_cpp.language())
        elif language == "swift":
            import tree_sitter_swift
            return ts.Language(tree_sitter_swift.language())
        elif language == "kotlin":
            import tree_sitter_kotlin
            return ts.Language(tree_sitter_kotlin.language())
        elif language in ("css", "scss"):
            import tree_sitter_css
            return ts.Language(tree_sitter_css.language())
        elif language == "sql":
            import tree_sitter_sql
            return ts.Language(tree_sitter_sql.language())
        # HTML: no AST chunking (element-level chunks are meaningless)
        # → falls through to blank-line fallback
    except ImportError:
        pass
    return None


def _extract_imports(root_node, ts_lang: str, source_bytes: bytes) -> list[str]:
    """Extract import paths from the AST root."""
    imports: list[str] = []
    for child in root_node.children:
        if ts_lang in ("typescript", "javascript"):
            if child.type == "import_statement":
                for desc in _iter_descendants(child):
                    if desc.type == "string":
                        text = _node_text(source_bytes, desc).strip("'\"")
                        imports.append(text)
        elif ts_lang == "python":
            if child.type in ("import_statement", "import_from_statement"):
                imports.append(_node_text(source_bytes, child))
        elif ts_lang == "rust":
            if child.type == "use_declaration":
                imports.append(_node_text(source_bytes, child))
        elif ts_lang == "go":
            if child.type == "import_declaration":
                for desc in _iter_descendants(child):
                    if desc.type == "interpreted_string_literal":
                        text = _node_text(source_bytes, desc).strip('"')
                        imports.append(text)
        elif ts_lang == "java":
            if child.type == "import_declaration":
                imports.append(_node_text(source_bytes, child))
        elif ts_lang == "ruby":
            if child.type == "call":
                text = _node_text(source_bytes, child)
                if text.startswith(("require", "require_relative")):
                    imports.append(text)
        elif ts_lang == "kotlin":
            if child.type == "import":
                imports.append(_node_text(source_bytes, child))
        elif ts_lang == "swift":
            if child.type == "import_declaration":
                imports.append(_node_text(source_bytes, child))
        elif ts_lang in ("c", "cpp"):
            if child.type == "preproc_include":
                imports.append(_node_text(source_bytes, child))
    return imports


def _extract_import_map(root_node, ts_lang: str, source_bytes: bytes) -> dict[str, str]:
    """Build a mapping of imported name → module path from AST root.

    Used to connect call names to their import source for call graph resolution.
    TS/JS: import { login } from "./auth" → {"login": "./auth"}
    Python: from src.auth import login → {"login": "src.auth"}
    Go: import "fmt" → {"fmt": "fmt"}
    """
    import_map: dict[str, str] = {}

    for child in root_node.children:
        if ts_lang in ("typescript", "javascript"):
            if child.type == "import_statement":
                module_path = None
                names: list[str] = []
                default_name = None
                for desc in _iter_descendants(child):
                    if desc.type == "string":
                        module_path = _node_text(source_bytes, desc).strip("'\"")
                    elif desc.type == "import_specifier":
                        # import { X } from "..." or import { X as Y } from "..."
                        children = list(desc.children)
                        if children:
                            # Last identifier is the local binding name
                            for c in reversed(children):
                                if c.type == "identifier":
                                    names.append(_node_text(source_bytes, c))
                                    break
                    elif desc.type == "identifier" and desc.parent == child:
                        # Default import: import X from "..."
                        default_name = _node_text(source_bytes, desc)
                    elif desc.type == "namespace_import":
                        # import * as X from "..."
                        for c in desc.children:
                            if c.type == "identifier":
                                names.append(_node_text(source_bytes, c))

                if module_path:
                    for name in names:
                        import_map[name] = module_path
                    if default_name:
                        import_map[default_name] = module_path

        elif ts_lang == "python":
            if child.type == "import_from_statement":
                # from X.Y import A, B   or   from X.Y import A as B
                # AST: from → dotted_name(module) → import → dotted_name(name)*
                module_name = None
                seen_import_keyword = False
                for sub in child.children:
                    if sub.type in ("from",):
                        continue
                    elif sub.type == "import":
                        seen_import_keyword = True
                        continue
                    elif not seen_import_keyword:
                        # Before 'import' keyword → module name
                        if sub.type in ("dotted_name", "relative_import"):
                            module_name = _node_text(source_bytes, sub)
                    else:
                        # After 'import' keyword → imported names
                        if sub.type == "dotted_name":
                            name = _node_text(source_bytes, sub)
                            if module_name:
                                import_map[name] = module_name
                        elif sub.type == "aliased_import":
                            alias_children = list(sub.children)
                            if len(alias_children) >= 2:
                                local_name = _node_text(source_bytes, alias_children[-1])
                                if module_name:
                                    import_map[local_name] = module_name

            elif child.type == "import_statement":
                # import X.Y or import X.Y as Z
                for sub in child.children:
                    if sub.type == "dotted_name":
                        full = _node_text(source_bytes, sub)
                        short = full.rsplit(".", 1)[-1]
                        import_map[short] = full
                    elif sub.type == "aliased_import":
                        alias_children = list(sub.children)
                        if len(alias_children) >= 2:
                            full = _node_text(source_bytes, alias_children[0])
                            local_name = _node_text(source_bytes, alias_children[-1])
                            import_map[local_name] = full

        elif ts_lang == "go":
            if child.type == "import_declaration":
                for desc in _iter_descendants(child):
                    if desc.type == "import_spec":
                        path_node = None
                        alias = None
                        for sub in desc.children:
                            if sub.type == "interpreted_string_literal":
                                path_node = _node_text(source_bytes, sub).strip('"')
                            elif sub.type == "package_identifier":
                                alias = _node_text(source_bytes, sub)
                        if path_node:
                            name = alias or path_node.rsplit("/", 1)[-1]
                            import_map[name] = path_node
                    elif desc.type == "interpreted_string_literal" and desc.parent and desc.parent.type == "import_declaration":
                        # Single import: import "fmt"
                        path = _node_text(source_bytes, desc).strip('"')
                        name = path.rsplit("/", 1)[-1]
                        import_map[name] = path

        elif ts_lang == "java":
            if child.type == "import_declaration":
                text = _node_text(source_bytes, child)
                # import com.example.Auth; → "Auth" → "com.example.Auth"
                # import com.example.*; → skip (wildcard)
                clean = text.removeprefix("import ").removesuffix(";").strip()
                if not clean.endswith("*"):
                    short = clean.rsplit(".", 1)[-1]
                    import_map[short] = clean

        elif ts_lang == "rust":
            if child.type == "use_declaration":
                text = _node_text(source_bytes, child)
                # use crate::auth::login; → "login" → "crate::auth"
                clean = text.removeprefix("use ").removesuffix(";").strip()
                if "::" in clean:
                    parts = clean.split("::")
                    module = "::".join(parts[:-1])
                    name = parts[-1]
                    if name.startswith("{"):
                        # use crate::auth::{login, logout};
                        names_str = name.strip("{}")
                        for n in names_str.split(","):
                            n = n.strip()
                            if n:
                                import_map[n] = module
                    else:
                        import_map[name] = module

        elif ts_lang == "ruby":
            if child.type == "call":
                text = _node_text(source_bytes, child)
                if text.startswith("require_relative"):
                    # require_relative "./auth" → module = "./auth"
                    match = re.search(r'''['"]([^'"]+)['"]''', text)
                    if match:
                        path = match.group(1)
                        name = Path(path).stem
                        import_map[name] = path
                elif text.startswith("require"):
                    match = re.search(r'''['"]([^'"]+)['"]''', text)
                    if match:
                        path = match.group(1)
                        name = path.rsplit("/", 1)[-1]
                        import_map[name] = path

        elif ts_lang == "kotlin":
            if child.type == "import":
                text = _node_text(source_bytes, child)
                clean = text.removeprefix("import ").strip()
                short = clean.rsplit(".", 1)[-1]
                module = clean.rsplit(".", 1)[0] if "." in clean else clean
                import_map[short] = module

        elif ts_lang == "swift":
            if child.type == "import_declaration":
                text = _node_text(source_bytes, child)
                clean = text.removeprefix("import ").strip()
                import_map[clean] = clean

    return import_map


def _extract_chunks(
    root_node,
    node_types: set[str],
    source_bytes: bytes,
    rel_path: str,
    project_id: str,
    language: str,
    import_map: dict[str, str] | None = None,
) -> list[CodeChunk]:
    """Walk AST and extract chunks for matching node types."""
    chunks: list[CodeChunk] = []
    _walk_node(root_node, node_types, source_bytes, rel_path, project_id, language, None, chunks, import_map or {})
    return chunks


def _walk_node(
    node,
    node_types: set[str],
    source_bytes: bytes,
    rel_path: str,
    project_id: str,
    language: str,
    parent_name: str | None,
    results: list[CodeChunk],
    import_map: dict[str, str] | None = None,
) -> None:
    """Recursively walk AST nodes, extracting chunks."""
    if node.type in node_types:
        name = _extract_name(node, source_bytes, language)
        node_type = _classify_node_type(node, language)
        content = _node_text(source_bytes, node)
        docstring = _extract_docstring(node, source_bytes, language)
        rationale = _extract_rationale(node, source_bytes, language)
        if rationale:
            docstring = f"{docstring}\n\n{rationale}" if docstring else rationale
        calls = _extract_calls(node, source_bytes, language, import_map or {}, parent_name)

        # For classes: extract the header, then recurse into methods
        if node.type in CLASS_NODE_TYPES:
            header_content = _extract_class_header(node, source_bytes, language)
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
            for child in node.children:
                _walk_node(
                    child, node_types, source_bytes, rel_path,
                    project_id, language, name, results, import_map,
                )
            return

        # For export statements, look inside for the actual declaration
        if node.type == "export_statement":
            for child in node.children:
                if child.type in node_types and child.type != "export_statement":
                    _walk_node(
                        child, node_types, source_bytes, rel_path,
                        project_id, language, parent_name, results, import_map,
                    )
                    return
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
        _walk_node(child, node_types, source_bytes, rel_path, project_id, language, parent_name, results, import_map)


def _extract_name(node, source_bytes: bytes, language: str) -> str:
    """Extract the name of a function/class/type from its AST node."""
    # Common name node types across languages
    _NAME_TYPES = {
        "identifier", "type_identifier", "property_identifier",
        "simple_identifier",  # Swift
        "constant",           # Ruby (class/module names)
        "field_identifier",   # Go method receiver
        "package_identifier", # Go
    }

    # Java method_declaration: type_identifier is the *return type*, not the name.
    # Must pick the plain identifier (method name) and skip type_identifier.
    if node.type in ("method_declaration", "constructor_declaration") and language == "java":
        for child in node.children:
            if child.type == "identifier":
                return _node_text(source_bytes, child)

    for child in node.children:
        if child.type in _NAME_TYPES:
            return _node_text(source_bytes, child)

    # C/C++ function_definition: name is inside function_declarator child
    if node.type == "function_definition":
        for child in node.children:
            if child.type == "function_declarator":
                for gc in child.children:
                    if gc.type in _NAME_TYPES:
                        return _node_text(source_bytes, gc)

    # C++ class_specifier/struct_specifier: name is type_identifier direct child
    if node.type in ("class_specifier", "struct_specifier"):
        for child in node.children:
            if child.type in _NAME_TYPES:
                return _node_text(source_bytes, child)

    # C++ namespace_definition: uses namespace_identifier
    if node.type == "namespace_definition":
        for child in node.children:
            if child.type == "namespace_identifier":
                return _node_text(source_bytes, child)

    # Go type_declaration: name is inside type_spec child
    if node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                for gc in child.children:
                    if gc.type in _NAME_TYPES:
                        return _node_text(source_bytes, gc)

    # Go method_declaration: receiver + name
    if node.type == "method_declaration":
        for child in node.children:
            if child.type == "field_identifier":
                return _node_text(source_bytes, child)

    # C++ template_declaration: look inside for the actual declaration name
    if node.type == "template_declaration":
        for child in node.children:
            if child.type in ("function_definition", "class_specifier", "struct_specifier"):
                return _extract_name(child, source_bytes, language)

    # CSS rule_set: use selector text as name
    if node.type == "rule_set":
        for child in node.children:
            if child.type == "selectors":
                return _node_text(source_bytes, child).strip()

    # SQL statement: extract table/view name from first few tokens
    if node.type == "statement":
        text = _node_text(source_bytes, node)[:80].strip()
        return text.split("\n")[0].strip()

    if node.type == "arrow_function" and node.parent:
        for sibling in node.parent.children:
            if sibling.type in ("identifier", "property_identifier"):
                return _node_text(source_bytes, sibling)

    if node.type == "lexical_declaration":
        for child in _iter_descendants(node):
            if child.type in ("identifier", "property_identifier"):
                return _node_text(source_bytes, child)

    return f"anonymous_L{node.start_point[0] + 1}"


def _classify_node_type(node, language: str) -> str:
    """Map AST node type to a simpler classification."""
    type_map = {
        # Phase 1: TS/JS/Python
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
        # Phase 3b
        "function_item": "function",       # Rust
        "struct_item": "struct",           # Rust
        "enum_item": "enum",              # Rust
        "trait_item": "trait",            # Rust
        "impl_item": "impl",             # Rust
        "mod_item": "module",            # Rust
        "type_item": "type",             # Rust
        "method_declaration": "method",    # Go, Java
        "type_declaration": "type",        # Go
        "method": "method",               # Ruby
        "class": "class",                 # Ruby
        "module": "module",               # Ruby
        "singleton_method": "method",      # Ruby
        "constructor_declaration": "constructor",  # Java
        "struct_specifier": "struct",      # C, C++
        "type_definition": "type",         # C
        "class_specifier": "class",        # C++
        "namespace_definition": "namespace",  # C++
        "template_declaration": "template",   # C++
        "protocol_declaration": "protocol",   # Swift
        "rule_set": "rule",                # CSS
        "media_statement": "media",        # CSS
        "keyframes_statement": "keyframes",  # CSS
        "statement": "statement",          # SQL
    }
    return type_map.get(node.type, "other")


def _extract_docstring(node, source_bytes: bytes, language: str) -> str | None:
    """Extract docstring/JSDoc from a node."""
    if language == "python":
        for child in node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type == "expression_statement":
                        for expr in stmt.children:
                            if expr.type == "string":
                                text = _node_text(source_bytes, expr)
                                return text.strip("'\"").strip()
                    break
    elif language in ("typescript", "javascript", "java"):
        # JSDoc / Javadoc: /** ... */ preceding the node
        if node.parent:
            idx = None
            for i, sibling in enumerate(node.parent.children):
                if sibling == node:
                    idx = i
                    break
            if idx and idx > 0:
                prev = node.parent.children[idx - 1]
                if prev.type == "comment":
                    text = _node_text(source_bytes, prev)
                    if text.startswith("/**"):
                        return _clean_jsdoc(text)
    elif language == "rust":
        # Rust doc comments: /// or //! preceding the node
        if node.parent:
            idx = None
            for i, sibling in enumerate(node.parent.children):
                if sibling == node:
                    idx = i
                    break
            if idx and idx > 0:
                doc_lines: list[str] = []
                for j in range(idx - 1, -1, -1):
                    prev = node.parent.children[j]
                    if prev.type == "line_comment":
                        text = _node_text(source_bytes, prev)
                        if text.startswith("///") or text.startswith("//!"):
                            doc_lines.insert(0, text.lstrip("/!").strip())
                        else:
                            break
                    else:
                        break
                if doc_lines:
                    return " ".join(doc_lines)
    elif language == "go":
        # Go doc comments: // preceding the node
        if node.parent:
            idx = None
            for i, sibling in enumerate(node.parent.children):
                if sibling == node:
                    idx = i
                    break
            if idx and idx > 0:
                prev = node.parent.children[idx - 1]
                if prev.type == "comment":
                    text = _node_text(source_bytes, prev)
                    return text.lstrip("/").strip()
    elif language == "ruby":
        # Ruby: # comment lines preceding the node
        if node.parent:
            idx = None
            for i, sibling in enumerate(node.parent.children):
                if sibling == node:
                    idx = i
                    break
            if idx and idx > 0:
                doc_lines: list[str] = []
                for j in range(idx - 1, -1, -1):
                    prev = node.parent.children[j]
                    if prev.type == "comment":
                        text = _node_text(source_bytes, prev).lstrip("#").strip()
                        doc_lines.insert(0, text)
                    else:
                        break
                if doc_lines:
                    return " ".join(doc_lines)
    return None


_RATIONALE_TAG_RE = re.compile(
    r"^(NOTE|WHY|TODO|FIXME|HACK|XXX)\s*:\s*(.+)$",
    re.IGNORECASE,
)

_COMMENT_NODE_TYPES = frozenset({
    "comment",          # python, ts/js, java, ruby
    "line_comment",     # rust
    "block_comment",    # rust
})


def _extract_rationale(node, source_bytes: bytes, language: str) -> str | None:
    """Collect NOTE/WHY/TODO/FIXME/HACK/XXX comments inside the node body.

    Also captures JSDoc/Javadoc ``@remarks`` / ``@note`` tags in the leading
    documentation block (TS/JS/Java). These are design-intent signals: why
    code exists, hidden invariants, surprising behavior — the things that
    don't show up in docstrings but guide reading.
    """
    results: list[str] = []

    stack = list(node.children)
    while stack:
        n = stack.pop()
        if n.type in _COMMENT_NODE_TYPES:
            text = _node_text(source_bytes, n)
            for line in text.splitlines():
                stripped = line.strip()
                # Strip leading comment markers (//, #, *, ///, //!, --)
                while stripped and stripped[0] in "/#*- ":
                    stripped = stripped[1:].lstrip()
                # Strip trailing */ from block comments
                if stripped.endswith("*/"):
                    stripped = stripped[:-2].rstrip()
                m = _RATIONALE_TAG_RE.match(stripped)
                if m:
                    body = m.group(2).strip()
                    if body:
                        results.append(f"{m.group(1).upper()}: {body}")
        else:
            stack.extend(n.children)

    if language in ("typescript", "javascript", "java"):
        # Walk up past any export_statement wrapper to find the JSDoc block
        # that sits as a sibling at the outer level.
        doc_target = node
        if doc_target.parent and doc_target.parent.type == "export_statement":
            doc_target = doc_target.parent
        if doc_target.parent:
            idx = None
            for i, sibling in enumerate(doc_target.parent.children):
                if sibling == doc_target:
                    idx = i
                    break
            if idx and idx > 0:
                prev = doc_target.parent.children[idx - 1]
                if prev.type == "comment":
                    text = _node_text(source_bytes, prev)
                    if text.startswith("/**"):
                        for m in re.finditer(
                            r"@(remarks?|note)\b\s*([^@]+?)(?=@|\*/)",
                            text, re.IGNORECASE | re.DOTALL,
                        ):
                            body = re.sub(r"\s*\*\s*", " ", m.group(2)).strip()
                            if body:
                                results.append(f"{m.group(1).upper()}: {body}")

    if not results:
        return None

    seen: set[str] = set()
    unique: list[str] = []
    for r in results:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return "\n".join(unique)


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


# Built-in / standard library DIRECT function calls (no receiver) that can never
# resolve to project chunks. Only unambiguous builtins — no names that could also
# be project functions (e.g. "parse", "format", "resolve" are kept).
_BUILTIN_CALLS: frozenset[str] = frozenset({
    # Python builtins (unambiguous)
    "len", "print", "range", "type", "str", "int", "float", "bool", "list",
    "dict", "set", "tuple", "isinstance", "issubclass", "hasattr", "getattr",
    "setattr", "delattr", "super", "enumerate", "zip", "sorted", "reversed",
    "abs", "min", "max", "sum", "all", "any", "repr", "id", "dir", "vars",
    "globals", "locals", "iter", "next", "hash", "callable", "chr", "ord",
    "hex", "oct", "bin", "property", "staticmethod", "classmethod", "object",
    # TS/JS globals (unambiguous)
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURI", "encodeURIComponent", "decodeURI", "decodeURIComponent",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame",
    "alert", "confirm", "atob", "btoa",
    "require",
    # React hooks (library, not project)
    "useState", "useEffect", "useCallback", "useMemo", "useRef",
    "useContext", "useReducer", "useLayoutEffect", "useImperativeHandle",
    "useDebugValue", "useTransition", "useDeferredValue", "useId",
    "forwardRef", "memo", "lazy",
    # Go builtins
    "make", "append", "copy", "panic", "recover", "cap",
    # Rust macros
    "println", "eprintln", "dbg", "todo", "unimplemented", "unreachable",
    "vec",
})

# Method names on CLEARLY non-project receivers (console.*, JSON.*, Math.*, DOM).
# Keep this minimal — ambiguous names like map/filter/find could be project methods.
_BUILTIN_METHOD_CALLS: frozenset[str] = frozenset({
    # console.* (TS/JS)
    "log", "warn", "error", "info", "debug", "trace", "assert",
    # JSON.* (TS/JS)
    "stringify",
    # Math.* (TS/JS)
    "floor", "ceil", "random", "sqrt", "pow",
    # DOM methods (clearly browser API)
    "getElementById", "getElementsByClassName", "getElementsByTagName",
    "querySelector", "querySelectorAll",
    "addEventListener", "removeEventListener",
    "getAttribute", "setAttribute", "removeAttribute",
    "appendChild", "removeChild", "insertBefore", "replaceChild",
    "createElement", "createTextNode",
    "preventDefault", "stopPropagation", "stopImmediatePropagation",
    # Rust macro-like
    "unwrap", "expect",
})


def _extract_calls(
    node,
    source_bytes: bytes,
    language: str,
    import_map: dict[str, str] | None = None,
    parent_name: str | None = None,
) -> list[tuple[str, str | None]]:
    """Extract function call names with their import module from within this node.

    Filters out built-in / standard library calls that can never resolve to project chunks.
    For this/self method calls, uses parent_name (containing class) to build a
    qualified module hint like "ClassName.methodName" → module = "__self__::ClassName".
    """
    calls: list[tuple[str, str | None]] = []
    imap = import_map or {}
    for desc in _iter_descendants(node):
        if desc.type in ("call_expression", "call"):
            name, is_self_call, is_method_call = _extract_call_name_ex(desc, source_bytes, language)
            if not name:
                continue
            # Filter out built-in / library calls
            if name in _BUILTIN_CALLS:
                continue
            if is_method_call and not is_self_call and name in _BUILTIN_METHOD_CALLS:
                continue

            if is_self_call and parent_name:
                module = f"__self__::{parent_name}"
            else:
                module = imap.get(name)
            calls.append((name, module))
    return calls


def _extract_call_name_ex(
    call_node, source_bytes: bytes, language: str,
) -> tuple[str | None, bool, bool]:
    """Extract function name from a call expression.

    Returns (name, is_self_call, is_method_call).
    - is_self_call: True when the call is on this/self (e.g., this.login(), self.login()).
    - is_method_call: True when the call is on any object (e.g., obj.method()).
    """
    if not call_node.children:
        return None, False, False

    func_node = call_node.children[0]

    # Direct identifier: foo()
    if func_node.type in ("identifier", "type_identifier"):
        return _node_text(source_bytes, func_node), False, False

    # Member expression: obj.method() — extract the method name
    if func_node.type in (
        "member_expression", "attribute",       # TS/JS, Python
        "selector_expression",                  # Go
        "field_expression",                     # Rust
        "scoped_identifier",                    # Rust (path::func)
    ):
        method_name = None
        is_self = False
        for child in reversed(func_node.children):
            if child.type in ("identifier", "property_identifier", "field_identifier"):
                method_name = _node_text(source_bytes, child)
                break

        # Check if the object is this/self
        if func_node.children:
            obj_node = func_node.children[0]
            if obj_node.type in ("this", "self"):
                is_self = True
            elif obj_node.type == "identifier":
                obj_text = _node_text(source_bytes, obj_node)
                if obj_text in ("this", "self"):
                    is_self = True

        return method_name, is_self, True  # is_method_call = True

    # Subscript expression or other complex forms — skip
    return None, False, False


def _extract_class_header(node, source_bytes: bytes, language: str) -> str:
    """Extract class header without method bodies — just signature + field declarations."""
    content = _node_text(source_bytes, node)
    lines = content.split("\n")[:5]
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
            if buffer:
                result.append(_merge_buffer(buffer, rel_path, project_id, language))
                buffer = []
                buffer_size = 0
            result.append(chunk)
            continue

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

    header = f"[{chunk.node_type}] "
    if chunk.parent_name:
        header += f"{chunk.parent_name}.{chunk.name}"
    else:
        header += chunk.name
    header += f" in {chunk.file_path}"
    parts.append(header)

    if chunk.imports:
        imports_str = ", ".join(chunk.imports[:10])
        parts.append(f"imports: {imports_str}")

    if chunk.docstring:
        parts.append(chunk.docstring)

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

    final: list[CodeChunk] = []
    for chunk in chunks:
        if _non_ws_count(chunk.content) > LARGE_CHUNK_THRESHOLD:
            parts = _split_large_chunks([chunk], source, rel_path, project_id, language)
            final.extend(parts)
        else:
            final.append(chunk)

    return _merge_small_chunks(final, rel_path, project_id, language)
