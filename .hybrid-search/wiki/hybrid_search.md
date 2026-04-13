# Hybrid Search

**Files**: 3 | **Symbols**: 15

## Files

- `src/hybrid_search/cli.py`
- `src/hybrid_search/config.py`
- `src/hybrid_search/server.py`

## Entry Points

- `src/hybrid_search/cli.py::main`
- `src/hybrid_search/server.py::_HotReloadableConfig+__init__+_get_mtime+1more`
- `src/hybrid_search/server.py::_run_server+main`
- `src/hybrid_search/server.py::create_server_part1`

## Symbols

### `src/hybrid_search/cli.py`

- **cmd_reindex** (function, L54)
  - calls: _mark_stale_wikis, _ollama_embed_request, cmd_sync_wiki, index_project, load_config
  - called by: main
- **_mark_stale_wikis** (function, L116)
  - calls: check_staleness, upsert_file
  - called by: cmd_reindex
- **cmd_status** (function, L138)
  - calls: load_config
  - called by: main
- **cmd_stale** (function, L155)
  - calls: check_staleness, load_config, upsert_file
  - called by: main
- **cmd_sync_wiki** (function, L194)
  - calls: _resolve_wiki_deps, compile_page, load_config, upsert_file
  - called by: cmd_reindex, main
- **_resolve_wiki_deps** (function, L282)
  - called by: cmd_sync_wiki
- **cmd_call_graph_stats** (function, L306)
  - calls: load_config, upsert_file
  - called by: main
- **cmd_install_hook** (function, L356)
  - called by: main
- **main** (function, L407)
  - calls: cmd_call_graph_stats, cmd_install_hook, cmd_reindex, cmd_stale, cmd_status, cmd_sync_wiki

### `src/hybrid_search/config.py`

- **load_config** (function, L112)
  - calls: _create_default_config
  - called by: TestDefaultConfig+test_default_data_dir+test_default_log_level+20more, _HotReloadableConfig+__init__+_get_mtime+1more, _run_server+main, cmd_call_graph_stats, cmd_reindex, cmd_stale, cmd_status, cmd_sync_wiki
- **_create_default_config** (function, L180)
  - called by: load_config

### `src/hybrid_search/server.py`

- **_HotReloadableConfig+__init__+_get_mtime+1more** (merged, L34)
  - calls: load_config
- **create_server_part1** (function, L64)
  - calls: _dispatch_tool, _ollama_embed_request, hybrid_search, index_project
- **_dispatch_tool** (function, L387)
  - calls: handle_check_wiki_staleness, handle_compile_to_wiki, handle_hybrid_search, handle_index_project, handle_index_status, handle_lookup_wiki, handle_refresh_wiki_page, handle_search_symbols
  - called by: create_server_part1
- **_run_server+main** (merged, L509)
  - calls: load_config

## External Dependencies

**Calls out to:**
- `Embedder._ollama_embed_request`
- `IndexingPipeline.index_project`
- `SearchOrchestrator.hybrid_search`
- `StoreDB.upsert_file`
- `WikiStore.check_staleness`
- `WikiStore.compile_page`
- `src/hybrid_search/tools/hybrid_search.py::handle_hybrid_search`
- `src/hybrid_search/tools/index.py::handle_index_project`
- `src/hybrid_search/tools/index.py::handle_index_status`
- `src/hybrid_search/tools/semantic_search.py::handle_semantic_search`

**Called by:**
- `TestLoadConfig.test_load_custom_config`
- `tests/test_config.py::TestDefaultConfig+test_default_data_dir+test_default_log_level+20more`
- `tests/test_config.py::test_partial_config_uses_defaults+test_data_dir_expansion+test_empty_projects_list`
