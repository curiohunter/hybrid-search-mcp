# Storage

**Files**: 2 | **Symbols**: 14

## Files

- `src/hybrid_search/storage/db.py`
- `src/hybrid_search/storage/wiki.py`

## Entry Points

- `StoreDB.upsert_file`
- `WikiStore.check_staleness`
- `WikiStore.compile_page`
- `WikiStore.find_indirectly_affected`
- `WikiStore.list_pages`

## Symbols

### `src/hybrid_search/storage/db.py`

- **_confidence_filter+anonymous_L132+anonymous_L144+3more** (merged, L19)
  - calls: _migrate_schema
- **_migrate_schema** (function, L190)
  - called by: _auto_prepare_synthesis, _confidence_filter+anonymous_L132+anonymous_L144+3more, _enrich_results, _mark_stale_wikis, _search_cross_project, _search_single, anonymous_L29+anonymous_L41, anonymous_L97+anonymous_L114+SearchOrchestrator+1more
- **upsert_file** (function, L314)
  - called by: anonymous_L46

### `src/hybrid_search/storage/wiki.py`

- **_extract_snippet+normalize_query+_page_id+5more** (merged, L20)
  - calls: _check_page_staleness, _evict_lru, _expand_graph, _sync_wikilinks
- **compile_page** (function, L93)
  - calls: _evict_lru, _sync_wikilinks
  - called by: anonymous_L46, cmd_generate_wiki, cmd_sync_wiki, finalize_module, test_compile_with_synthesis_meta, test_finalize_missing_module+TestWikiStoreSynthesis, test_finds_linked_pages, test_no_skip_for_missing_module+TestGetSynthesisHash+test_returns_none_when_not_synthesized+2more
- **lookup_page** (function, L167)
  - calls: _check_page_staleness, _expand_graph
  - called by: cmd_lookup_wiki, test_compile_with_synthesis_meta, test_compile_without_synthesis_meta+TestSchemaMigration+test_fresh_db_has_synthesis_columns+4more, test_creates_parent_dirs+TestFinalizeModule, test_empty_when_no_links+TestVerifySymbols+test_finds_existing_symbols+6more, test_finalize_missing_module+TestWikiStoreSynthesis, test_finalize_updates_db_synthesis_meta, test_finds_linked_pages
- **check_staleness** (function, L220)
  - calls: _check_page_staleness
  - called by: _auto_prepare_synthesis, _mark_stale_wikis, cmd_stale, cmd_synthesize_wiki_part1, cmd_verify_wiki
- **refresh_page** (function, L244)
  - calls: _sync_wikilinks
  - called by: cmd_verify_synthesis
- **list_pages** (function, L310)
  - called by: cmd_synthesize_wiki_part1, cmd_verify_synthesis, cmd_verify_wiki
- **find_indirectly_affected** (function, L402)
  - calls: _expand_graph
  - called by: _auto_prepare_synthesis, test_empty_when_no_links+TestVerifySymbols+test_finds_existing_symbols+6more, test_finds_linked_pages, test_no_skip_for_missing_module+TestGetSynthesisHash+test_returns_none_when_not_synthesized+2more
- **_check_page_staleness** (function, L431)
  - called by: _extract_snippet+normalize_query+_page_id+5more, check_staleness, lookup_page, should_skip_synthesis
- **_sync_wikilinks** (function, L457)
  - called by: _extract_snippet+normalize_query+_page_id+5more, compile_page, refresh_page
- **_expand_graph** (function, L499)
  - called by: _extract_snippet+normalize_query+_page_id+5more, find_indirectly_affected, lookup_page
- **_evict_lru** (function, L583)
  - called by: _extract_snippet+normalize_query+_page_id+5more, compile_page

## Related Modules
- [[HANDOFF (isolated)]]
- [[benchmarks]]
- [[tools]]

- [[hybrid_search]]
- [[index]]
- [[search]]
- [[tests]]

## External Dependencies

**Called by:**
- `SearchOrchestrator._enrich_results`
- `SearchOrchestrator._search_cross_project`
- `SearchOrchestrator._search_single`
- `TestFinalizeModule.test_finalize_updates_db_synthesis_meta`
- `TestFindIndirectlyAffected.test_finds_linked_pages`
- `TestWikiStoreSynthesis.test_compile_with_synthesis_meta`
- `src/hybrid_search/cli.py::_auto_prepare_synthesis`
- `src/hybrid_search/cli.py::_mark_stale_wikis`
- `src/hybrid_search/cli.py::cmd_call_graph_stats`
- `src/hybrid_search/cli.py::cmd_generate_wiki`
