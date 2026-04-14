# Tools
> synthesized: 2026-04-14

## Overview

The Tools module provides the MCP tool handler layer -- thin adapter functions that bridge the MCP server's `_dispatch_tool` dispatcher to the core domain logic (search orchestrators, indexing pipeline, call graph stores, and wiki store). Each handler validates inputs, opens the appropriate project stores, delegates to domain objects, and formats results as JSON-serializable dicts. This separation keeps protocol concerns (MCP tool definitions) decoupled from business logic.

## Key Design Decisions

- **Handlers as pure functions, not methods**: All tool handlers are module-level functions taking explicit dependencies (`config`, `registry`, `embedder`, etc.) rather than being methods on a class, enabling straightforward testing and avoiding shared mutable state (`src/hybrid_search/tools/hybrid_search.py:L8`, `src/hybrid_search/tools/trace.py:L24`).
- **3-priority symbol resolution for trace tools**: `_resolve_start` uses a deterministic ordering -- exact qualified_name > exact name > fuzzy LIKE match -- and returns ALL matches (not just the first) so tracing covers every matching symbol (`src/hybrid_search/tools/trace.py:L251-L626`).
- **MAX_NODES cap with truncation flag**: Trace handlers enforce a `MAX_NODES` limit on recursive graph traversal and set a `truncated` flag in the response, preventing unbounded result sets while informing the caller that results are incomplete (-L386`).
- **Unresolved callees are surfaced as partial results**: When `_trace_callees_recursive` encounters a call edge with no resolved `callee_chunk_id`, it includes the edge with `unresolved: True` rather than silently dropping it, giving callers visibility into the graph's completeness (-L525`).
- **Wiki dependency resolution via chunk-to-file mapping**: `_resolve_file_deps` converts `source_chunk_ids` to file-level dependencies with current hashes, establishing the staleness tracking contract between wiki content and source files (`src/hybrid_search/tools/wiki.py:L52-L74`).
- **Semantic search over-fetches by 3x then filters**: `handle_semantic_search` requests `limit * 3` from the vector engine and post-filters by similarity threshold, ensuring enough results survive the threshold cutoff (-L266`).

## Data Flow

```
MCP Client
   |
   v
server.py :: _dispatch_tool(name, args)
   |
   +---> handle_hybrid_search()     --> SearchOrchestrator.hybrid_search()
   +---> handle_semantic_search()   --> Embedder + VectorEngine.search()
   +---> handle_search_symbols()    --> StoreDB.search_chunks_by_name()
   +---> handle_index_project()     --> IndexingPipeline.index_project()
   +---> handle_index_status()      --> ProjectRegistry.list_all()
   +---> handle_trace_callers()     --> StoreDB.get_callers() (recursive)
   +---> handle_trace_callees()     --> StoreDB.get_callees() (recursive)
   +---> handle_compile_to_wiki()   --> WikiStore.compile_page()
   +---> handle_lookup_wiki()       --> WikiStore.lookup_page()
   +---> handle_check_wiki_staleness() --> WikiStore.check_staleness()
   +---> handle_refresh_wiki_page() --> WikiStore.refresh_page()
   |
   v
JSON dict response -> TextContent -> MCP Client
```

## Caveats

- **StoreDB connections are opened and closed per call**: Each handler opens its own `StoreDB` instance and closes it in a `finally` block. Concurrent MCP tool calls could contend on the same SQLite database file (`src/hybrid_search/tools/trace.py:L220-L577`).
- **`handle_index_status` accesses `pipeline._registry` directly**: The handler reaches into the pipeline's private `_registry` attribute rather than receiving the registry as a parameter, creating a coupling to the pipeline's internal structure ().
- **`handle_search_symbols` truncates content to 500 chars**: Symbol content is silently truncated at 500 characters with no indication to the caller that truncation occurred ().
- **Trace recursion is depth-limited but not stack-limited**: The recursive trace functions use Python's call stack. With `max_depth=10` and large fan-out, the visited set prevents infinite loops but deep chains could theoretically hit Python's recursion limit (`src/hybrid_search/tools/trace.py:L124`, `src/hybrid_search/tools/trace.py:L166`).
- **Wiki compile uses a transaction but trace does not**: Wiki write operations wrap DB mutations in `db.transaction()`, but trace operations (which are read-only) do not, which is correct but means any future write operations added to trace would need explicit transaction handling ().

## Related Modules

- [[search]] -- `handle_hybrid_search` delegates to `SearchOrchestrator`; `handle_semantic_search` uses `VectorEngine` directly
- [[wiki-system]] -- wiki tool handlers (`compile_to_wiki`, `lookup_wiki`, etc.) delegate to `WikiStore`
- [[call-graph-&-module-tree]] -- trace handlers traverse the resolved call graph stored by the indexing pipeline
- [[search-(isolated)]] -- `server.py` defines the MCP tool schemas and dispatches to these handlers

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `src/hybrid_search/tools/hybrid_search.py`
- `src/hybrid_search/tools/index.py`
- `src/hybrid_search/tools/semantic_search.py`
- `src/hybrid_search/tools/symbols.py`
- `src/hybrid_search/tools/trace.py`
- `src/hybrid_search/tools/wiki.py`

## Entry Points

- `src/hybrid_search/tools/hybrid_search.py::handle_hybrid_search`
- `src/hybrid_search/tools/index.py::handle_index_project`
- `src/hybrid_search/tools/index.py::handle_index_status`
- `src/hybrid_search/tools/semantic_search.py::handle_semantic_search`
- `src/hybrid_search/tools/symbols.py::handle_search_symbols`

## Symbols

### `src/hybrid_search/tools/hybrid_search.py`

- **handle_hybrid_search** (function, L8)
  - calls: hybrid_search
  - called by: _dispatch_tool

### `src/hybrid_search/tools/index.py`

- **handle_index_project** (function, L13)
  - calls: index_project
  - called by: _dispatch_tool
- **handle_index_status** (function, L38)
  - called by: _dispatch_tool

### `src/hybrid_search/tools/semantic_search.py`

- **handle_semantic_search** (function, L15)
  - calls: _build_filter, _make_snippet, search, upsert_file
  - called by: _dispatch_tool
- **_build_filter** (function, L111)
  - called by: handle_semantic_search
- **_make_snippet** (function, L141)
  - called by: handle_semantic_search

### `src/hybrid_search/tools/symbols.py`

- **handle_search_symbols** (function, L11)
  - calls: upsert_file
  - called by: _dispatch_tool

### `src/hybrid_search/tools/trace.py`

- **TraceError** (class, L20)
  - called by: _open_stores
- **handle_trace_callers** (function, L24)
  - calls: _open_stores, _resolve_start, _trace_callers_recursive
  - called by: _dispatch_tool
- **handle_trace_callees** (function, L74)
  - calls: _open_stores, _resolve_start, _trace_callees_recursive
  - called by: _dispatch_tool
- **_trace_callers_recursive** (function, L124)
  - calls: get_callers
  - called by: handle_trace_callers
- **_trace_callees_recursive** (function, L166)
  - calls: get_callees
  - called by: handle_trace_callees
- **_open_stores** (function, L220)
  - calls: TraceError, upsert_file
  - called by: handle_trace_callees, handle_trace_callers
- **_resolve_start** (function, L251)
  - called by: handle_trace_callees, handle_trace_callers

### `src/hybrid_search/tools/wiki.py`

- **WikiError+_open_store+_resolve_file_deps** (merged, L20)
  - calls: upsert_file
- **handle_compile_to_wiki** (function, L67)
  - calls: compile_page
  - called by: _dispatch_tool
- **handle_lookup_wiki** (function, L106)
  - calls: lookup_page
  - called by: _dispatch_tool
- **handle_check_wiki_staleness** (function, L144)
  - calls: check_staleness
  - called by: _dispatch_tool
- **handle_refresh_wiki_page** (function, L164)
  - calls: refresh_page
  - called by: _dispatch_tool

## External Dependencies

**Calls out to:**
- `IndexingPipeline.index_project`
- `SearchOrchestrator.hybrid_search`
- `StoreDB.get_callees`
- `StoreDB.get_callers`
- `StoreDB.upsert_file`
- `VectorEngine.search`
- `WikiStore.check_staleness`
- `WikiStore.compile_page`
- `WikiStore.lookup_page`
- `WikiStore.refresh_page`

**Called by:**
- `src/hybrid_search/server.py::_dispatch_tool`

</details>