# Tools

**Files**: 4 | **Symbols**: 7

## Files

- `src/hybrid_search/tools/hybrid_search.py`
- `src/hybrid_search/tools/index.py`
- `src/hybrid_search/tools/semantic_search.py`
- `src/hybrid_search/tools/wiki.py`

## Entry Points

- `src/hybrid_search/tools/hybrid_search.py::handle_hybrid_search`
- `src/hybrid_search/tools/index.py::handle_index_project`
- `src/hybrid_search/tools/semantic_search.py::handle_semantic_search`
- `src/hybrid_search/tools/wiki.py::handle_compile_to_wiki`
- `src/hybrid_search/tools/wiki.py::handle_refresh_wiki_page`

## Symbols

### `src/hybrid_search/tools/hybrid_search.py`

- **handle_hybrid_search** (function, L21)
  - calls: hybrid_search
  - called by: TestRerankingConfig+test_defaults+test_custom_values+17more, create_server, test_result_count_matches_response+test_result_fields_complete+TestConfigTomlParsing+2more

### `src/hybrid_search/tools/index.py`

- **handle_index_project** (function, L13)
  - calls: index_project

### `src/hybrid_search/tools/semantic_search.py`

- **handle_semantic_search** (function, L15)
  - calls: _build_filter, _make_snippet, search
- **_build_filter** (function, L111)
  - called by: handle_semantic_search
- **_make_snippet** (function, L141)
  - called by: handle_semantic_search

### `src/hybrid_search/tools/wiki.py`

- **handle_compile_to_wiki** (function, L67)
  - calls: _resolve_file_deps
- **handle_refresh_wiki_page** (function, L177)
  - calls: _resolve_file_deps

## Related Modules
- [[HANDOFF (isolated)]]
- [[benchmarks]]
- [[storage]]

- [[hybrid_search]]
- [[index]]
- [[search]]
- [[tests]]

## External Dependencies

**Calls out to:**
- `IndexingPipeline.index_project`
- `SearchOrchestrator.hybrid_search`
- `VectorEngine.search`
- `src/hybrid_search/index/synthesizer.py::_resolve_file_deps`

**Called by:**
- `src/hybrid_search/server.py::create_server`
- `tests/test_reranker.py::TestRerankingConfig+test_defaults+test_custom_values+17more`
- `tests/test_reranker.py::test_result_count_matches_response+test_result_fields_complete+TestConfigTomlParsing+2more`
