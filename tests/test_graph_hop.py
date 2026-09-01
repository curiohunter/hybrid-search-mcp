"""graph_hop — 1-hop call-graph candidate expansion (WS2)."""

from __future__ import annotations

from types import SimpleNamespace

from hybrid_search.search.graph_hop import (
    _NEIGHBORS_PER_HIT,
    _SCORE_DECAY,
    collect_neighbor_ids,
    merge_by_score,
)


def _hit(cid, score=0.03, node_type="function"):
    return SimpleNamespace(chunk_id=cid, rrf_score=score, node_type=node_type)


class _FakeDB:
    """callers/callees keyed by chunk_id; raises for ids in ``broken``."""

    def __init__(self, callers=None, callees=None, broken=()):
        self._callers = callers or {}
        self._callees = callees or {}
        self._broken = set(broken)

    def get_callers(self, chunk_id, project_id, min_confidence):
        if chunk_id in self._broken:
            raise RuntimeError("db went away")
        return [
            {"caller_chunk_id": cid, "confidence_score": conf}
            for cid, conf in self._callers.get(chunk_id, [])
        ]

    def get_callees(self, chunk_id, project_id, min_confidence):
        if chunk_id in self._broken:
            raise RuntimeError("db went away")
        return [
            {"callee_chunk_id": cid, "confidence_score": conf}
            for cid, conf in self._callees.get(chunk_id, [])
        ]


class TestCollectNeighborIds:
    def test_neighbors_get_decayed_source_score(self):
        db = _FakeDB(callers={"a": [("n1", 0.9)]})
        pairs = collect_neighbor_ids(
            db, "p", [_hit("a", score=0.03)], exclude_ids={"a"}, total_cap=10,
        )
        assert pairs == [("n1", 0.03 * _SCORE_DECAY)]

    def test_non_code_hits_are_not_expanded(self):
        db = _FakeDB(callers={"a": [("n1", 0.9)]})
        hits = [
            _hit("a", node_type="qa_log"),
            _hit("a", node_type="module"),
            _hit("a", node_type="section"),
        ]
        assert collect_neighbor_ids(db, "p", hits, exclude_ids=set(), total_cap=10) == []

    def test_per_hit_cap_takes_best_confidence_first(self):
        neighbors = [(f"n{i}", i / 10) for i in range(6)]  # n5 best
        db = _FakeDB(callers={"a": neighbors})
        pairs = collect_neighbor_ids(
            db, "p", [_hit("a")], exclude_ids={"a"}, total_cap=10,
        )
        assert len(pairs) == _NEIGHBORS_PER_HIT
        assert [cid for cid, _ in pairs] == ["n5", "n4", "n3"]

    def test_existing_candidates_are_not_duplicated(self):
        db = _FakeDB(callers={"a": [("already", 0.9), ("fresh", 0.8)]})
        pairs = collect_neighbor_ids(
            db, "p", [_hit("a")], exclude_ids={"a", "already"}, total_cap=10,
        )
        assert [cid for cid, _ in pairs] == ["fresh"]

    def test_shared_neighbor_claimed_by_higher_hit(self):
        db = _FakeDB(callers={"a": [("shared", 0.9)], "b": [("shared", 0.9)]})
        pairs = collect_neighbor_ids(
            db, "p", [_hit("a", 0.03), _hit("b", 0.02)],
            exclude_ids={"a", "b"}, total_cap=10,
        )
        assert pairs == [("shared", 0.03 * _SCORE_DECAY)]

    def test_unresolved_callee_rows_are_skipped(self):
        db = _FakeDB(callees={"a": [(None, 0.9), ("real", 0.8)]})
        pairs = collect_neighbor_ids(
            db, "p", [_hit("a")], exclude_ids={"a"}, total_cap=10,
        )
        assert [cid for cid, _ in pairs] == ["real"]

    def test_db_failure_degrades_to_no_expansion_for_that_hit(self):
        db = _FakeDB(callers={"b": [("n1", 0.9)]}, broken={"a"})
        pairs = collect_neighbor_ids(
            db, "p", [_hit("a"), _hit("b")], exclude_ids={"a", "b"}, total_cap=10,
        )
        assert [cid for cid, _ in pairs] == ["n1"]

    def test_total_cap_bounds_the_pool(self):
        callers = {f"h{i}": [(f"n{i}{j}", 0.9) for j in range(3)] for i in range(5)}
        db = _FakeDB(callers=callers)
        hits = [_hit(f"h{i}") for i in range(5)]
        pairs = collect_neighbor_ids(
            db, "p", hits, exclude_ids=set(h.chunk_id for h in hits), total_cap=4,
        )
        assert len(pairs) == 4


class TestMergeByScore:
    def _row(self, cid, score):
        return SimpleNamespace(chunk_id=cid, rrf_score=score)

    def test_addition_lands_before_first_lower_scored_row(self):
        existing = [self._row("a", 0.03), self._row("b", 0.02), self._row("c", 0.01)]
        add = [self._row("n", 0.015)]
        out = merge_by_score(existing, add)
        assert [r.chunk_id for r in out] == ["a", "b", "n", "c"]

    def test_existing_order_is_never_disturbed(self):
        # Deliberately non-sorted existing order (in-flight overlay rows).
        existing = [self._row("a", 0.01), self._row("b", 0.03)]
        out = merge_by_score(existing, [self._row("n", 0.02)])
        assert [r.chunk_id for r in out] == ["n", "a", "b"]
        assert out.index(existing[0]) < out.index(existing[1])

    def test_tie_keeps_existing_first(self):
        existing = [self._row("a", 0.02)]
        out = merge_by_score(existing, [self._row("n", 0.02)])
        assert [r.chunk_id for r in out] == ["a", "n"]

    def test_no_additions_is_identity(self):
        existing = [self._row("a", 0.03)]
        assert merge_by_score(existing, []) == existing
