# Tests

**Files**: 11 | **Symbols**: 60

## Files

- `tests/test_ast_chunker.py`
- `tests/test_callgraph.py`
- `tests/test_config.py`
- `tests/test_dag.py`
- `tests/test_doc_chunker.py`
- `tests/test_embedder.py`
- `tests/test_fusion.py`
- `tests/test_query_classifier.py`
- `tests/test_reranker.py`
- `tests/test_scanner.py`
- `tests/test_synthesizer.py`

## Entry Points

- `TestBuiltinFiltering.test_python_builtins_filtered`
- `TestBuiltinFiltering.test_react_hooks_filtered`
- `TestBuiltinFiltering.test_ts_builtins_filtered`
- `TestCommonNameRelaxation.test_common_name_multiple_candidates_with_module`
- `TestCommonNameRelaxation.test_common_name_with_module_upgrades_to_medium`

## Symbols

### `tests/test_ast_chunker.py`

- **TestChunkCodeFilePython+_chunk+test_extracts_function_and_class+22more** (merged, L23)
  - calls: chunk_code_file
- **test_c+test_ruby+test_unsupported_language_uses_fallback+15more** (merged, L194)
  - calls: _fallback_chunking, chunk_code_file

### `tests/test_callgraph.py`

- **_make_db** (function, L17)
  - called by: TestModuleMatches+test_exact_match+test_prefix_stripped+6more, TestSelfMethodResolution, test_high_confidence_with_module_from_import, test_insert_call_edges_with_module, test_self_method_resolves_high
- **_seed_db** (function, L21)
  - called by: TestModuleMatches+test_exact_match+test_prefix_stripped+6more
- **TestResolveSingle+_build_indexes+test_high_confidence_with_module+4more** (merged, L79)
  - calls: _resolve_single
- **test_same_file_preference** (function, L141)
  - calls: _resolve_single
- **test_multiple_candidates_no_same_file_returns_low** (function, L160)
  - calls: _resolve_single
- **TestModuleMatches+test_exact_match+test_prefix_stripped+6more** (merged, L177)
  - calls: _make_db, _module_matches, _seed_db, chunk_code_file, resolve_call_edges
- **test_high_confidence_with_module_from_import** (function, L218)
  - calls: _make_db, resolve_call_edges
- **test_import_call_binding_ts** (function, L252)
  - calls: chunk_code_file
- **test_import_call_binding_python** (function, L277)
  - calls: chunk_code_file
- **test_unmatched_call_has_none_module** (function, L300)
  - calls: chunk_code_file
- **test_insert_call_edges_with_module** (function, L321)
  - calls: _make_db
- **TestSelfMethodResolution** (class, L346)
  - calls: _make_db, chunk_code_file, resolve_call_edges
- **test_self_method_binding_python** (function, L349)
  - calls: chunk_code_file
- **test_this_method_binding_ts** (function, L401)
  - calls: chunk_code_file
- **test_self_method_resolves_high** (function, L448)
  - calls: _make_db, resolve_call_edges
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

- **TestDefaultConfig+test_default_data_dir+test_default_log_level+16more** (merged, L18)
  - calls: test_load_custom_config
- **test_load_custom_config** (function, L102)
  - called by: TestDefaultConfig+test_default_data_dir+test_default_log_level+16more, TestRerankingConfig+test_defaults+test_custom_values+17more

### `tests/test_dag.py`

- **_make_db** (function, L24)
  - called by: TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more, TestGenerateModuleWiki, test_no_edges_all_isolated, test_produces_modules_from_seeded_graph+test_isolated_nodes_grouped_by_directory+test_coverage_calculation+2more, test_wiki_page_contains_symbols+test_wiki_page_shows_call_relationships+test_wiki_page_has_external_deps+2more, test_wiki_page_has_title_and_files
- **_seed_graph_db** (function, L28)
  - called by: TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more, TestGenerateModuleWiki, test_produces_modules_from_seeded_graph+test_isolated_nodes_grouped_by_directory+test_coverage_calculation+2more, test_wiki_page_contains_symbols+test_wiki_page_shows_call_relationships+test_wiki_page_has_external_deps+2more, test_wiki_page_has_title_and_files
- **TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more** (merged, L121)
  - calls: _derive_module_name, _make_db, _seed_graph_db, build_dependency_graph, find_connected_components, topological_sort
- **test_produces_modules_from_seeded_graph+test_isolated_nodes_grouped_by_directory+test_coverage_calculation+2more** (merged, L270)
  - calls: _make_db, _seed_graph_db
- **test_no_edges_all_isolated** (function, L333)
  - calls: _make_db
- **TestGenerateModuleWiki** (class, L352)
  - calls: _make_db, _seed_graph_db
- **test_wiki_page_has_title_and_files** (function, L355)
  - calls: _make_db, _seed_graph_db
- **test_wiki_page_contains_symbols+test_wiki_page_shows_call_relationships+test_wiki_page_has_external_deps+2more** (merged, L378)
  - calls: _make_db, _seed_graph_db

### `tests/test_doc_chunker.py`

- **TestMarkdownChunking+_chunk+test_splits_on_headings+13more** (merged, L16)
  - calls: chunk_doc_file

### `tests/test_embedder.py`

- **TestEmbedderBasics+test_default_config_uses_openai+test_embedding_dim_is_1536+3more** (merged, L12)
  - calls: _embed_all, _openai_embed_request
- **test_embed_request_calls_correct_url** (function, L46)
  - calls: _openai_embed_request
- **test_embed_all_normalizes** (function, L63)
  - calls: _embed_all, _openai_embed_request
- **test_api_error_gives_clear_message+TestHotReloadableConfig+test_no_reload_when_unchanged+2more** (merged, L79)
  - calls: _openai_embed_request

### `tests/test_fusion.py`

- **TestReciprocalRankFusion+test_basic_fusion_both_lists+test_scores_are_descending+11more** (merged, L6)
  - calls: reciprocal_rank_fusion

### `tests/test_query_classifier.py`

- **TestClassifyQuery+test_camel_case+test_camel_case_multi_word+23more** (merged, L11)
  - calls: classify_query, get_bm25_weight

### `tests/test_reranker.py`

- **TestRerankingConfig+test_defaults+test_custom_values+17more** (merged, L17)
  - calls: handle_hybrid_search, test_load_custom_config
- **test_result_count_matches_response+test_result_fields_complete+TestConfigTomlParsing+2more** (merged, L150)
  - calls: handle_hybrid_search, load_config

### `tests/test_scanner.py`

- **TestIsChanged+test_empty_hash_triggers_reindex+test_matching_hash_not_changed+2more** (merged, L9)
  - calls: _is_changed

### `tests/test_synthesizer.py`

- **anonymous_L29+anonymous_L41** (merged, L29)
  - calls: _migrate_schema
- **anonymous_L46** (function, L46)
  - calls: compile_page, test_compile_with_synthesis_meta, upsert_file
- **TestSynthesisHash+test_same_input_same_hash+test_different_input_different_hash+14more** (merged, L100)
  - calls: _format_source_chunks, merge_synthesis_with_structure, verify_references
- **test_truncation_with_budget+TestEstimateTokens+test_token_estimate+4more** (merged, L204)
  - calls: _format_source_chunks, collect_module_context, estimate_tokens, prepare_context_file
- **test_writes_context_file** (function, L252)
  - calls: prepare_context_file
- **test_creates_parent_dirs+TestFinalizeModule** (merged, L274)
  - calls: finalize_module, lookup_page, prepare_context_file, test_compile_with_synthesis_meta
- **test_finalize_with_valid_refs** (function, L291)
  - calls: finalize_module
- **test_finalize_removes_bad_refs** (function, L316)
  - calls: finalize_module
- **test_finalize_updates_db_synthesis_meta** (function, L333)
  - calls: finalize_module, lookup_page, test_compile_with_synthesis_meta
- **test_finalize_missing_module+TestWikiStoreSynthesis** (merged, L351)
  - calls: compile_page, finalize_module, lookup_page, test_compile_with_synthesis_meta
- **test_compile_with_synthesis_meta** (function, L365)
  - calls: compile_page, lookup_page
  - called by: anonymous_L46, test_compile_without_synthesis_meta+TestSchemaMigration+test_fresh_db_has_synthesis_columns+4more, test_creates_parent_dirs+TestFinalizeModule, test_empty_when_no_links+TestVerifySymbols+test_finds_existing_symbols+6more, test_finalize_missing_module+TestWikiStoreSynthesis, test_finalize_updates_db_synthesis_meta, test_finds_linked_pages, test_no_skip_for_missing_module+TestGetSynthesisHash+test_returns_none_when_not_synthesized+2more
- **test_compile_without_synthesis_meta+TestSchemaMigration+test_fresh_db_has_synthesis_columns+4more** (merged, L387)
  - calls: finalize_module, lookup_page, should_skip_synthesis, test_compile_with_synthesis_meta
- **test_no_skip_when_files_changed** (function, L436)
  - calls: finalize_module, should_skip_synthesis
- **test_no_skip_for_missing_module+TestGetSynthesisHash+test_returns_none_when_not_synthesized+2more** (merged, L455)
  - calls: compile_page, finalize_module, find_indirectly_affected, lookup_page, should_skip_synthesis, test_compile_with_synthesis_meta
- **test_finds_linked_pages** (function, L489)
  - calls: compile_page, find_indirectly_affected, lookup_page, test_compile_with_synthesis_meta
- **test_empty_when_no_links+TestVerifySymbols+test_finds_existing_symbols+6more** (merged, L523)
  - calls: find_indirectly_affected, lookup_page, test_compile_with_synthesis_meta, verify_symbols

## Related Modules
- [[HANDOFF (isolated)]]
- [[benchmarks]]

- [[hybrid_search]]
- [[index]]
- [[search]]
- [[storage]]
- [[tools]]

## External Dependencies

**Calls out to:**
- `Embedder._embed_all`
- `Embedder._openai_embed_request`
- `StoreDB._migrate_schema`
- `StoreDB.upsert_file`
- `WikiStore.compile_page`
- `WikiStore.find_indirectly_affected`
- `WikiStore.lookup_page`
- `src/hybrid_search/config.py::load_config`
- `src/hybrid_search/index/ast_chunker.py::_fallback_chunking`
- `src/hybrid_search/index/ast_chunker.py::chunk_code_file`
