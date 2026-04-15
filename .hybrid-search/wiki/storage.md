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

- **WikiPage** (dataclass, L60)
  - Fields: id, project_id, query_key, title, content, tags, created_at, updated_at, accessed_at, access_count, version, stale, changed_files, linked_pages, synthesis_model, synthesis_version, synthesis_hash, last_synthesized_at
- **WikiStore** (class, L88)
  - Wiki page CRUD + dependency-based staleness detection
  - calls: _check_page_staleness, _evict_lru, _expand_graph, _sync_wikilinks
- **compile_page** (function, L93)
  - Store a wiki page with file dependency snapshots and wikilink extraction
  - Supports synthesis metadata (model, hash)
  - calls: _evict_lru, _sync_wikilinks
  - called by: finalize_module, cmd_generate_wiki, cmd_sync_wiki
- **lookup_page** (function, L167)
  - Find by normalized query or tag, updates access tracking
  - Automatically checks staleness and expands wikilink graph (2 hops)
  - calls: _check_page_staleness, _expand_graph
  - called by: cmd_lookup_wiki
- **check_staleness** (function, L220)
  - Check staleness for one page or all pages in a project
  - calls: _check_page_staleness
  - called by: _auto_prepare_synthesis, _mark_stale_wikis, cmd_stale, cmd_synthesize_wiki_part1
- **refresh_page** (function, L249)
  - Update page content and re-snapshot file hashes
  - Re-syncs wikilinks from updated content
  - calls: _sync_wikilinks
  - called by: cmd_verify_synthesis
- **list_pages** (function, L310)
  - called by: cmd_synthesize_wiki_part1, cmd_verify_synthesis, cmd_verify_wiki
- **get_page_row+find_page_by_title+get_page_file_hashes+get_page_deps+get_linked_page_ids+get_page_title_and_content+is_synthesized+get_synthesis_hash** (public helpers, L335-L401)
  - Helper methods for external consumers (synthesizer, CLI)
- **find_indirectly_affected** (function, L402)
  - Find pages linked to stale pages (1-hop neighbors) that are NOT themselves stale
  - Used for partial wiki regeneration of "Related Modules" sections
  - calls: _expand_graph
  - called by: _auto_prepare_synthesis
- **_check_page_staleness** (function, L431)
  - Detects three types of staleness:
    1. File modified (hash changed)
    2. File deleted/moved (hash is NULL via LEFT JOIN)
    3. New files added to covered directories (via last_modified > wiki updated_at)
  - Zero dependencies → stale with "(all dependencies lost)" marker
  - called by: WikiStore, check_staleness, lookup_page, should_skip_synthesis
- **_sync_wikilinks** (function, L497)
  - Parse [[link_text]] from content, resolve by title or query_key, upsert wiki_links
  - called by: WikiStore, compile_page, refresh_page
- **_expand_graph** (function, L539)
  - BFS from start page following wikilinks up to max_hops (default 2, max_pages 10)
  - Follows both incoming and outgoing links
  - called by: WikiStore, find_indirectly_affected, lookup_page
- **_evict_lru** (function, L623)
  - Evict oldest-accessed pages when over max_pages limit
  - called by: WikiStore, compile_page

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
