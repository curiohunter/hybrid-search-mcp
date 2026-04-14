# Tests (Isolated)
> synthesized: 2026-04-14

## Overview

The `tests/` directory contains two test files (`test_callgraph.py` and `test_embedder.py`) that validate the call graph resolution pipeline and the OpenAI embedding backend. `test_callgraph.py` covers the 3-tier confidence resolution (`_resolve_single`), import-call binding for TypeScript and Python, self/this method resolution, common-name suppression, same-file preference, module matching, and integration tests for `resolve_call_edges`. `test_embedder.py` validates the `Embedder` class construction, OpenAI API request formatting, L2 normalization, error handling, and the `_HotReloadableConfig` hot-reload mechanism including mtime-based reload and invalid TOML resilience.

## Key Design Decisions

- **3-tier confidence testing strategy**: Tests explicitly verify high/medium/low confidence outputs of `_resolve_single`, ensuring that module-qualified calls get "high", single-candidate name matches get "medium", and common names like "init" stay "low" (`tests/test_callgraph.py:L299-L332`)
- **Same-file preference assertion**: When multiple candidates exist for a callee name, the resolver should prefer the one in the same file as the caller -- this is a non-obvious heuristic tested explicitly (`tests/test_callgraph.py:L141`)
- **COMMON_NAMES frozen set verification**: `TestCommonNames` validates that the common names set is frozen (immutable) and contains expected entries, preventing accidental mutation during resolution (`tests/test_callgraph.py:L613`)
- **No live API calls in embedder tests**: All OpenAI API interactions are mocked via `unittest.mock.patch` on `urllib.request.urlopen`, making tests runnable without an API key (`tests/test_embedder.py:L46`)
- **Hot-reload resilience to invalid config**: `test_reload_survives_invalid_toml` verifies that the server keeps the previous valid config when a TOML parse fails, preventing runtime crashes from config file corruption ()

## Data Flow

```
test_callgraph.py:
  _make_db(tmp_path)  -->  StoreDB (in-memory SQLite)
       |
  _seed_db(db)        -->  files + chunks + unresolved call_edges
       |
  resolve_call_edges  -->  3-tier resolution (high/medium/low)
       |
  assertions          -->  confidence levels, chunk_id matches

test_embedder.py:
  EmbeddingConfig()   -->  Embedder(cfg)
       |
  mock urlopen        -->  _openai_embed_request / _embed_all
       |
  assertions          -->  URL, normalization, error messages

  _HotReloadableConfig:
  config.toml (tmp)   -->  check_reload()  -->  mtime comparison
       |                                         |
  write new content   -->  reload or keep previous config
```

## Caveats

- **time.sleep in hot-reload tests**: `test_reload_when_mtime_changes` and `test_reload_survives_invalid_toml` use `time.sleep(0.05)` plus `os.utime` to force mtime changes, which could be flaky on fast filesystems where mtime resolution is coarse ()
- **Integration tests depend on tree-sitter**: `TestImportCallBinding` tests (`test_import_call_binding_ts`, `test_import_call_binding_python`) call `chunk_code_file` which requires tree-sitter grammars to be installed; missing grammars will cause test failures (`tests/test_callgraph.py:L464`)
- **Idempotency test uses inequality**: `test_idempotent_resolution` asserts `stats2["unresolved"] <= stats1["unresolved"]` rather than exact equality, which could mask re-resolution bugs (`tests/test_callgraph.py:L418`)

## Related Modules

- [[storage-(isolated)]] -- tests create `StoreDB` instances and use `FileRecord`/`ChunkRecord` dataclasses
- [[search-(isolated)]] -- `_HotReloadableConfig` is imported from `hybrid_search.server`
- [[architecture]] -- call graph resolution is a core architectural feature tested here

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `tests/test_callgraph.py`
- `tests/test_embedder.py`

## Symbols

### `tests/test_callgraph.py`

- **TestCommonNames+test_common_names_contains_expected+test_common_names_is_frozen** (merged, L613)

### `tests/test_embedder.py`

- **test_reload_when_mtime_changes** (function, L94)
- **test_reload_survives_invalid_toml** (function, L112)

</details>