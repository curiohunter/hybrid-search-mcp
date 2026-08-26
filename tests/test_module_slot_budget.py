"""Module rows must never take more than half the result slots.

`_interleave_modules` has always documented that invariant, but nothing
enforced it: cards were capped at ``limit // 2`` and members at
``limit // 3``, computed independently and never compared. A caller
asking for 5 results got 2 cards + 1 member — 3 of 5 rows — leaving two
slots for actual code. At limit=10 it was 3 + 3 of 10.

Measured on a live corpus: module rows fell from 27% of slots to 18% at
limit=5, with target-hit rate unchanged (17/25).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hybrid_search.search.orchestrator import _interleave_modules


def _rows(prefix: str, n: int):
    return [SimpleNamespace(file_path=f"{prefix}{i}.py", chunk_id=f"{prefix}{i}")
            for i in range(n)]


def _module_count(results, cards, members) -> int:
    ids = {r.chunk_id for r in cards} | {r.chunk_id for r in members}
    return sum(1 for r in results if r.chunk_id in ids)


@pytest.mark.parametrize("limit", [4, 5, 6, 8, 10, 20])
def test_modules_never_exceed_half_the_slots(limit):
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _interleave_modules(chunks, cards, slots=3, limit=limit, members=members)
    assert _module_count(out, cards, members) * 2 <= limit


def test_limit_five_leaves_three_chunk_slots():
    """The reported case: 5 results, of which only 2 were usable."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _interleave_modules(chunks, cards, slots=3, limit=5, members=members)
    assert len(out) == 5
    assert _module_count(out, cards, members) == 2


def test_cards_are_funded_before_members():
    """Cards carry the retrieval win on this corpus — members take what is
    left, not the other way round."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _interleave_modules(chunks, cards, slots=3, limit=5, members=members)
    card_ids = {r.chunk_id for r in cards}
    assert sum(1 for r in out if r.chunk_id in card_ids) == 2
    member_ids = {r.chunk_id for r in members}
    assert sum(1 for r in out if r.chunk_id in member_ids) == 0


def test_a_generous_limit_still_admits_members():
    """The cap must not become a ban — at limit=10 members still surface."""
    cards, members, chunks = _rows("m", 6), _rows("v", 6), _rows("c", 40)
    out = _interleave_modules(chunks, cards, slots=3, limit=10, members=members)
    member_ids = {r.chunk_id for r in members}
    assert sum(1 for r in out if r.chunk_id in member_ids) >= 1


def test_no_modules_means_pure_chunks():
    chunks = _rows("c", 40)
    out = _interleave_modules(chunks, [], slots=3, limit=5, members=[])
    assert [r.chunk_id for r in out] == [f"c{i}" for i in range(5)]
