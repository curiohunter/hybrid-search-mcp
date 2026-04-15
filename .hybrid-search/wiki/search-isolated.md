# Search (Isolated)

**Files**: 4 | **Symbols**: 6

## Files

- `src/hybrid_search/search/bm25.py`
- `src/hybrid_search/search/fusion.py`
- `src/hybrid_search/search/orchestrator.py`
- `src/hybrid_search/search/vector.py`

## Symbols

### `src/hybrid_search/search/bm25.py`

- **_build_schema** (function, L57)
  - Fields: chunk_id (stored, raw), name (stored), qualified_name (stored), content, docstring
- **_get_writer** (function, L66)
  - Lazy writer init, heap_size=50MB
- **add** (function, L71)
  - Deduplication: deletes existing doc with same chunk_id before adding new one
- **delete+delete_batch+commit** (merged, L92)
- **count** (property, L142)
  - Approximate document count via searcher.num_docs
- **_escape_tantivy_query** (function, L156)
  - Escapes Tantivy query syntax special characters

### `src/hybrid_search/search/fusion.py`

- **anonymous_L8** (function, L8)

### `src/hybrid_search/search/orchestrator.py`

- **QueryType** (class, L30)
- **anonymous_L251** (function, L251)

### `src/hybrid_search/search/vector.py`

- **anonymous_L114+save** (merged, L114)
