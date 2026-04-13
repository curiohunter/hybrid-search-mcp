# Tools

**Files**: 6 | **Symbols**: 19

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
