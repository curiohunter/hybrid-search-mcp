# Index

**Files**: 8 | **Symbols**: 62

## Files

- `src/hybrid_search/index/ast_chunker.py`
- `src/hybrid_search/index/callgraph.py`
- `src/hybrid_search/index/dag.py`
- `src/hybrid_search/index/doc_chunker.py`
- `src/hybrid_search/index/embedder.py`
- `src/hybrid_search/index/pipeline.py`
- `src/hybrid_search/index/scanner.py`
- `src/hybrid_search/index/synthesizer.py`

## Entry Points

- `src/hybrid_search/index/dag.py::generate_all_wiki_pages`
- `src/hybrid_search/index/embedder.py::Embedder+__init__+anonymous_L35+3more`
- `src/hybrid_search/index/pipeline.py::anonymous_L39+anonymous_L55+IndexingPipeline+1more`
- `src/hybrid_search/index/synthesizer.py::collect_module_context`
- `src/hybrid_search/index/synthesizer.py::estimate_tokens`

## Symbols

### `src/hybrid_search/index/ast_chunker.py`

- **chunk_code_file** (function, L166)
  - calls: _build_embedding_input, _extract_chunks, _extract_imports, _fallback_chunking, _get_ts_language, _merge_small_chunks, _split_large_chunks
  - called by: TestBuiltinFiltering, TestChunkCodeFilePython+_chunk+test_extracts_function_and_class+22more, TestModuleMatches+test_exact_match+test_prefix_stripped+6more, TestSelfMethodResolution, _chunk_file, anonymous_L39+anonymous_L55+IndexingPipeline+1more, build_chunk_set, chunk_project
- **_get_ts_language** (function, L209)
  - called by: chunk_code_file
- **_extract_imports** (function, L258)
  - called by: chunk_code_file
- **_extract_chunks** (function, L469)
  - calls: _walk_node
  - called by: chunk_code_file
- **_walk_node** (function, L484)
  - calls: _classify_node_type, _extract_calls, _extract_docstring, _extract_name
  - called by: _extract_chunks
- **_extract_name** (function, L572)
  - called by: _walk_node
- **_classify_node_type** (function, L658)
  - called by: _walk_node
- **_extract_docstring** (function, L703)
  - calls: _clean_jsdoc
  - called by: _walk_node
- **_clean_jsdoc** (function, L786)
  - called by: _extract_docstring
- **_extract_calls** (function, L852)
  - calls: _extract_call_name_ex
  - called by: _walk_node
- **_extract_call_name_ex** (function, L886)
  - called by: _extract_calls
- **_split_large_chunks** (function, L961)
  - called by: _fallback_chunking, chunk_code_file
- **_merge_small_chunks** (function, L1011)
  - calls: _merge_buffer
  - called by: _fallback_chunking, chunk_code_file
- **_merge_buffer** (function, L1050)
  - called by: _merge_small_chunks
- **_build_embedding_input** (function, L1088)
  - called by: chunk_code_file
- **_fallback_chunking** (function, L1114)
  - calls: _merge_small_chunks, _split_large_chunks
  - called by: chunk_code_file, test_c+test_ruby+test_unsupported_language_uses_fallback+15more

### `src/hybrid_search/index/callgraph.py`

- **_build_module_index** (function, L34)
  - called by: resolve_call_edges
- **resolve_call_edges** (function, L78)
  - calls: _build_module_index, _resolve_single
  - called by: TestModuleMatches+test_exact_match+test_prefix_stripped+6more, TestSelfMethodResolution, anonymous_L39+anonymous_L55+IndexingPipeline+1more, cmd_reindex, index_project, test_high_confidence_with_module_from_import, test_self_method_resolves_high
- **_resolve_single** (function, L154)
  - calls: _module_matches
  - called by: TestCommonNameRelaxation, TestResolveSingle+_build_indexes+test_high_confidence_with_module+4more, resolve_call_edges, test_common_name_multiple_candidates_with_module, test_common_name_with_module_upgrades_to_medium, test_multiple_candidates_no_same_file_returns_low, test_same_file_preference
- **_module_matches** (function, L227)
  - called by: TestModuleMatches+test_exact_match+test_prefix_stripped+6more, _resolve_single

### `src/hybrid_search/index/dag.py`

- **build_dependency_graph** (function, L65)
  - called by: TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more, generate_all_wiki_pages, generate_wiki_plan
- **find_connected_components** (function, L93)
  - called by: TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more, generate_wiki_plan
- **topological_sort** (function, L138)
  - called by: TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more, generate_wiki_plan
- **_derive_module_name** (function, L177)
  - called by: TestBuildDependencyGraph+test_builds_forward_and_reverse+test_ignores_low_confidence+21more, _split_large_module, generate_wiki_plan
- **_group_isolated_by_directory** (function, L228)
  - called by: generate_wiki_plan
- **_representative_paths** (function, L261)
  - called by: _split_large_module, generate_wiki_plan
- **generate_wiki_plan** (function, L268)
  - calls: _deduplicate_names, _derive_module_name, _group_isolated_by_directory, _representative_paths, _split_large_module, build_dependency_graph, find_connected_components, topological_sort
  - called by: cmd_generate_wiki_plan, cmd_verify_wiki, generate_all_wiki_pages
- **_split_large_module** (function, L397)
  - calls: _derive_module_name, _representative_paths
  - called by: generate_wiki_plan
- **_inject_coreference_wikilinks** (function, L669)
  - called by: generate_all_wiki_pages
- **generate_all_wiki_pages** (function, L735)
  - calls: _inject_coreference_wikilinks, build_dependency_graph, generate_wiki_plan
  - called by: cmd_generate_wiki
- **_deduplicate_names** (function, L811)
  - called by: generate_wiki_plan

### `src/hybrid_search/index/doc_chunker.py`

- **chunk_doc_file** (function, L16)
  - calls: _chunk_markdown, _chunk_plain
  - called by: TestMarkdownChunking+_chunk+test_splits_on_headings+13more, _chunk_file, anonymous_L39+anonymous_L55+IndexingPipeline+1more, chunk_project, chunk_project
- **_chunk_markdown** (function, L36)
  - calls: _whole_file_chunk
  - called by: chunk_doc_file
- **_chunk_plain** (function, L91)
  - calls: _whole_file_chunk
  - called by: chunk_doc_file
- **_whole_file_chunk** (function, L158)
  - called by: _chunk_markdown, _chunk_plain

### `src/hybrid_search/index/embedder.py`

- **Embedder+__init__+anonymous_L35+3more** (merged, L27)
  - calls: _embed_all, _load_dotenv_key, _openai_embed_request, _truncate
- **_openai_embed_request** (function, L68)
  - calls: _truncate
  - called by: Embedder+__init__+anonymous_L35+3more, TestEmbedderBasics+test_default_config_uses_openai+test_embedding_dim_is_1536+3more, _embed_all, cmd_reindex, create_server, test_api_error_gives_clear_message+TestHotReloadableConfig+test_no_reload_when_unchanged+2more, test_embed_all_normalizes, test_embed_request_calls_correct_url
- **_truncate** (function, L107)
  - called by: Embedder+__init__+anonymous_L35+3more, _openai_embed_request
- **_embed_all** (function, L117)
  - calls: _openai_embed_request
  - called by: Embedder+__init__+anonymous_L35+3more, TestEmbedderBasics+test_default_config_uses_openai+test_embedding_dim_is_1536+3more, test_embed_all_normalizes
- **_load_dotenv_key** (function, L132)
  - called by: Embedder+__init__+anonymous_L35+3more

### `src/hybrid_search/index/pipeline.py`

- **anonymous_L39+anonymous_L55+IndexingPipeline+1more** (merged, L39)
  - calls: __init__, _chunk_file, _clear_project, _flush_pending, _process_deletions, _store_file, chunk_code_file, chunk_doc_file
- **index_project** (function, L81)
  - calls: __init__, _chunk_file, _clear_project, _flush_pending, _process_deletions, resolve_call_edges, scan_project, search
  - called by: _load, anonymous_L19+VectorEngine+__init__+4more, anonymous_L39+anonymous_L55+IndexingPipeline+1more, cmd_reindex, create_server, handle_index_project
- **_chunk_file** (function, L219)
  - calls: chunk_code_file, chunk_doc_file
  - called by: anonymous_L39+anonymous_L55+IndexingPipeline+1more, index_project
- **_flush_pending** (function, L260)
  - calls: _store_file
  - called by: anonymous_L39+anonymous_L55+IndexingPipeline+1more, index_project
- **_store_file** (function, L296)
  - called by: _flush_pending, anonymous_L39+anonymous_L55+IndexingPipeline+1more
- **_process_deletions** (function, L385)
  - called by: anonymous_L39+anonymous_L55+IndexingPipeline+1more, index_project
- **_clear_project** (function, L409)
  - called by: anonymous_L39+anonymous_L55+IndexingPipeline+1more, index_project

### `src/hybrid_search/index/scanner.py`

- **scan_project** (function, L29)
  - calls: _build_ignore_spec, _is_changed, _walk_files
  - called by: anonymous_L39+anonymous_L55+IndexingPipeline+1more, index_project
- **_is_changed** (function, L106)
  - called by: TestIsChanged+test_empty_hash_triggers_reindex+test_matching_hash_not_changed+2more, scan_project
- **_walk_files** (function, L136)
  - called by: build_chunk_set, chunk_project, chunk_project, scan_project
- **_build_ignore_spec** (function, L190)
  - called by: build_chunk_set, chunk_project, chunk_project, scan_project

### `src/hybrid_search/index/synthesizer.py`

- **should_skip_synthesis** (function, L105)
  - calls: _check_page_staleness
  - called by: _auto_prepare_synthesis, cmd_synthesize_wiki_part1, test_compile_without_synthesis_meta+TestSchemaMigration+test_fresh_db_has_synthesis_columns+4more, test_no_skip_for_missing_module+TestGetSynthesisHash+test_returns_none_when_not_synthesized+2more, test_no_skip_when_files_changed
- **collect_module_context** (function, L149)
  - calls: _extract_summary
  - called by: _auto_prepare_synthesis, cmd_synthesize_wiki_part1, test_truncation_with_budget+TestEstimateTokens+test_token_estimate+4more
- **_extract_summary** (function, L230)
  - called by: collect_module_context
- **prepare_context_file** (function, L248)
  - calls: _format_source_chunks
  - called by: _auto_prepare_synthesis, cmd_synthesize_wiki_part1, test_creates_parent_dirs+TestFinalizeModule, test_truncation_with_budget+TestEstimateTokens+test_token_estimate+4more, test_writes_context_file
- **verify_references** (function, L306)
  - called by: TestSynthesisHash+test_same_input_same_hash+test_different_input_different_hash+14more, cmd_verify_synthesis, finalize_module
- **verify_symbols** (function, L373)
  - called by: cmd_verify_synthesis, test_empty_when_no_links+TestVerifySymbols+test_finds_existing_symbols+6more
- **merge_synthesis_with_structure** (function, L416)
  - called by: TestSynthesisHash+test_same_input_same_hash+test_different_input_different_hash+14more, finalize_module
- **finalize_module** (function, L468)
  - calls: _resolve_file_deps, compile_page, merge_synthesis_with_structure, verify_references
  - called by: cmd_synthesize_wiki_part1, test_compile_without_synthesis_meta+TestSchemaMigration+test_fresh_db_has_synthesis_columns+4more, test_creates_parent_dirs+TestFinalizeModule, test_finalize_missing_module+TestWikiStoreSynthesis, test_finalize_removes_bad_refs, test_finalize_updates_db_synthesis_meta, test_finalize_with_valid_refs, test_no_skip_for_missing_module+TestGetSynthesisHash+test_returns_none_when_not_synthesized+2more
- **_resolve_file_deps** (function, L549)
  - called by: finalize_module, handle_compile_to_wiki, handle_refresh_wiki_page
- **_format_source_chunks** (function, L570)
  - called by: TestSynthesisHash+test_same_input_same_hash+test_different_input_different_hash+14more, estimate_tokens, prepare_context_file, test_truncation_with_budget+TestEstimateTokens+test_token_estimate+4more
- **estimate_tokens** (function, L595)
  - calls: _format_source_chunks
  - called by: cmd_synthesize_wiki_part1, test_truncation_with_budget+TestEstimateTokens+test_token_estimate+4more

## Related Modules
- [[HANDOFF (isolated)]]
- [[design (isolated)]]

- [[benchmarks]]
- [[hybrid_search]]
- [[search]]
- [[storage]]
- [[tests]]
- [[tools]]

## External Dependencies

**Calls out to:**
- `BM25Engine.__init__`
- `VectorEngine.search`
- `WikiStore._check_page_staleness`
- `WikiStore.compile_page`

**Called by:**
- `TestBuiltinFiltering.test_python_builtins_filtered`
- `TestBuiltinFiltering.test_react_hooks_filtered`
- `TestBuiltinFiltering.test_ts_builtins_filtered`
- `TestCommonNameRelaxation.test_common_name_multiple_candidates_with_module`
- `TestCommonNameRelaxation.test_common_name_with_module_upgrades_to_medium`
- `TestFinalizeModule.test_finalize_removes_bad_refs`
- `TestFinalizeModule.test_finalize_updates_db_synthesis_meta`
- `TestFinalizeModule.test_finalize_with_valid_refs`
- `TestImportCallBinding.test_high_confidence_with_module_from_import`
- `TestImportCallBinding.test_import_call_binding_python`
