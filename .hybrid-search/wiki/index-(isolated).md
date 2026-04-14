# Index (Isolated)
> synthesized: 2026-04-14

## Overview

This isolated module group contains internal helper functions from the indexing pipeline -- specifically `ast_chunker.py` and `scanner.py` -- that have no inbound call edges from other wiki modules. These functions handle the low-level mechanics of AST-based code chunking (tree-sitter parsing, import map extraction across 13 languages, recursive AST walking, name extraction, node type classification) and file change detection (hash computation, language detection). They form the foundation of the indexing pipeline but are invoked only through `chunk_code_file()` and `scan_files()` entry points.

## Key Design Decisions

- **Byte-offset-based node text extraction**: `_node_text()` uses `node.start_byte:node.end_byte` on the raw bytes rather than character offsets, correctly handling multi-byte Unicode sources like Korean comments (`src/hybrid_search/index/ast_chunker.py:L140`)
- **Import map as separate function from import list**: `_extract_import_map()` builds a `name -> module` dictionary for call graph resolution, kept separate from `_extract_imports()` which returns raw import strings for embedding input. This avoids coupling embedding and call graph concerns (`src/hybrid_search/index/ast_chunker.py:L300`)
- **Language-specific import parsing per AST node type**: Each of the 13 supported languages has a dedicated branch in `_extract_import_map()` handling its import syntax (named/default/namespace for TS/JS, from-import/aliased for Python, use-declarations for Rust, etc.) (`src/hybrid_search/index/ast_chunker.py:L300`)
- **Lazy tree-sitter grammar loading**: `_get_ts_language()` imports grammar packages on demand rather than at module load time, so unsupported grammars only fail at parse time with a graceful `None` return and fallback to blank-line chunking (`src/hybrid_search/index/ast_chunker.py:L209`)
- **Class chunks extract header only**: When a class node is encountered, only the header (signature + docstring) is stored as the class chunk, while methods recurse as separate chunks. This prevents duplication of method content in the class chunk (`src/hybrid_search/index/ast_chunker.py:L487`)
- **Post-processing pipeline**: After initial AST extraction, chunks go through `_split_large_chunks` (>4000 non-whitespace chars) then `_merge_small_chunks` (<500 chars) to maintain optimal chunk sizes (`src/hybrid_search/index/ast_chunker.py:L166`)

## Data Flow

```
Source file (bytes)
  |
  v
_get_ts_language(language) --> None? --> _fallback_chunking()
  |
  v (Language object)
ts.Parser.parse(source_bytes) --> tree
  |
  v
_extract_imports(root)       --> list[str] (for embedding)
_extract_import_map(root)    --> dict[str, str] (for call graph)
_extract_chunks(root)        --> list[CodeChunk] (raw)
  |
  v
_split_large_chunks() --> _merge_small_chunks()
  |
  v
_build_embedding_input(chunk) per chunk
  |
  v
list[CodeChunk] (final)
```

## Caveats

- Java `method_declaration` name extraction has a special case to skip `type_identifier` (return type) and pick `identifier` (method name) -- other languages with similar ambiguity (Kotlin, Swift) may not have this guard (`src/hybrid_search/index/ast_chunker.py:L568`)
- Ruby import parsing uses regex on the raw `require` call text rather than AST structure, which could miss edge cases like interpolated strings (`src/hybrid_search/index/ast_chunker.py:L420`)
- Rust `use` declaration parsing with curly braces (`use crate::auth::{login, logout}`) splits on commas naively and does not handle nested groups (`use crate::{a::{b, c}}`) (`src/hybrid_search/index/ast_chunker.py:L381`)
- The `_extract_import_map` function is split into 3 parts in the synthesis input, suggesting it is quite large -- a single function handling 13 language branches (`src/hybrid_search/index/ast_chunker.py:L300`)
- `compute_file_hash` and `detect_language` in `scanner.py` are isolated helpers with no outgoing calls tracked, suggesting they are leaf utility functions (`src/hybrid_search/index/scanner.py:L78`)

## Related Modules

- [[tests]] -- `test_ast_chunker.py` and `test_scanner.py` exercise these functions
- [[architecture]] -- these modules implement the AST Chunker and File Scanner components of the indexing pipeline

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `src/hybrid_search/index/ast_chunker.py`
- `src/hybrid_search/index/scanner.py`

## Symbols

### `src/hybrid_search/index/ast_chunker.py`

- **_node_text+anonymous_L145** (merged, L140)
- **_extract_import_map_part1** (function, L300)
- **_extract_import_map_part2** (function, L300)
- **_extract_import_map_part3** (function, L300)
- **_extract_class_header+_iter_descendants+_make_chunk_id+1more** (merged, L934)

### `src/hybrid_search/index/scanner.py`

- **anonymous_L22** (function, L22)
- **compute_file_hash+detect_language** (merged, L78)

</details>