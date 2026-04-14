# Search

**Files**: 4 | **Symbols**: 16

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
