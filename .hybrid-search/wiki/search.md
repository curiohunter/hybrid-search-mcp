# Search
> synthesized: 2026-04-14

## Overview

The Search module implements the hybrid BM25 + vector search pipeline that is the core retrieval capability of the MCP server. It combines Tantivy-based full-text BM25 scoring with USearch HNSW vector similarity search, fuses results via Reciprocal Rank Fusion (RRF), and supports cross-language queries (Korean/English) through automatic query classification and adaptive weight tuning. This module coordinates single-project and cross-project search with working-directory-aware boosting.

## Key Design Decisions

- **3-class query classifier drives weight selection**: Queries are classified as `EXACT_SYMBOL` (camelCase/snake_case), `KOREAN_NL` (>50% Korean chars), or `ENGLISH_NL`, each mapped to a preset BM25 weight. Mixed Korean+symbol queries get a middle weight of 0.4, balancing keyword and semantic search (`src/hybrid_search/search/orchestrator.py:L55-L519`).
- **RRF with asymmetric weighting**: The standard RRF formula `weight / (k + rank)` is extended with a `bm25_weight` parameter so the BM25 and vector contributions can be independently tuned per query type, rather than treating both lists equally (`src/hybrid_search/search/fusion.py:L16`).
- **3x retrieval depth before fusion**: Both BM25 and vector engines retrieve `limit * 3` candidates before RRF fusion and truncation, ensuring sufficient candidate overlap for effective rank fusion ().
- **CWD-aware cross-project boosting**: When searching across multiple projects, the project matching `cwd` gets 2:1 BM25 interleave priority and a 5% cosine similarity boost for vector results, keeping local-project results prominent without excluding others (-L778`).
- **Cross-project BM25 merge via round-robin interleave**: Rather than merging BM25 scores directly (which are not comparable across indices), per-project ranked lists are interleaved round-robin to preserve rank ordering within each index (`src/hybrid_search/search/orchestrator.py:L432`).
- **USearch cosine distance-to-similarity conversion**: USearch returns `1 - similarity` as distance; the engine converts back to similarity scores for consistent downstream use ().
- **BM25 schema mismatch auto-recovery**: If the Tantivy index has a schema mismatch (e.g., after field changes), it is silently deleted and recreated in write mode (-L370`).
- **Tantivy query escaping fallback**: If `parse_query` fails on special characters, the query is escaped and retried before returning empty results (-L444`).

## Data Flow

```
User Query (Korean or English)
   |
   v
classify_query()              -- EXACT_SYMBOL / KOREAN_NL / ENGLISH_NL
get_bm25_weight()             -- preset or explicit weight
   |
   v
Embedder.embed_query()        -- query -> vector (once)
   |
   +---> BM25Engine.search()       -- Tantivy full-text
   |         |
   |         v  ranked chunk_ids (BM25)
   |
   +---> VectorEngine.search()     -- USearch HNSW cosine
   |         |
   |         v  ranked chunk_ids (vector)
   |
   v  (cross-project: interleave + boost)
   |
reciprocal_rank_fusion()      -- weighted RRF merge
   |
   v  FusedResult[] (sorted by rrf_score)
   |
_enrich_results()             -- chunk metadata lookup from StoreDB
   |
   v
HybridSearchResponse { results, query_type, effective_bm25_weight, ... }
```

## Caveats

- **`_build_filter` loads all project chunks into memory**: When `file_pattern` or `node_types` are specified, all chunks for the project are fetched to build a filter set, which could be expensive for very large projects ().
- **Vector key mapping uses `allow_pickle=True`**: The numpy `.npz` loader for key mappings uses `allow_pickle=True`, which is a security concern if index files could be tampered with ().
- **BM25Engine silently returns empty on broken index**: In read-only mode, if the index cannot be opened, `self._index` is set to `None` and all searches return empty results with no error propagation (-L364`).
- **Cross-project search uses ThreadPoolExecutor with hard timeout**: Projects that exceed `PROJECT_TIMEOUT_S` are silently skipped and added to `skipped_projects`, which may not be noticed by callers (-L760`).
- **`_enrich_results` iterates all project DBs per fused result**: For each fused result, it linearly scans all project DB connections until the chunk is found, which is O(results * projects) (-L826`).
- **VectorEngine re-add removes then inserts**: When updating an existing chunk vector, the old key is removed and a new monotonically-increasing key is allocated, which could fragment the HNSW index over many updates (-L181`).

## Related Modules

- [[tools]] -- `handle_hybrid_search` and `handle_semantic_search` delegate to `SearchOrchestrator` and `VectorEngine`
- [[call-graph-&-module-tree]] -- call edges stored in StoreDB are indexed alongside the search data
- [[search-(isolated)]] -- `config.py` provides search configuration (`rrf_k`, weights); `server.py` creates `SearchOrchestrator`

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `src/hybrid_search/search/bm25.py`
- `src/hybrid_search/search/fusion.py`
- `src/hybrid_search/search/orchestrator.py`
- `src/hybrid_search/search/vector.py`

## Entry Points

- `SearchOrchestrator.hybrid_search`
- `src/hybrid_search/search/orchestrator.py::anonymous_L97+anonymous_L114+SearchOrchestrator+1more`
- `src/hybrid_search/search/vector.py::anonymous_L19+VectorEngine+__init__+4more`

## Symbols

### `src/hybrid_search/search/bm25.py`

- **__init__** (function, L32)
  - called by: _search_cross_project, _search_single, anonymous_L33+IndexingPipeline+__init__, anonymous_L97+anonymous_L114+SearchOrchestrator+1more, index_project

### `src/hybrid_search/search/fusion.py`

- **reciprocal_rank_fusion** (function, L16)
  - called by: TestReciprocalRankFusion+test_basic_fusion_both_lists+test_scores_are_descending+11more, anonymous_L97+anonymous_L114+SearchOrchestrator+1more, hybrid_search

### `src/hybrid_search/search/orchestrator.py`

- **classify_query** (function, L55)
  - called by: TestClassifyQuery+test_camel_case+test_camel_case_multi_word+23more, get_bm25_weight
- **get_bm25_weight** (function, L78)
  - calls: classify_query
  - called by: TestClassifyQuery+test_camel_case+test_camel_case_multi_word+23more, anonymous_L97+anonymous_L114+SearchOrchestrator+1more, hybrid_search
- **anonymous_L97+anonymous_L114+SearchOrchestrator+1more** (merged, L97)
  - calls: __init__, _build_filter, _enrich_results, _interleave_round_robin, _make_snippet, _search_cross_project, _search_single, _weighted_interleave
- **hybrid_search** (function, L132)
  - calls: _enrich_results, _search_cross_project, _search_single, get_bm25_weight, reciprocal_rank_fusion
  - called by: create_server_part1, handle_hybrid_search
- **_search_single** (function, L206)
  - calls: __init__, _build_filter, search, upsert_file
  - called by: anonymous_L97+anonymous_L114+SearchOrchestrator+1more, hybrid_search
- **_search_cross_project** (function, L265)
  - calls: __init__, _build_filter, _interleave_round_robin, _weighted_interleave, search, upsert_file
  - called by: anonymous_L97+anonymous_L114+SearchOrchestrator+1more, hybrid_search
- **_enrich_results** (function, L354)
  - calls: _make_snippet, upsert_file
  - called by: anonymous_L97+anonymous_L114+SearchOrchestrator+1more, hybrid_search
- **_weighted_interleave** (function, L403)
  - called by: TestWeightedInterleave+test_basic_2_to_1+test_dedup+15more, _search_cross_project, anonymous_L97+anonymous_L114+SearchOrchestrator+1more
- **_interleave_round_robin** (function, L432)
  - called by: TestWeightedInterleave+test_basic_2_to_1+test_dedup+15more, _search_cross_project, anonymous_L97+anonymous_L114+SearchOrchestrator+1more
- **_build_filter** (function, L447)
  - called by: _search_cross_project, _search_single, anonymous_L97+anonymous_L114+SearchOrchestrator+1more
- **_make_snippet** (function, L480)
  - called by: _enrich_results, anonymous_L97+anonymous_L114+SearchOrchestrator+1more

### `src/hybrid_search/search/vector.py`

- **anonymous_L19+VectorEngine+__init__+4more** (merged, L19)
  - calls: _load, index_project, search
- **search** (function, L81)
  - called by: _search_cross_project, _search_single, anonymous_L19+VectorEngine+__init__+4more, anonymous_L33+IndexingPipeline+__init__, anonymous_L97+anonymous_L114+SearchOrchestrator+1more, evaluate, evaluate, evaluate_model
- **_load** (function, L135)
  - calls: index_project
  - called by: anonymous_L19+VectorEngine+__init__+4more

## External Dependencies

**Calls out to:**
- `IndexingPipeline.index_project`
- `StoreDB.upsert_file`

**Called by:**
- `IndexingPipeline.index_project`
- `benchmarks/run_benchmark.py::evaluate_model`
- `benchmarks/run_benchmark_fast.py::evaluate`
- `benchmarks/run_benchmark_v2.py::evaluate`
- `src/hybrid_search/index/pipeline.py::anonymous_L33+IndexingPipeline+__init__`
- `src/hybrid_search/server.py::create_server_part1`
- `src/hybrid_search/tools/hybrid_search.py::handle_hybrid_search`
- `src/hybrid_search/tools/semantic_search.py::handle_semantic_search`
- `tests/test_cwd_boost.py::TestWeightedInterleave+test_basic_2_to_1+test_dedup+15more`
- `tests/test_fusion.py::TestReciprocalRankFusion+test_basic_fusion_both_lists+test_scores_are_descending+11more`

</details>