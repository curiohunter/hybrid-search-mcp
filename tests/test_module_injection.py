"""Tests for module-card injection into hybrid_search response (Phase 5 Step 4)."""

from __future__ import annotations

from hybrid_search.search.orchestrator import (
    HybridResult,
    QueryType,
    _interleave_modules,
    _module_slots_for,
)


def _mk_chunk(file_path: str, name: str = "x") -> HybridResult:
    return HybridResult(
        chunk_id=f"c_{file_path}", rrf_score=1.0, bm25_rank=1, vector_rank=1,
        file_path=file_path, project="p", name=name, qualified_name=name,
        node_type="function", start_line=1, end_line=10,
        content=None, snippet="chunk snip",
    )


def _mk_module(file_path: str, name: str = "m") -> HybridResult:
    return HybridResult(
        chunk_id=f"module:{name}", rrf_score=0.0, bm25_rank=None, vector_rank=None,
        file_path=file_path, project="p", name=name,
        qualified_name=f"module:{name}",
        node_type="module", start_line=None, end_line=None,
        content=None, snippet="module summary",
        module_id=f"mod-{name}",
    )


# ---------- _module_slots_for ----------

def test_slots_korean_nl_three():
    assert _module_slots_for(QueryType.KOREAN_NL) == 3


def test_slots_english_nl_two():
    assert _module_slots_for(QueryType.ENGLISH_NL) == 2


def test_slots_exact_symbol_zero():
    assert _module_slots_for(QueryType.EXACT_SYMBOL) == 0


# ---------- _interleave_modules ----------

def test_interleave_places_module_at_top_then_alternates():
    # Interleave rule: positions 0,2,4 get modules (up to slots), rest chunks.
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    out = _interleave_modules(chunks, modules, slots=3, limit=10)
    types = [r.node_type for r in out]
    assert types[0] == "module"
    assert types[1] == "function"
    assert types[2] == "module"
    assert types[3] == "function"
    assert types[4] == "module"
    # After the 3 modules consumed, tail is all chunks
    assert all(t == "function" for t in types[5:])
    assert len(out) == 10


def test_interleave_respects_limit():
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    out = _interleave_modules(chunks, modules, slots=3, limit=5)
    assert len(out) == 5
    # Positions 0,2,4 → 3 modules in a limit-5 result
    assert sum(1 for r in out if r.node_type == "module") == 3


def test_interleave_slots_zero_returns_chunks_only():
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(5)]
    modules = [_mk_module("m.ts", "M")]
    out = _interleave_modules(chunks, modules, slots=0, limit=5)
    assert all(r.node_type == "function" for r in out)
    assert len(out) == 5


def test_interleave_no_modules_returns_chunks():
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(3)]
    out = _interleave_modules(chunks, [], slots=3, limit=10)
    assert len(out) == 3
    assert out == chunks


def test_interleave_fewer_modules_than_slots():
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module("m0.ts", "M0")]
    out = _interleave_modules(chunks, modules, slots=3, limit=10)
    assert out[0].node_type == "module"
    # Only one module available, rest of positions fill with chunks in order
    assert all(r.node_type == "function" for r in out[1:])
    assert len(out) == 10


def test_interleave_dedupes_chunk_when_module_has_same_file():
    # A module whose representative file matches one chunk's file — the chunk
    # should be dropped so the same file doesn't appear twice in results.
    shared = "components/portal-v3/shell.tsx"
    chunks = [
        _mk_chunk(shared, "ShellComponent"),
        _mk_chunk("components/tuition/row.tsx", "Row"),
    ]
    modules = [_mk_module(shared, "portal-v3")]
    out = _interleave_modules(chunks, modules, slots=1, limit=5)
    assert out[0].node_type == "module"
    # Shared file is not in a chunk result anymore
    chunk_files = [r.file_path for r in out if r.node_type == "function"]
    assert shared not in chunk_files
    assert "components/tuition/row.tsx" in chunk_files


def test_interleave_chunk_stays_at_position_2():
    """Rationale-style query: the top chunk must stay near the top even when
    modules are injected, because the real answer may be a plan doc (chunk)
    rather than any module card."""
    top_chunk_file = "docs/plans/2026-04-21-ledger-writepath-abc.md"
    chunks = [_mk_chunk(top_chunk_file, "rationale")] + [
        _mk_chunk(f"c{i}.ts") for i in range(5)
    ]
    modules = [_mk_module("m.ts", "Meta")]
    out = _interleave_modules(chunks, modules, slots=1, limit=5)
    # Position 1 = module (one slot), position 2 = top chunk
    assert out[0].node_type == "module"
    assert out[1].file_path == top_chunk_file
