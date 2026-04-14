# Storage (Isolated)
> synthesized: 2026-04-14

## Overview

Storage (Isolated) covers the isolated code chunks from `storage/db.py` and `storage/indexes.py` that don't have incoming call graph edges from other modules. These include the core `StoreDB` class implementation, WAL mode initialization, schema migration, and the `IndexPaths` utility for per-project directory management. While functionally integral to the system, they appear isolated in the call graph because they are entry points called by the pipeline and CLI rather than being called by other graph-connected modules.

## Key Design Decisions

- **WAL mode with explicit transaction management**: `isolation_level=None` for Python 3.13 compatibility, with explicit `BEGIN IMMEDIATE` / `COMMIT` via `transaction()` context manager (`src/hybrid_search/storage/db.py:L30`)
- **`ON CONFLICT DO UPDATE` pattern**: Avoids `INSERT OR REPLACE` which triggers DELETE+INSERT internally, cascading FK constraints (`src/hybrid_search/storage/db.py`)
- **Int-based schema version comparison**: `_migrate_schema()` converts version to int before comparison to avoid string ordering issues (e.g., "9" < "10" is False as strings) (`src/hybrid_search/storage/db.py`)
- **IndexPaths as thin wrapper**: Simple dataclass-like path calculator without filesystem state, enabling easy testing and path prediction (`src/hybrid_search/storage/indexes.py`)

## Data Flow

```
StoreDB.__init__(db_path)
  → PRAGMA journal_mode=WAL, foreign_keys=ON
    → _init_schema() → CREATE TABLE IF NOT EXISTS (6 tables)
      → _migrate_schema() → version check → ALTER TABLE if needed

IndexPaths(project_dir)
  → .store_db, .tantivy_dir, .vectors_dir, .lock_file
    → .ensure_dirs() creates tantivy/ + vectors/
    → .delete_all() removes entire project_dir
```

## Caveats

- `_conn` direct access from external code bypasses transaction safety — all external access should use public methods or `transaction()` context manager
- Schema migration is append-only (ALTER TABLE ADD COLUMN); column removal or type changes require manual intervention
- `delete_all()` uses `shutil.rmtree()` which is irreversible and removes the entire project directory including all three stores

## Related Modules

- [[storage-layer]] -- architectural overview of the triple-store system
- [[indexing-pipeline]] -- primary consumer of StoreDB write operations
- [[wiki-system]] -- WikiStore wraps StoreDB for wiki-specific operations

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `src/hybrid_search/storage/db.py`
- `src/hybrid_search/storage/indexes.py`

## Symbols

### `src/hybrid_search/storage/db.py`

- **_confidence_filter+anonymous_L116+anonymous_L128+11more** (merged, L19)
- **delete_file+get_all_file_paths+insert_chunks+11more** (merged, L289)
- **get_callers_by_name** (function, L436)
- **get_all_call_edges+update_call_edge_resolution+find_chunk_by_qualified_name+2more** (merged, L518)

### `src/hybrid_search/storage/indexes.py`

- **IndexPaths+__init__+anonymous_L16+6more** (merged, L9)

</details>