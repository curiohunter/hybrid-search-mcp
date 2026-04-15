# Storage (Isolated)

**Files**: 3 | **Symbols**: 9

## Files

- `src/hybrid_search/storage/db.py`
- `src/hybrid_search/storage/indexes.py`
- `src/hybrid_search/storage/wiki.py`

## Symbols

### `src/hybrid_search/storage/db.py`

- **anonymous_L223+close+wiki_store+5more** (merged, L223)
- **delete_file+get_all_file_paths+insert_chunks+12more** (merged, L341)
- **get_callers** (function, L457)
- **get_callers_by_name** (function, L495)
- **get_callees** (function, L535)
- **get_all_call_edges+update_call_edge_resolution+find_chunk_by_qualified_name+3more** (merged, L577)

### `src/hybrid_search/storage/indexes.py`

- **IndexPaths+__init__+anonymous_L16+6more** (merged, L9)

### `src/hybrid_search/storage/wiki.py`

- **delete_page** (function, L305)
  - Delete a wiki page (dependencies cascade)
- **get_page_row+find_page_by_title+get_page_file_hashes+get_page_deps+get_linked_page_ids+get_page_title_and_content+is_synthesized+get_synthesis_hash** (merged, L335)
  - Public helper methods for external consumers (synthesizer, CLI)
- **_check_page_staleness** (function, L431)
  - Three staleness types: file modified, file deleted (LEFT JOIN NULL), new files in covered dirs (last_modified comparison)
  - Zero-dependency pages marked stale with "(all dependencies lost)"
