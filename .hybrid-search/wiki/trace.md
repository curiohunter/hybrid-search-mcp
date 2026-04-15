# Trace

**Files**: 1 | **Symbols**: 7

## Files

- `src/hybrid_search/tools/trace.py`

## Entry Points

- `src/hybrid_search/tools/trace.py::handle_trace_callees`
- `src/hybrid_search/tools/trace.py::handle_trace_callers`

## Symbols

### `src/hybrid_search/tools/trace.py`

- **TraceError** (class, L20)
  - called by: _open_stores
- **handle_trace_callers** (function, L24)
  - calls: _open_stores, _resolve_start, _trace_callers_recursive
- **handle_trace_callees** (function, L74)
  - calls: _open_stores, _resolve_start, _trace_callees_recursive
- **_trace_callers_recursive** (function, L124)
  - called by: handle_trace_callers
- **_trace_callees_recursive** (function, L166)
  - called by: handle_trace_callees
- **_open_stores** (function, L220)
  - calls: TraceError
  - called by: handle_trace_callees, handle_trace_callers
- **_resolve_start** (function, L251)
  - called by: handle_trace_callees, handle_trace_callers
