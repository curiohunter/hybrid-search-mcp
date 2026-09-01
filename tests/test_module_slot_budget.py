"""Module rows must never take more than half the result slots.

`_interleave_modules` documented that invariant for a long time while
nothing enforced it; the 2026-08-27 fix compared cards and members inside
the module lane, and WS1 (2026-09-01) moved ALL budgeting into
``slot_planner.plan_slots`` — the layout function no longer budgets at
all. These tests exercise the planner+layout pair the way the
orchestrator wires them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hybrid_search.search.orchestrator import _interleave_modules
from hybrid_search.search.slot_planner import plan_slots


def _rows(prefix: str, n: int):
    return [SimpleNamespace(file_path=f"{prefix}{i}.py", chunk_id=f"{prefix}{i}")
            for i in range(n)]


def _module_count(results, cards, members) -> int:
    ids = {r.chunk_id for r in cards} | {r.chunk_id for r in members}
    return sum(1 for r in results if r.chunk_id in ids)


def _planned_interleave(chunks, cards, requested, limit, members=None, n_memory=0):
    plan = plan_slots(
        limit,
        memory_intent=False,
        requested_card_slots=requested,
        n_chunks=len(chunks),
        n_memory=n_memory,
        n_cards=len(cards),
        n_members=len(members or []),
    )
    return _interleave_modules(
        chunks, cards, plan.card_slots, limit,
        members=members, member_slots=plan.member_slots,
    )


@pytest.mark.parametrize("limit", [4, 5, 6, 8, 10, 20])
def test_modules_never_exceed_half_the_slots(limit):
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _planned_interleave(chunks, cards, 3, limit, members=members)
    assert _module_count(out, cards, members) * 2 <= limit


def test_limit_five_leaves_three_chunk_slots():
    """The reported case: 5 results, of which only 2 were usable."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _planned_interleave(chunks, cards, 3, 5, members=members)
    assert len(out) == 5
    assert _module_count(out, cards, members) == 2


def test_cards_are_funded_before_members():
    """Cards carry the retrieval win on this corpus — members take what is
    left, not the other way round."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _planned_interleave(chunks, cards, 3, 5, members=members)
    card_ids = {r.chunk_id for r in cards}
    assert sum(1 for r in out if r.chunk_id in card_ids) == 2
    member_ids = {r.chunk_id for r in members}
    assert sum(1 for r in out if r.chunk_id in member_ids) == 0


def test_a_generous_limit_still_admits_members():
    """The cap must not become a ban — at limit=10 members still surface."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _planned_interleave(chunks, cards, 3, 10, members=members)
    member_ids = {r.chunk_id for r in members}
    assert sum(1 for r in out if r.chunk_id in member_ids) >= 1


def test_no_modules_means_pure_chunks():
    chunks = _rows("c", 40)
    out = _planned_interleave(chunks, [], 3, 5, members=[])
    assert [r.chunk_id for r in out] == [f"c{i}" for i in range(5)]


def test_memory_slot_shrinks_the_aux_budget():
    """WS1's actual fix: with a memory candidate present, one aux slot is
    reserved for it BEFORE members are funded — the sum stays bounded.
    Pre-planner, memory (3) + cards (3) + members (2) claimed 8 of 10."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _planned_interleave(chunks, cards, 3, 10, members=members, n_memory=4)
    aux = _module_count(out, cards, members)
    # cards 3 + members 1 (one member slot ceded to memory) = 4 module rows.
    # In the real pipeline the memory row is spliced into the chunk stream
    # before layout; here chunks backfill its position, so len stays 10.
    assert aux == 4
    assert len(out) == 10
