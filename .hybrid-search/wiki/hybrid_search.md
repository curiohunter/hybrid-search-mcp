# Hybrid Search

**Files**: 3 | **Symbols**: 27

## Files

- `src/hybrid_search/cli.py`
- `src/hybrid_search/config.py`
- `src/hybrid_search/server.py`

## Entry Points

- `src/hybrid_search/cli.py::cmd_synthesize_wiki_part1`
- `src/hybrid_search/cli.py::main_part1`
- `src/hybrid_search/server.py::_HotReloadableConfig+__init__+_get_mtime+1more`
- `src/hybrid_search/server.py::_run_server+main`

## Symbols

### `src/hybrid_search/cli.py`

- **_detect_project** (function, L27)
  - called by: cmd_call_graph_stats, cmd_generate_wiki, cmd_generate_wiki_plan, cmd_lookup_wiki, cmd_reindex, cmd_search_symbols, cmd_stale, cmd_sync_wiki
- **_ensure_claude_md** (function, L59)
  - called by: cmd_reindex
- **_write_gap_flag** (function, L77)
  - called by: cmd_reindex
- **cmd_reindex** (function, L91)
  - calls: _auto_prepare_synthesis, _detect_project, _ensure_claude_md, _mark_stale_wikis, _migrate_schema, _openai_embed_request, _write_gap_flag, cmd_generate_wiki
  - called by: main_part1
- **_mark_stale_wikis** (function, L195)
  - calls: _migrate_schema, check_staleness
  - called by: cmd_reindex
- **_auto_prepare_synthesis** (function, L253)
  - calls: _migrate_schema, check_staleness, collect_module_context, find_indirectly_affected, prepare_context_file, should_skip_synthesis
  - called by: cmd_reindex
- **cmd_status** (function, L352)
  - calls: load_config
  - called by: main_part1
- **cmd_stale** (function, L369)
  - calls: _detect_project, _migrate_schema, check_staleness, load_config
  - called by: main_part1
- **cmd_sync_wiki** (function, L408)
  - calls: _detect_project, _migrate_schema, _resolve_wiki_deps, compile_page, load_config
  - called by: cmd_reindex, main_part1
- **_resolve_wiki_deps** (function, L496)
  - called by: cmd_sync_wiki
- **cmd_call_graph_stats** (function, L520)
  - calls: _detect_project, _migrate_schema, load_config
  - called by: main_part1
- **cmd_generate_wiki** (function, L574)
  - calls: _detect_project, _migrate_schema, compile_page, generate_all_wiki_pages, load_config
  - called by: cmd_reindex, main_part1
- **cmd_generate_wiki_plan** (function, L659)
  - calls: _detect_project, _migrate_schema, generate_wiki_plan, load_config
  - called by: main_part1
- **cmd_verify_wiki** (function, L744)
  - calls: _detect_project, _migrate_schema, check_staleness, generate_wiki_plan, list_pages, load_config
  - called by: main_part1
- **cmd_search_symbols** (function, L848)
  - calls: _detect_project, _migrate_schema, load_config
  - called by: main_part1
- **cmd_remove_project** (function, L901)
  - calls: load_config
  - called by: main_part1
- **cmd_lookup_wiki** (function, L922)
  - calls: _detect_project, _migrate_schema, load_config, lookup_page
  - called by: main_part1
- **cmd_verify_synthesis** (function, L966)
  - calls: _detect_project, _migrate_schema, list_pages, load_config, refresh_page, verify_references, verify_symbols
  - called by: main_part1
- **cmd_synthesize_wiki_part1** (function, L1099)
  - calls: _detect_project, _migrate_schema, check_staleness, collect_module_context, estimate_tokens, finalize_module, list_pages, load_config
- **cmd_setup** (function, L1265)
  - called by: main_part1
- **cmd_install_hook** (function, L1397)
  - called by: cmd_reindex, main_part1
- **main_part1** (function, L1448)
  - calls: cmd_call_graph_stats, cmd_generate_wiki, cmd_generate_wiki_plan, cmd_install_hook, cmd_lookup_wiki, cmd_reindex, cmd_remove_project, cmd_search_symbols

### `src/hybrid_search/config.py`

- **load_config** (function, L121)
  - calls: _create_default_config
  - called by: _HotReloadableConfig+__init__+_get_mtime+1more, _run_server+main, cmd_call_graph_stats, cmd_generate_wiki, cmd_generate_wiki_plan, cmd_lookup_wiki, cmd_reindex, cmd_remove_project
- **_create_default_config** (function, L202)
  - called by: load_config

### `src/hybrid_search/server.py`

- **_HotReloadableConfig+__init__+_get_mtime+1more** (merged, L28)
  - calls: load_config
- **create_server** (function, L58)
  - calls: _openai_embed_request, handle_hybrid_search, hybrid_search, index_project
  - called by: _run_server+main
- **_run_server+main** (merged, L156)
  - calls: create_server, load_config

## Related Modules
- [[HANDOFF (isolated)]]
- [[benchmarks]]
- [[design (isolated)]]

- [[CLAUDE (isolated)]]
- [[index]]
- [[search]]
- [[storage]]
- [[tests]]
- [[tools]]

## External Dependencies

**Calls out to:**
- `Embedder._openai_embed_request`
- `IndexingPipeline.index_project`
- `SearchOrchestrator.hybrid_search`
- `StoreDB._migrate_schema`
- `WikiStore.check_staleness`
- `WikiStore.compile_page`
- `WikiStore.find_indirectly_affected`
- `WikiStore.list_pages`
- `WikiStore.lookup_page`
- `WikiStore.refresh_page`

**Called by:**
- `tests/test_reranker.py::test_result_count_matches_response+test_result_fields_complete+TestConfigTomlParsing+2more`
