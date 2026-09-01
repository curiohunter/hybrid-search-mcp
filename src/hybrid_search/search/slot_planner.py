"""SlotPlanner — the single place result-slot allocation is decided.

WS1 of docs/plans/2026-09-01-retrieval-master-plan.md. Three attempts to
cap lanes inside the assembly pipeline failed the same way (2026-08-29
handoff): each lane computed its own budget and nothing compared the sum,
so at ``limit=10`` the auxiliary lanes (module cards, members, memory
head) claimed 8 slots and actual searched chunks got 2.

``plan_slots`` is called once, before any splicing, with the size of every
candidate pool. Downstream stages consume its numbers and may not budget
on their own. Contracts (from the handoff, upgraded to code):

- Chunks keep at least ``ceil(limit/2)`` slots — bounded by availability,
  never by another lane's appetite. Scarcity is judged on pool size alone
  (a lane either ranked a row or didn't); no relevance thresholds here.
- Memory gets at least 1 slot when it has candidates ("why we built it"
  context must never fully disappear), and stays under half.
- ``memory_intent`` queries are exempt from the memory cap — the record
  IS the answer — but chunks still keep one slot when they exist, so a
  misclassified prompt cannot blank the code lane entirely.
- Auxiliary priority: cards → memory → members (ablation: cards carried
  retrieval wins, members were the top pollution source with one
  contribution).
- Whatever an auxiliary lane cannot fill returns to chunks implicitly:
  layout fills unclaimed slots with chunks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Memory head size for explicit recall queries. Carried over unchanged
# from the pre-planner `_merge_memory_results` default: exempt from the
# under-half cap, but not "fill every slot" — the answer excerpt lives in
# one or two records, the rest of the list stays available for evidence.
_MEMORY_INTENT_HEAD = 3

# Anti-flood cap for module members, carried over from the pre-planner
# code: members leaked up to 11 rows at limit=40 while contributing one
# ablation win. The cap lives HERE now, not in the layout function.
def _member_cap(limit: int) -> int:
    return limit // 3


# Per-file diversity cap for the chunk stream. Measured on valuein S1
# ("수강료 정산 시스템은 어떻게 구성되어 있나"): one file's sections held
# 11 of the top-50 rows and 4 of the top-10 — every duplicate past the
# second says nothing new and costs a slot another document needed. Two
# rows is enough for a file to show both its overview and its detail hit.
PER_FILE_CAP = 2


def cap_per_file(results: list, cap: int = PER_FILE_CAP) -> list:
    """Keep at most ``cap`` rows per file_path, preserving order.

    Applied to the fused chunk stream before planning, so freed positions
    backfill with distinct files rather than deeper clones. Rows without
    a file_path pass through untouched.
    """
    seen: dict[str, int] = {}
    out = []
    for r in results:
        fp = getattr(r, "file_path", None)
        if not fp:
            out.append(r)
            continue
        n = seen.get(fp, 0) + 1
        seen[fp] = n
        if n <= cap:
            out.append(r)
    return out


@dataclass(frozen=True)
class SlotPlan:
    """Final per-lane allocations. Sums to at most ``limit``."""

    limit: int
    chunk_floor: int   # slots guaranteed to searched chunks
    memory_slots: int  # spliced memory head (qa/card/term/episodic/commit)
    card_slots: int    # module cards
    member_slots: int  # module members

    @property
    def aux_total(self) -> int:
        return self.memory_slots + self.card_slots + self.member_slots


def plan_slots(
    limit: int,
    *,
    memory_intent: bool,
    requested_card_slots: int,
    n_chunks: int,
    n_memory: int,
    n_cards: int,
    n_members: int,
) -> SlotPlan:
    """Decide every lane's slot count in one place."""
    if limit <= 0:
        return SlotPlan(limit=max(limit, 0), chunk_floor=0, memory_slots=0,
                        card_slots=0, member_slots=0)

    if memory_intent:
        # Module lanes are off for recall queries (they answer structure,
        # not history). Memory takes its head uncapped by the half rule,
        # chunks keep one slot when any exist.
        chunk_reserve = 1 if n_chunks > 0 else 0
        memory = min(_MEMORY_INTENT_HEAD, n_memory, max(0, limit - chunk_reserve))
        return SlotPlan(
            limit=limit,
            chunk_floor=limit - memory,
            memory_slots=memory,
            card_slots=0,
            member_slots=0,
        )

    chunk_floor = min(math.ceil(limit / 2), n_chunks)
    aux_budget = limit - chunk_floor

    # Memory's guaranteed slot is carved out before cards so that a full
    # card request can never zero it — but cards outrank memory for any
    # budget beyond that minimum (ambient memory head has always been 1).
    memory = 1 if (n_memory > 0 and aux_budget > 0) else 0
    cards = min(requested_card_slots, n_cards, max(0, aux_budget - memory))
    if cards > 0:
        members = max(0, min(n_members, _member_cap(limit), aux_budget - memory - cards))
    else:
        # Members ride the card lane — a query that earned no cards
        # (rationale/symbol signals, or no matching modules) gets no
        # sibling-file rows either. The layout enforces the same rule.
        members = 0

    return SlotPlan(
        limit=limit,
        chunk_floor=chunk_floor,
        memory_slots=memory,
        card_slots=cards,
        member_slots=members,
    )
