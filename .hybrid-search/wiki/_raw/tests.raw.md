# Tests
> synthesized: 2026-04-14

## Overview

The test suite validates the core modules of the hybrid search system across 11 test files with 47 symbols, covering configuration loading, AST chunking, call graph resolution, document chunking, embedding backends, RRF fusion, query classification, file scanning delta detection, SQLite store operations, and wiki page lifecycle. These tests ensure correctness of cross-language search, crash recovery, and the reactive wiki layer without requiring live API calls or real embedding models.

## Key Design Decisions

- **In-memory SQLite via `tmp_path` fixtures**: All database-dependent tests (callgraph, wiki, store_db) create ephemeral `StoreDB` instances scoped to pytest `tmp_path`, avoiding shared state between tests (`tests/test_callgraph.py:L17`, `tests/test_wiki.py:L13`)
- **Mock-based embedding tests**: OpenAI API calls are patched with `unittest.mock.patch` on `urllib.request.urlopen`, verifying URL construction and L2 normalization without network access (`tests/test_embedder.py:L46`, `tests/test_embedder.py:L63`)
- **Query classifier uses pattern-based heuristics, not ML**: Tests assert deterministic classification of camelCase/snake_case as `EXACT_SYMBOL`, Korean text as `KOREAN_NL`, and plain English as `ENGLISH_NL` (`tests/test_query_classifier.py:L11`)
- **Crash recovery via empty hash detection**: `_is_changed()` treats `file_hash=""` as a partial-write crash marker that always triggers reindex (`tests/test_scanner.py:L9`)
- **Word-order-invariant wiki lookup**: `normalize_query` sorts words alphabetically so "auth flow" and "flow auth" map to the same key (`tests/test_wiki.py:L45`)

## Data Flow

```
Test Fixtures (tmp_path, seeded_db)
  |
  v
Unit Under Test (config, chunker, callgraph, wiki, etc.)
  |
  v
Assertions (pytest assert)
  |
  +-- Config: load_config() from TOML string -> verify defaults/overrides
  +-- AST Chunker: chunk_code_file() on inline source -> verify chunk names/types
  +-- Call Graph: _resolve_single() / resolve_call_edges() -> verify confidence tiers
  +-- Wiki: compile_page() -> check_staleness() -> refresh_page() -> verify lifecycle
  +-- Fusion: reciprocal_rank_fusion() -> verify score ordering
  +-- Scanner: _is_changed() with FileRecord -> verify delta detection
```

## Caveats

- Wiki test `seeded_db` fixture accesses `db._conn` directly (private attribute) to set up test data, coupling tests to internal DB implementation (`tests/test_wiki.py:L18`)
- Call graph tests use hardcoded `PROJECT_ID` constant and manually constructed `FileRecord`/`ChunkRecord` objects rather than going through the indexing pipeline, so integration-level regressions in the pipeline could be missed (`tests/test_callgraph.py:L21`)
- `test_reload_when_mtime_changes` uses `time.sleep(0.05)` and manual `os.utime` to force mtime changes, which can be flaky on filesystems with coarse timestamps (`tests/test_embedder.py:L94`)
- No integration tests for the full search pipeline (BM25 + Vector + RRF combined) -- each component is tested in isolation

## Related Modules

- [[architecture]] -- tests validate the core components described in the architecture (config, chunker, callgraph, search, wiki)
- [[index-(isolated)]] -- AST chunker and scanner source code tested by `test_ast_chunker.py` and `test_scanner.py`
- [[tests-(isolated)]] -- additional isolated test symbols for callgraph common names and embedder hot-reload

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `tests/test_ast_chunker.py`
- `tests/test_callgraph.py`
- `tests/test_config.py`
- `tests/test_cwd_boost.py`
- `tests/test_doc_chunker.py`
- `tests/test_embedder.py`
- `tests/test_fusion.py`
- `tests/test_query_classifier.py`
- `tests/test_scanner.py`
- `tests/test_store_db.py`
- `tests/test_wiki.py`

## Entry Points

- `TestBuiltinFiltering.test_python_builtins_filtered`
- `TestBuiltinFiltering.test_react_hooks_filtered`
- `TestBuiltinFiltering.test_ts_builtins_filtered`
- `TestCascade.test_file_delete_cascades_to_dependencies`
- `TestCommonNameRelaxation.test_common_name_multiple_candidates_with_module`

## Symbols

### `tests/test_ast_chunker.py`

- **TestChunkCodeFilePython+_chunk+test_extracts_function_and_class+22more** (merged, L23)
  - calls: chunk_code_file
- **test_c+test_ruby+test_unsupported_language_uses_fallback+15more** (merged, L194)
  - calls: _fallback_chunking, chunk_code_file

### `tests/test_callgraph.py`

- **_make_db** (function, L17)
  - calls: upsert_file
  - called by: TestModuleMatches+test_exact_match+test_prefix_stripped+6more, TestSelfMethodResolution, test_high_confidence_with_module_from_import, test_insert_call_edges_with_module, test_self_method_resolves_high
- **_seed_db** (function, L21)
  - calls: upsert_file
  - called by: TestModuleMatches+test_exact_match+test_prefix_stripped+6more
- **TestResolveSingle+_build_indexes+test_high_confidence_with_module+4more** (merged, L79)
  - calls: _resolve_single
- **test_same_file_preference** (function, L141)
  - calls: _resolve_single
- **test_multiple_candidates_no_same_file_returns_low** (function, L160)
  - calls: _resolve_single
- **TestModuleMatches+test_exact_match+test_prefix_stripped+6more** (merged, L177)
  - calls: _make_db, _module_matches, _seed_db, chunk_code_file, resolve_call_edges, upsert_file
- **test_high_confidence_with_module_from_import** (function, L218)
  - calls: _make_db, resolve_call_edges, upsert_file
- **test_import_call_binding_ts** (function, L252)
  - calls: chunk_code_file
- **test_import_call_binding_python** (function, L277)
  - calls: chunk_code_file
- **test_unmatched_call_has_none_module** (function, L300)
  - calls: chunk_code_file
- **test_insert_call_edges_with_module** (function, L321)
  - calls: _make_db, upsert_file
- **TestSelfMethodResolution** (class, L346)
  - calls: _make_db, chunk_code_file, resolve_call_edges, upsert_file
- **test_self_method_binding_python** (function, L349)
  - calls: chunk_code_file
- **test_this_method_binding_ts** (function, L401)
  - calls: chunk_code_file
- **test_self_method_resolves_high** (function, L448)
  - calls: _make_db, resolve_call_edges, upsert_file
- **TestCommonNameRelaxation** (class, L482)
  - calls: _resolve_single
- **test_common_name_with_module_upgrades_to_medium** (function, L485)
  - calls: _resolve_single
- **test_common_name_multiple_candidates_with_module** (function, L503)
  - calls: _resolve_single
- **TestBuiltinFiltering** (class, L531)
  - calls: chunk_code_file
- **test_python_builtins_filtered** (function, L534)
  - calls: chunk_code_file
- **test_ts_builtins_filtered** (function, L562)
  - calls: chunk_code_file
- **test_react_hooks_filtered** (function, L588)
  - calls: chunk_code_file

### `tests/test_config.py`

- **TestDefaultConfig+test_default_data_dir+test_default_log_level+20more** (merged, L18)
  - calls: load_config, test_load_custom_config
- **test_load_custom_config** (function, L118)
  - calls: load_config
  - called by: TestDefaultConfig+test_default_data_dir+test_default_log_level+20more
- **test_partial_config_uses_defaults+test_data_dir_expansion+test_empty_projects_list** (merged, L156)
  - calls: load_config

### `tests/test_cwd_boost.py`

- **TestWeightedInterleave+test_basic_2_to_1+test_dedup+15more** (merged, L13)
  - calls: _interleave_round_robin, _weighted_interleave

### `tests/test_doc_chunker.py`

- **TestMarkdownChunking+_chunk+test_splits_on_headings+13more** (merged, L16)
  - calls: chunk_doc_file

### `tests/test_embedder.py`

- **TestEmbedderBackendSelection+test_default_backend_is_onnx+test_st_backend+4more** (merged, L12)
  - calls: _ollama_embed_request
- **test_ollama_embed_request_builds_correct_payload** (function, L51)
  - calls: _ollama_embed_request
- **test_ollama_connection_error_gives_clear_message+TestHotReloadableConfig+test_no_reload_when_unchanged** (merged, L69)
  - calls: _ollama_embed_request

### `tests/test_fusion.py`

- **TestReciprocalRankFusion+test_basic_fusion_both_lists+test_scores_are_descending+11more** (merged, L6)
  - calls: reciprocal_rank_fusion

### `tests/test_query_classifier.py`

- **TestClassifyQuery+test_camel_case+test_camel_case_multi_word+23more** (merged, L11)
  - calls: classify_query, get_bm25_weight

### `tests/test_scanner.py`

- **TestIsChanged+test_empty_hash_triggers_reindex+test_matching_hash_not_changed+2more** (merged, L9)
  - calls: _is_changed

### `tests/test_store_db.py`

- **test_upsert_file_does_not_delete_existing_chunks** (function, L6)
  - calls: upsert_file

### `tests/test_wiki.py`

- **anonymous_L13** (function, L13)
  - calls: upsert_file
- **anonymous_L18** (function, L18)
  - calls: upsert_file
- **anonymous_L45+TestNormalizeQuery+test_basic+17more** (merged, L45)
  - calls: check_staleness, compile_page, lookup_page
- **test_stale_changed_files_detail** (function, L178)
  - calls: check_staleness, compile_page
- **test_check_specific_page+TestRefresh+test_refresh_bumps_version** (merged, L196)
  - calls: check_staleness, compile_page, refresh_page
- **test_refresh_re_snapshots_hashes** (function, L217)
  - calls: check_staleness, compile_page, refresh_page
- **test_refresh_nonexistent+TestEviction+test_lru_eviction** (merged, L236)
  - calls: compile_page, list_pages, lookup_page, refresh_page
- **test_lru_eviction_respects_access** (function, L254)
  - calls: compile_page, list_pages, lookup_page
- **TestDelete+test_delete_page+test_delete_nonexistent+1more** (merged, L277)
  - calls: compile_page, delete_page, lookup_page
- **test_file_delete_cascades_to_dependencies** (function, L294)
  - calls: compile_page
- **TestListPages+test_list_pages+test_access_count_increments** (merged, L313)
  - calls: compile_page, list_pages, lookup_page

## External Dependencies

**Calls out to:**
- `Embedder._ollama_embed_request`
- `StoreDB.upsert_file`
- `WikiStore.check_staleness`
- `WikiStore.compile_page`
- `WikiStore.delete_page`
- `WikiStore.list_pages`
- `WikiStore.lookup_page`
- `WikiStore.refresh_page`
- `src/hybrid_search/config.py::load_config`
- `src/hybrid_search/index/ast_chunker.py::_fallback_chunking`

</details>