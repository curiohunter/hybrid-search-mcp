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
- `src/hybrid_search/index/embedder.py::Embedder` (OpenAI API, token-aware batching, halve-and-retry)
- `src/hybrid_search/index/pipeline.py::IndexingPipeline` (atomic rebuild, _ConsistencyMismatchError, staged swap)
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

- **_BatchTooLargeError** (class, L28)
  - Raised when OpenAI returns 400, likely due to batch size
  - called by: _openai_embed_single_batch, _openai_embed_request
- **Embedder+__init__** (merged, L33)
  - calls: _embed_all, _get_api_key, _openai_embed_request, _split_into_token_batches, _truncate
- **_get_api_key** (function, L56)
  - calls: _load_dotenv_key
  - called by: Embedder, _openai_embed_request
- **_openai_embed_request** (function, L74)
  - calls: _truncate, _openai_embed_single_batch
  - Halve-and-retry: on _BatchTooLargeError, splits batch in half and retries recursively
  - called by: Embedder, _embed_all, cmd_reindex, create_server
- **_openai_embed_single_batch** (function, L99)
  - Sends a single batch to OpenAI. Raises _BatchTooLargeError on 400 errors
  - Retries up to 5 times on 429 rate limits with parsed wait time
  - called by: _openai_embed_request
- **_truncate** (function, L147)
  - Uses tiktoken for token-accurate truncation (default 8000, fallback 4000)
  - called by: Embedder, _openai_embed_request
- **_split_into_token_batches** (function, L157)
  - Token-aware batch splitting: respects both count (batch_size) and token limits (MAX_BATCH_TOKENS=250k)
  - called by: _embed_all
- **_embed_all** (function, L185)
  - Embeds texts via OpenAI API in token-aware batches with L2 normalization
  - calls: _openai_embed_request, _split_into_token_batches
  - called by: Embedder, embed_texts
- **_load_dotenv_key** (function, L203)
  - called by: _get_api_key

### `src/hybrid_search/index/pipeline.py`

- **IndexingResult** (dataclass, L48)
  - Fields: project_id, project_name, files_added, files_changed, files_deleted, chunks_total, elapsed_seconds, errors
- **_FileChunkResult** (dataclass, L64)
  - Intermediate result from Pass 1 (chunking only, no embedding yet)
- **_ConsistencyMismatchError** (dataclass/RuntimeError, L78)
  - Raised when SQLite/BM25/Vector counts diverge; triggers atomic rebuild
  - Fields: sqlite_count, bm25_count, vector_count
- **IndexingPipeline** (class, L88)
  - calls: __init__, _chunk_file, _clear_project, _flush_pending, _index_project_once, _process_deletions, _rebuild_project_atomically, _recover_atomic_rebuild, _store_file, chunk_code_file, chunk_doc_file
- **index_project** (function, L96)
  - calls: _index_project_once, _rebuild_project_atomically, _recover_atomic_rebuild
  - On force=True: delegates to _rebuild_project_atomically
  - On _ConsistencyMismatchError: auto-triggers atomic rebuild with error reporting
  - called by: cmd_reindex, create_server, handle_index_project
- **_index_project_once** (function, L172)
  - 2-pass architecture: chunk files (Pass 1), then batch embed + store (Pass 2)
  - EMBED_FLUSH_THRESHOLD=128 for memory-bounded batching
  - Post-index consistency check: raises _ConsistencyMismatchError if counts diverge
  - Supports scan_project_subset for delta indexing (changed_paths/deleted_paths)
  - calls: _chunk_file, _flush_pending, _process_deletions, resolve_call_edges, scan_project, scan_project_subset
  - called by: index_project, _rebuild_project_atomically
- **_rebuild_project_atomically** (function, L274)
  - Staged rebuild: builds into .rebuilding dir, then atomic swap via rename
  - Recovery: on failure, cleans up .rebuilding; on crash, _recover_atomic_rebuild restores from .backup
  - calls: _index_project_once, _recover_atomic_rebuild, _swap_project_dirs, _read_project_file_count
  - called by: index_project (on force or consistency mismatch)
- **_recover_atomic_rebuild** (function, L309)
  - Crash recovery: cleans up .rebuilding, restores .backup if project_dir missing
  - called by: index_project, _rebuild_project_atomically
- **_swap_project_dirs** (function, L321)
  - Atomic directory swap: project_dir → .backup, .rebuilding → project_dir
  - Rollback on failure: restores .backup if rename fails
  - called by: _rebuild_project_atomically
- **_read_project_file_count** (function, L343)
  - called by: _rebuild_project_atomically
- **_chunk_file** (function, L353)
  - Pass 1: chunk a single file without embedding
  - calls: chunk_code_file, chunk_doc_file, compute_file_hash, detect_language
  - called by: _index_project_once
- **_flush_pending** (function, L394)
  - Pass 2: batch-embed all pending chunks across files, write to stores, checkpoint
  - calls: _store_file, embed_texts
  - called by: _index_project_once
- **_store_file** (function, L430)
  - Multi-store update: SQLite transaction → BM25 → Vector
  - called by: _flush_pending
- **_process_deletions** (function, L519)
  - Remove deleted files from all stores (SQLite + BM25 + Vector)
  - called by: _index_project_once
- **_clear_project** (function, L543)
  - Clear all data for a project (for force re-index)
  - called by: IndexingPipeline

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
