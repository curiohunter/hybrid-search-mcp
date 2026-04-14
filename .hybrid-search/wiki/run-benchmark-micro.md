# Run Benchmark Micro
> synthesized: 2026-04-14

## Overview

Run Benchmark Micro is a self-contained evaluation script that measures embedding model quality for cross-language retrieval. It tests multiple sentence-transformer models against 30 golden query-passage pairs across Korean NL, English NL, and mixed categories, computing Recall@1/3/5 and MRR metrics to determine which model best handles Korean-to-English code search (`benchmarks/run_benchmark_micro.py:L144`).

## Key Design Decisions

- **Cross-similarity matrix evaluation**: Builds a full NxN similarity matrix to simulate retrieval ranking, not just pairwise similarity (`benchmarks/run_benchmark_micro.py:L91`)
- **Per-category breakdown**: Results split by korean_nl, english_nl, and mixed so model selection can prioritize Korean cross-language performance (`benchmarks/run_benchmark_micro.py:L111`)
- **Model cleanup between runs**: `del model` + `gc.collect()` prevents memory accumulation (`benchmarks/run_benchmark_micro.py:L145`)
- **E5 query/passage prefix**: Queries prefixed with "query: " and passages with "passage: " following E5 convention (`benchmarks/run_benchmark_micro.py:L79`)

## Data Flow

```
GOLDEN_PAIRS (30 pairs, 3 categories)
  → SentenceTransformer.encode()
    → Cross-similarity matrix (30x30)
      → Rank computation → R@1, R@3, R@5, MRR
        → Per-category aggregation
          → benchmark_micro_results.json
```

## Caveats

- Hardcodes `trust_remote_code=True` when loading models — executes arbitrary code from HuggingFace (`benchmarks/run_benchmark_micro.py:L77`)
- Only 30 golden pairs — small changes in 2-3 query rankings can swing R@1 by 10%+
- Catches all model exceptions and continues, potentially masking silently failing models (`benchmarks/run_benchmark_micro.py:L166`)

## Related Modules

- [[benchmarks]] -- other benchmark scripts for end-to-end search quality evaluation
- [[embedder----openai-api-backend]] -- production embedder using the model selected by these benchmarks

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `benchmarks/run_benchmark_micro.py`

## Entry Points

- `benchmarks/run_benchmark_micro.py::main`

## Symbols

### `benchmarks/run_benchmark_micro.py`

- **test_model** (function, L67)
  - called by: main
- **main** (function, L144)
  - calls: test_model

</details>