"""Tests for module-card injection into hybrid_search response (Phase 5 Step 4)."""

from __future__ import annotations

from hybrid_search.search.orchestrator import (
    HybridResult,
    QueryType,
    _filename_token_set,
    _has_rationale_signal,
    _has_symbol_signal,
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


def _mk_member(file_path: str, parent_module: str) -> HybridResult:
    """Step K module_member helper."""
    return HybridResult(
        chunk_id=f"member:{parent_module}:{file_path}", rrf_score=0.0,
        bm25_rank=None, vector_rank=None,
        file_path=file_path, project="p", name=parent_module,
        qualified_name=f"module:{parent_module}",
        node_type="module_member", start_line=None, end_line=None,
        content=None, snippet="module summary",
        module_id=f"mod-{parent_module}",
    )


# ---------- _module_slots_for ----------

def test_slots_korean_nl_three():
    assert _module_slots_for(QueryType.KOREAN_NL) == 3


def test_slots_english_nl_two():
    assert _module_slots_for(QueryType.ENGLISH_NL) == 2


def test_slots_exact_symbol_zero():
    assert _module_slots_for(QueryType.EXACT_SYMBOL) == 0


# ---------- _has_rationale_signal / rationale routing ----------

def test_rationale_signal_korean_iyu():
    assert _has_rationale_signal("portal v3로 리팩토링하는 이유는 무엇인가")


def test_rationale_signal_korean_bae_gyeong():
    assert _has_rationale_signal("ledger writepath ABC 설계를 택한 배경은")


def test_rationale_signal_korean_mokjeok():
    assert _has_rationale_signal("AI 콘텐츠 팩토리를 만드는 목적")


def test_rationale_signal_korean_wae_token():
    assert _has_rationale_signal("entrance test 관리 플랜은 왜 세워졌나")


def test_rationale_signal_english_why():
    assert _has_rationale_signal("why we chose portal v3")


def test_rationale_signal_english_rationale_word_boundary():
    assert _has_rationale_signal("rationale for the write-path refactor")


def test_rationale_signal_english_not_inside_word():
    # "multipurpose" must not fire — word-boundary required
    assert not _has_rationale_signal("multipurpose sorter component")


def test_rationale_signal_negative_structure_query():
    assert not _has_rationale_signal("수강료 정산 시스템은 어떻게 구성되어 있나")
    assert not _has_rationale_signal("월별 학원 통계는 어떻게 집계되나")
    assert not _has_rationale_signal("학부모 학생 포털 인증 및 레이아웃 흐름")


def test_rationale_signal_negative_exploration_query():
    assert not _has_rationale_signal("변형 문제 variant problems 생성 로직")
    assert not _has_rationale_signal("출결 관리 기능은 어디에 있나")


def test_slots_korean_nl_rationale_returns_zero():
    # With rationale signal, a Korean NL query should skip module injection.
    assert _module_slots_for(QueryType.KOREAN_NL, "portal v3 리팩토링 이유") == 0
    assert _module_slots_for(QueryType.KOREAN_NL, "왜 tuition hub를 만드는가") == 0


def test_slots_english_nl_rationale_returns_zero():
    assert _module_slots_for(QueryType.ENGLISH_NL, "why did we pick portal v3") == 0


def test_slots_korean_nl_non_rationale_unchanged():
    # Structure/exploration queries keep the Korean NL default of 3 slots.
    assert _module_slots_for(QueryType.KOREAN_NL, "수강료 정산 시스템 구조") == 3


# ---------- Step C: symbol signal routing ----------

def test_symbol_signal_detects_camel_case():
    assert _has_symbol_signal("TuitionChargeSection 컴포넌트")


def test_symbol_signal_detects_snake_case():
    assert _has_symbol_signal("admission_results 테이블 스키마")


def test_symbol_signal_detects_screaming_snake():
    assert _has_symbol_signal("MAX_RETRIES 상수")


def test_symbol_signal_detects_dot_qualified():
    assert _has_symbol_signal("AuthService.signIn 찾아줘")


def test_symbol_signal_negative_plain_korean_nl():
    assert not _has_symbol_signal("수강료 정산 시스템은 어떻게 구성되어 있나")


def test_symbol_signal_negative_english_nl():
    assert not _has_symbol_signal("how is the attendance module organized")


def test_slots_mixed_symbol_korean_returns_zero():
    # Mixed symbol + Korean query — classify_query maps this to KOREAN_NL,
    # but the intent is precision lookup, not subsystem exploration.
    assert _module_slots_for(QueryType.KOREAN_NL, "TuitionChargeSection 컴포넌트") == 0
    assert _module_slots_for(QueryType.KOREAN_NL, "admission_results 테이블 스키마") == 0


def test_slots_pure_korean_unaffected_by_symbol_check():
    # Real Korean NL query still gets module slots.
    assert _module_slots_for(QueryType.KOREAN_NL, "수강료 시스템 구성") == 3


# ---------- L5 two-tier cap ----------

def test_interleave_cap_at_limit_10():
    """Default benchmark limit — cap is a no-op."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    out = _interleave_modules(chunks, modules, slots=3, limit=10)
    assert sum(1 for r in out if r.node_type == "module") == 3


def test_interleave_cap_low_limit_preserves_chunks():
    """limit=3 with slots=3: L5 caps to 1 module so chunks stay the majority."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    out = _interleave_modules(chunks, modules, slots=3, limit=3)
    assert len(out) == 3
    n_mod = sum(1 for r in out if r.node_type == "module")
    n_chunk = sum(1 for r in out if r.node_type == "function")
    assert n_mod == 1
    assert n_chunk == 2


def test_interleave_cap_limit_2_minimum_one_module():
    """limit=2: cap floor is 1 (max(1, 2//2) = 1) — still get a module for
    structural queries, but chunk stays available."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(5)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    out = _interleave_modules(chunks, modules, slots=3, limit=2)
    assert len(out) == 2
    assert sum(1 for r in out if r.node_type == "module") == 1
    assert sum(1 for r in out if r.node_type == "function") == 1


def test_interleave_cap_limit_zero_returns_empty():
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(3)]
    modules = [_mk_module("m.ts", "M")]
    assert _interleave_modules(chunks, modules, slots=3, limit=0) == []


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
    # L5 two-tier cap: modules never exceed limit // 2. With limit=5 and
    # slots=3, cap is 2 — two modules, three chunks. Keeps chunks in the
    # majority for small result windows.
    assert sum(1 for r in out if r.node_type == "module") == 2
    assert sum(1 for r in out if r.node_type == "function") == 3


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


# ---------- Step J: _filename_token_set ----------

def test_filename_tokens_split_camel_and_hyphen():
    """Step J needs camelCase-aware filename tokens so .tsx members can
    match English query terms. Without this, `HomeworkTab.tsx` collapses
    to one token and loses to hyphenated docs on query-aware rep pick."""
    toks = _filename_token_set("components/learning/homework-analysis/HomeworkTab.tsx")
    assert toks == {"homework", "tab"}


def test_filename_tokens_split_underscore():
    toks = _filename_token_set("database/migrations/create_academy_monthly_stats.sql")
    assert toks == {"create", "academy", "monthly", "stats"}


def test_filename_tokens_strip_date_prefix():
    toks = _filename_token_set("database/migrations/20260327_create_admission_results.sql")
    assert "20260327" not in toks
    assert "admission" in toks
    assert "results" in toks


def test_filename_tokens_drop_short_pieces():
    # "a" is < 3 chars and should be dropped; "index" alone stays.
    assert _filename_token_set("foo/a.ts") == set()
    assert _filename_token_set("foo/index.ts") == {"index"}


def test_filename_tokens_mixed_camel_and_hyphen():
    toks = _filename_token_set("components/ConceptWeaknessCard.tsx")
    assert toks == {"concept", "weakness", "card"}


# ---------- Step K: module_member placement ----------

def test_members_placed_at_tail_positions():
    """Members surface at trailing ranks — ranks 1/3/5 stay module cards,
    ranks 2/4/6/7 stay chunks (primary-target docs for S2/S3 queries
    would otherwise get pushed out by aggressive member insertion)."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    members = [
        _mk_member("mem/a1.tsx", "nonA"),
        _mk_member("mem/a2.tsx", "nonA"),
    ]
    out = _interleave_modules(chunks, modules, slots=3, limit=10, members=members)
    assert len(out) == 10
    types = [r.node_type for r in out]
    # Modules at 0, 2, 4
    assert types[0] == "module"
    assert types[2] == "module"
    assert types[4] == "module"
    # First chunk survives at rank 2 (position 1)
    assert types[1] == "function"
    # Members at the tail (last two positions)
    assert types[-1] == "module_member"
    assert types[-2] == "module_member"


def test_members_respect_chunks_at_primary_positions():
    """Regression: S2 primary target is a chunk at rank 2. Members must
    not displace it."""
    top_chunk = "docs/features/2026-04-08-portal-parent-student.md"
    chunks = [_mk_chunk(top_chunk, "portal")] + [
        _mk_chunk(f"c{i}.ts") for i in range(5)
    ]
    modules = [_mk_module("auth.md", "auth"), _mk_module("students.ts", "students")]
    members = [_mk_member("mem/x.tsx", "other")]
    out = _interleave_modules(chunks, modules, slots=2, limit=10, members=members)
    # Rank 2 (position 1) should still be the top chunk.
    assert out[1].file_path == top_chunk
    assert out[1].node_type == "function"


def test_members_dedup_against_module_cards():
    """A member that points at a path already held by a module card
    must be dropped — otherwise the same file appears twice in
    results, wasting a slot."""
    shared = "components/portal-v3/PortalShell.tsx"
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(5)]
    modules = [_mk_module(shared, "portal-v3")]
    members = [_mk_member(shared, "portal-v3-alt")]
    out = _interleave_modules(chunks, modules, slots=1, limit=10, members=members)
    paths = [r.file_path for r in out]
    # Only one entry with the shared path (the module card).
    assert paths.count(shared) == 1
    # The shared-path entry is the module card (rank 1).
    assert out[0].node_type == "module" and out[0].file_path == shared


def test_members_budget_caps_at_limit_third():
    """At limit=10, at most 3 members reach the result to keep chunks
    at a numeric majority (7 slots for cards+chunks)."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(10)]
    modules = [_mk_module(f"m{i}.ts", f"M{i}") for i in range(3)]
    members = [_mk_member(f"mem/m{i}.tsx", f"non{i}") for i in range(8)]
    out = _interleave_modules(chunks, modules, slots=3, limit=10, members=members)
    member_count = sum(1 for r in out if r.node_type == "module_member")
    # limit=10 // 3 = 3 members max.
    assert member_count == 3


def test_members_absorb_slack_when_chunks_short():
    """If chunks run out, members can still fill the non-module slots
    up to the member budget."""
    chunks = [_mk_chunk("c0.ts"), _mk_chunk("c1.ts")]
    modules = [_mk_module("m0.ts", "M0")]
    members = [_mk_member(f"mem/m{i}.tsx", f"non{i}") for i in range(5)]
    out = _interleave_modules(chunks, modules, slots=1, limit=10, members=members)
    # 1 card + 2 chunks + up to 3 members = 6; rest positions empty & dropped.
    chunk_count = sum(1 for r in out if r.node_type == "function")
    member_count = sum(1 for r in out if r.node_type == "module_member")
    assert chunk_count == 2
    assert member_count == 3


def test_members_none_preserves_old_behavior():
    """Passing ``members=None`` (or empty) keeps the pre-K behaviour —
    callers that don't opt in see exactly the old layout."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(5)]
    modules = [_mk_module("m0.ts", "M0"), _mk_module("m1.ts", "M1")]
    out_no_members = _interleave_modules(chunks, modules, slots=2, limit=5)
    out_empty_members = _interleave_modules(
        chunks, modules, slots=2, limit=5, members=[],
    )
    assert [r.file_path for r in out_no_members] == [r.file_path for r in out_empty_members]


def test_members_slots_zero_returns_chunks_only():
    """Rationale/precision queries get ``slots=0`` from ``_module_slots_for``;
    members must not leak through in that case."""
    chunks = [_mk_chunk(f"c{i}.ts") for i in range(5)]
    modules = [_mk_module("m.ts", "M")]
    members = [_mk_member("mem.tsx", "non")]
    out = _interleave_modules(chunks, modules, slots=0, limit=5, members=members)
    assert all(r.node_type == "function" for r in out)
    assert len(out) == 5
