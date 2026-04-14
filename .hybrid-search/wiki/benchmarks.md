# Benchmarks
> synthesized: 2026-04-14

## Overview

The Benchmarks module provides three standalone scripts for evaluating embedding model quality on the hybrid-search codebase itself. It exists to answer a critical question: which embedding model delivers the best Recall@10 and MRR for Korean-English cross-language code search? The scripts (`run_benchmark.py`, `run_benchmark_fast.py`, `run_benchmark_v2.py`) each take a slightly different approach to chunking scope and evaluation speed, but all share the same core pattern: chunk the project, embed chunks with SentenceTransformer models, build a temporary VectorEngine index, run queries from `query_set.json`, and measure recall/MRR broken down by language category (korean_nl, english_nl, mixed).

## Key Design Decisions

- **Three benchmark variants for different tradeoffs**: `run_benchmark.py` chunks the entire project (slowest, most realistic), `run_benchmark_v2.py` filters to `BENCHMARK_DIRS` and `BENCHMARK_EXTENSIONS` for targeted evaluation, and `run_benchmark_fast.py` chunks only query-relevant files plus random distractors for speed (`benchmarks/run_benchmark.py:L88`, `benchmarks/run_benchmark_v2.py:L79`, `benchmarks/run_benchmark_fast.py:L58`)
- **Distractor-based fast benchmarking**: `build_chunk_set` in the fast variant adds `DISTRACTOR_COUNT` random non-relevant files to simulate noise, using `random.seed(42)` for reproducibility (-L395`)
- **Combined file + symbol recall metric**: Recall is computed as `max(file_recall, sym_recall)`, meaning a query succeeds if it finds either the expected file or the expected symbol name in the top-10 results -- this avoids penalizing models that find the right symbol in a differently-named file (`benchmarks/run_benchmark_v2.py:L228-L230`)
- **Query prefix convention**: All query embeddings use a `"query: "` prefix before encoding, following the asymmetric encoding convention used by many retrieval-focused embedding models (`benchmarks/run_benchmark_v2.py:L187`)
- **Temporary VectorEngine for each model**: Each model evaluation creates a fresh `VectorEngine` in a `tempfile.TemporaryDirectory`, ensuring no cross-contamination between model evaluations (`benchmarks/run_benchmark_v2.py:L195-L196`)
- **Explicit model cleanup with gc.collect()**: After each model evaluation, the model is deleted and garbage collected to free GPU/CPU memory before loading the next model (`benchmarks/run_benchmark_v2.py:L264-L265`)

## Data Flow

```
query_set.json ──────► queries[] + project_path
                              │
                              ▼
                    chunk_project(project_path)
                    ├── _build_ignore_spec()
                    ├── _walk_files()
                    ├── chunk_code_file()  ◄── from ast_chunker
                    └── chunk_doc_file()   ◄── from doc_chunker
                              │
                              ▼
                        chunks[]
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         Model A          Model B         Model C
     SentenceTransformer  (etc.)          (etc.)
              │
              ▼
    VectorEngine (temp dir)
        .add(chunk_id, embedding)
              │
              ▼
    for query in queries:
        .search(query_emb, limit=10)
              │
              ▼
    Recall@10, MRR (by category)
              │
              ▼
    benchmark_results_*.json
```

## Caveats

- **`query_set.json` contains a hardcoded `project_path`**: The benchmark assumes the project path in the query set file is valid on the current machine; this will break if the benchmark is run on a different system (`benchmarks/run_benchmark_v2.py:L257`)
- **Memory measurement is macOS-specific**: `get_mem_mb()` divides `ru_maxrss` by `1024 * 1024`, which is correct for macOS (bytes) but would be wrong on Linux (kilobytes) (`benchmarks/run_benchmark_v2.py:L108`, )
- **Silent exception swallowing during chunking**: Both `chunk_project` variants catch all exceptions during file chunking with a bare `except Exception: pass`, hiding potential parsing errors (`benchmarks/run_benchmark_v2.py:L163-L164`)
- **No deduplication of expected_files vs expected_symbols**: If a query has overlapping file and symbol expectations, the `max(file_recall, sym_recall)` approach may inflate scores for queries where only one dimension matches
- **`trust_remote_code=True` used for all models**: SentenceTransformer models are loaded with `trust_remote_code=True`, which could execute arbitrary Python from HuggingFace model repos (`benchmarks/run_benchmark_v2.py:L156`)

## Related Modules

- [[benchmarks-(isolated)]] -- the isolated view including data files (query_set.json, results JSON)
- [[hybrid-search]] -- provides the chunking pipeline (ast_chunker, doc_chunker, scanner) that benchmarks reuse

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `benchmarks/run_benchmark.py`
- `benchmarks/run_benchmark_fast.py`
- `benchmarks/run_benchmark_v2.py`

## Entry Points

- `benchmarks/run_benchmark.py::main`
- `benchmarks/run_benchmark_fast.py::main`
- `benchmarks/run_benchmark_v2.py::main`

## Symbols

### `benchmarks/run_benchmark.py`

- **chunk_project** (function, L88)
  - calls: _build_ignore_spec, _walk_files, chunk_code_file, chunk_doc_file
  - called by: main
- **create_embedder_for_model** (function, L115)
  - called by: evaluate_model
- **evaluate_model** (function, L160)
  - calls: create_embedder_for_model, search
  - called by: main
- **main** (function, L252)
  - calls: chunk_project, evaluate_model

### `benchmarks/run_benchmark_fast.py`

- **build_chunk_set** (function, L58)
  - calls: _build_ignore_spec, _walk_files, chunk_code_file
  - called by: main
- **evaluate** (function, L94)
  - calls: search
  - called by: main
- **main** (function, L180)
  - calls: build_chunk_set, evaluate

### `benchmarks/run_benchmark_v2.py`

- **chunk_project** (function, L79)
  - calls: _build_ignore_spec, _walk_files, chunk_code_file, chunk_doc_file
  - called by: main
- **evaluate** (function, L113)
  - calls: search
  - called by: main
- **main** (function, L217)
  - calls: chunk_project, evaluate

## External Dependencies

**Calls out to:**
- `VectorEngine.search`
- `src/hybrid_search/index/ast_chunker.py::chunk_code_file`
- `src/hybrid_search/index/doc_chunker.py::chunk_doc_file`
- `src/hybrid_search/index/scanner.py::_build_ignore_spec`
- `src/hybrid_search/index/scanner.py::_walk_files`

</details>