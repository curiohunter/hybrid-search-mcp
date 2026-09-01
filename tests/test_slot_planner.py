"""slot_planner — the single allocation decision point (WS1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hybrid_search.search.slot_planner import cap_per_file, plan_slots


def _plan(limit=10, *, memory_intent=False, requested=3,
          n_chunks=40, n_memory=0, n_cards=6, n_members=6):
    return plan_slots(
        limit,
        memory_intent=memory_intent,
        requested_card_slots=requested,
        n_chunks=n_chunks,
        n_memory=n_memory,
        n_cards=n_cards,
        n_members=n_members,
    )


class TestChunkFloor:
    def test_chunks_keep_at_least_half(self):
        p = _plan(10, n_memory=5)
        assert p.chunk_floor == 5
        assert p.aux_total <= 5

    def test_odd_limit_rounds_floor_up(self):
        p = _plan(5, n_memory=5)
        assert p.chunk_floor == 3
        assert p.aux_total <= 2

    def test_scarce_chunks_release_budget_to_aux(self):
        p = _plan(10, n_chunks=2, n_memory=5)
        assert p.chunk_floor == 2
        assert p.card_slots == 3
        assert p.memory_slots == 1
        assert p.member_slots >= 1

    def test_sum_never_exceeds_limit(self):
        for limit in (1, 2, 3, 4, 5, 7, 10, 20, 40):
            for n_chunks in (0, 1, 5, 40):
                for n_memory in (0, 1, 10):
                    p = _plan(limit, n_chunks=n_chunks, n_memory=n_memory)
                    assert p.chunk_floor + p.aux_total <= limit


class TestMemoryGuarantee:
    def test_memory_gets_one_slot_when_candidates_exist(self):
        p = _plan(10, n_memory=4)
        assert p.memory_slots == 1

    def test_no_candidates_no_slot(self):
        p = _plan(10, n_memory=0)
        assert p.memory_slots == 0

    def test_full_card_request_cannot_zero_memory(self):
        # The reported failure shape: aux lanes independently maxing out.
        p = _plan(10, requested=5, n_memory=4)
        assert p.memory_slots == 1
        assert p.card_slots + p.member_slots <= 4


class TestMemoryIntent:
    def test_memory_head_is_exempt_from_half_cap(self):
        p = _plan(4, memory_intent=True, n_memory=10)
        assert p.memory_slots == 3  # 3 of 4 > half — allowed for recall

    def test_modules_are_off(self):
        p = _plan(10, memory_intent=True, n_memory=10)
        assert p.card_slots == 0
        assert p.member_slots == 0

    def test_chunks_keep_one_slot_when_they_exist(self):
        # Misclassified prompt must not blank the code lane entirely.
        p = _plan(3, memory_intent=True, n_memory=10, n_chunks=5)
        assert p.chunk_floor >= 1

    def test_no_chunks_lets_memory_take_all(self):
        p = _plan(3, memory_intent=True, n_memory=10, n_chunks=0)
        assert p.memory_slots == 3


class TestAuxPriority:
    def test_cards_before_members(self):
        p = _plan(5, n_memory=0)
        assert p.card_slots == 2
        assert p.member_slots == 0

    def test_members_ride_the_card_lane(self):
        # No cards granted (rationale/symbol queries request 0) → no members.
        p = _plan(10, requested=0, n_members=6)
        assert p.card_slots == 0
        assert p.member_slots == 0

    def test_member_anti_flood_cap(self):
        p = _plan(20, requested=3, n_members=10, n_memory=0)
        assert p.member_slots == 6  # limit // 3

    def test_pools_bound_allocations(self):
        p = _plan(10, n_cards=1, n_members=0, n_memory=0)
        assert p.card_slots == 1
        assert p.member_slots == 0


class TestEdges:
    def test_limit_zero(self):
        p = _plan(0)
        assert p.chunk_floor == p.aux_total == 0

    def test_limit_one_prefers_chunk(self):
        p = _plan(1, n_memory=5)
        assert p.chunk_floor == 1
        assert p.aux_total == 0

    @pytest.mark.parametrize("limit", [2, 3, 5, 10])
    def test_reproduces_handoff_target_composition(self, limit):
        # The handoff's target: at limit=10 with all lanes populated,
        # chunks 5 / cards 3 / memory 1 / members 1 (was 2/3/3/2 = 8 aux).
        p = _plan(limit, n_memory=4)
        if limit == 10:
            assert (p.chunk_floor, p.card_slots, p.memory_slots, p.member_slots) == (5, 3, 1, 1)


class TestPerFileCap:
    def _row(self, fp):
        return SimpleNamespace(file_path=fp)

    def test_third_row_of_a_file_is_dropped(self):
        rows = [self._row("a.md"), self._row("a.md"), self._row("a.md"),
                self._row("b.md")]
        out = cap_per_file(rows)
        assert [r.file_path for r in out] == ["a.md", "a.md", "b.md"]

    def test_order_and_distinct_files_preserved(self):
        rows = [self._row(f"f{i}.md") for i in range(5)]
        assert cap_per_file(rows) == rows

    def test_rows_without_path_pass_through(self):
        rows = [self._row(None), self._row(None), self._row(None)]
        assert cap_per_file(rows) == rows
