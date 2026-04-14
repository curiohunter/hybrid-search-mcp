# Benchmarks

**Files**: 3 | **Symbols**: 10

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

## Related Modules
- [[hybrid_search]]
- [[storage]]
- [[tests]]
- [[tools]]

- [[index]]
- [[search]]

## External Dependencies

**Calls out to:**
- `VectorEngine.search`
- `src/hybrid_search/index/ast_chunker.py::chunk_code_file`
- `src/hybrid_search/index/doc_chunker.py::chunk_doc_file`
- `src/hybrid_search/index/scanner.py::_build_ignore_spec`
- `src/hybrid_search/index/scanner.py::_walk_files`
