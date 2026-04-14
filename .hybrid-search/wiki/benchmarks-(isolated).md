# Benchmarks (Isolated)
> synthesized: 2026-04-14

## Overview

Benchmarks (Isolated) covers the data files and isolated code chunks from the benchmarks directory — including `query_set.json` (ground truth query-passage pairs), `benchmark_micro_results.json` (cached evaluation results), and isolated benchmark utility functions. These provide the evaluation infrastructure and test data for measuring embedding model quality and search accuracy across Korean-English cross-language retrieval.

## Key Design Decisions

- **External JSON ground truth**: Golden query-passage pairs stored in `query_set.json` as structured data separate from benchmark scripts, enabling reuse across different evaluation scripts (`benchmarks/query_set.json`)
- **Three-category language breakdown**: Queries categorized as korean_nl, english_nl, and mixed to isolate cross-language performance (`benchmarks/run_benchmark_micro.py`)
- **Cached micro results**: `benchmark_micro_results.json` preserves evaluation results on disk so historical comparisons don't require re-running expensive model evaluations
- **Deterministic random seed**: Some benchmarks use fixed seeds for reproducible distractor generation

## Data Flow

```
query_set.json (golden pairs)
  → benchmark scripts load and evaluate
    → per-model metrics (R@1, R@3, MRR)
      → benchmark_micro_results.json (cached)
        → human analysis for model selection
```

## Caveats

- No schema validation on query_set.json or results JSON — malformed entries silently produce incorrect metrics
- Cached results in benchmark_micro_results.json may be stale if golden pairs or models change
- Platform-dependent memory measurement (macOS-specific `resource.getrusage`)

## Related Modules

- [[benchmarks]] -- graph-connected benchmark scripts that use these data files
- [[run-benchmark-micro]] -- micro benchmark script consuming golden pairs
- [[embedder----openai-api-backend]] -- production embedder selected via benchmark results

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `benchmarks/benchmark_micro_results.json`
- `benchmarks/query_set.json`
- `benchmarks/run_benchmark.py`
- `benchmarks/run_benchmark_fast.py`
- `benchmarks/run_benchmark_v2.py`

## Symbols

### `benchmarks/benchmark_micro_results.json`

- **benchmark_micro_results:L1-L164** (block, L1)
- **benchmark_micro_results:L164-L335** (block, L164)
- **benchmark_micro_results:L335-L502** (block, L335)
- **benchmark_micro_results:L502-L645** (block, L502)

### `benchmarks/query_set.json`

- **query_set:L1-L75** (block, L1)
- **query_set:L75-L157** (block, L75)
- **query_set:L157-L219** (block, L157)

### `benchmarks/run_benchmark.py`

- **anonymous_L57+load_query_set+get_process_memory_mb** (merged, L57)

### `benchmarks/run_benchmark_fast.py`

- **anonymous_L42+load_qs** (merged, L42)

### `benchmarks/run_benchmark_v2.py`

- **anonymous_L56+get_mem_mb** (merged, L56)

</details>