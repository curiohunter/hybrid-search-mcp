# Hybrid Search (Isolated)
> synthesized: 2026-04-14

## Overview

Hybrid Search (Isolated) covers the same four core files (cli.py, config.py, project.py, server.py) from an isolated call-graph perspective — code chunks without incoming edges, serving as top-level entry points. This includes the registry upsert semantics, mutable dict closures for server state management, automatic wiki sync after reindex, and the wiki plan JSON persistence for downstream synthesis.

## Key Design Decisions

- **Registry upsert semantics**: `ProjectRegistry.register()` uses INSERT OR REPLACE on the project name, allowing path updates without duplicate entries (`src/hybrid_search/project.py`)
- **Mutable dict closure for server state**: Server resources (orchestrator, pipeline, embedder) are captured in a dict closure passed to tool handlers, enabling hot-reload to swap references (`src/hybrid_search/server.py`)
- **Wiki plan as JSON**: `generate-wiki-plan` writes `wiki-plan.json` to `.hybrid-search/`, making the module tree available for downstream CLI commands and skills (`src/hybrid_search/cli.py`)

## Data Flow

```
config.toml (TOML)
  → load_config() → HybridSearchConfig (frozen dataclasses)
    → EmbeddingConfig, SearchConfig, IndexingConfig, WikiConfig
      → consumed by Embedder, Pipeline, Orchestrator, WikiStore

project_registry.db (SQLite)
  → ProjectRegistry → register/get_by_name/list_all
    → per-project store.db path resolution via project hash
```

## Caveats

- ProjectRegistry SQLite connections may not be explicitly closed in server.py's long-running process
- Config hot-reload replaces the entire Embedder if embedding settings change, requiring re-fetching the API key
- wiki-plan.json on disk may become stale if reindex changes the module structure without regenerating the plan

## Related Modules

- [[hybrid-search]] -- graph-connected view of the same files
- [[configuration-&-project-management]] -- detailed config system documentation
- [[mcp-server-&-cli]] -- MCP server and CLI interface

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `src/hybrid_search/cli.py`
- `src/hybrid_search/config.py`
- `src/hybrid_search/project.py`
- `src/hybrid_search/server.py`

## Symbols

### `src/hybrid_search/cli.py`

- **_detect_project+_write_gap_flag** (merged, L27)
- **_extract_title+_extract_tags** (merged, L261)

### `src/hybrid_search/config.py`

- **anonymous_L41+anonymous_L62+anonymous_L70+2more** (merged, L41)
- **anonymous_L89** (function, L89)

### `src/hybrid_search/project.py`

- **anonymous_L25+project_hash+ProjectRegistry+10more** (merged, L25)

### `src/hybrid_search/server.py`

- **create_server_part4** (function, L64)
- **create_server_part3** (function, L64)
- **create_server_part2** (function, L64)
- **create_server_part6** (function, L64)
- **create_server_part5** (function, L64)

</details>