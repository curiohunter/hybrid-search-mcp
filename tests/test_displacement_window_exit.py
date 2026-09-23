"""The displacement audit counts answers pushed out of the window, not only deleted.

2026-09-23: the audit reported damage 0 while a gold answer slid from rank
2 to rank 12 — nothing deleted, just past the limit. ``--carry`` re-asks
last cycle's in-window probes and counts the ones now outside it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "displacement_audit.py"
_spec = importlib.util.spec_from_file_location("displacement_audit", _PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _qa(question: str) -> str:
    return f'---\nquery: "{question}"\n---\n\n## Answer excerpt\n\nsomething\n'


class TestCarriedProbes:
    def test_only_in_window_probes_are_carried(self) -> None:
        prev = {"rows": [
            {"probe": "a", "query": "q a", "self_found_on": True},
            {"probe": "b", "query": "q b", "self_found_on": False},
        ]}
        asked, deferred, gone = audit._carried_probes(
            prev, {"a": _qa("q a"), "b": _qa("q b")}, cap=10, seed=1)
        assert [c for c, _ in asked] == ["a"]
        assert deferred == [] and gone == 0

    def test_rewritten_record_is_gone_not_an_exit(self) -> None:
        prev = {"rows": [{"probe": "a", "query": "q a", "self_found_on": True}]}
        asked, _, gone = audit._carried_probes(prev, {}, cap=10, seed=1)
        assert asked == [] and gone == 1

    def test_watch_set_accumulates_across_cycles(self) -> None:
        prev = {
            "rows": [{"probe": "new", "query": "q new", "self_found_on": True}],
            "carried_in_window": [{"probe": "old", "query": "q old"}],
        }
        asked, _, _ = audit._carried_probes(
            prev, {"new": _qa("q new"), "old": _qa("q old")}, cap=10, seed=1)
        assert sorted(c for c, _ in asked) == ["new", "old"]

    def test_cap_defers_rather_than_drops(self) -> None:
        prev = {"rows": [{"probe": str(i), "query": f"q {i}", "self_found_on": True}
                         for i in range(5)]}
        current = {str(i): _qa(f"q {i}") for i in range(5)}
        asked, deferred, _ = audit._carried_probes(prev, current, cap=2, seed=1)
        assert len(asked) == 2 and len(deferred) == 3
        assert {c for c, _ in asked + deferred} == set(current)

    def test_seeded_sample_is_stable(self) -> None:
        prev = {"rows": [{"probe": str(i), "query": f"q {i}", "self_found_on": True}
                         for i in range(20)]}
        current = {str(i): _qa(f"q {i}") for i in range(20)}
        first = audit._carried_probes(prev, current, cap=5, seed=7)[0]
        again = audit._carried_probes(prev, current, cap=5, seed=7)[0]
        assert first == again

    def test_current_question_is_asked(self) -> None:
        prev = {"rows": [{"probe": "a", "query": "truncated", "self_found_on": True}]}
        asked, _, _ = audit._carried_probes(prev, {"a": _qa("full question")},
                                            cap=10, seed=1)
        assert asked == [("a", "full question")]


class TestWindowExits:
    def test_counts_only_probes_missing_from_their_own_results(self) -> None:
        carried = [("a", "q a"), ("b", "q b")]
        results = {"a": [("x", ""), ("a", "")], "b": [("x", ""), ("y", "")]}
        assert audit._window_exits(carried, results) == [
            {"probe": "b", "query": "q b"}]

    def test_no_results_is_an_exit(self) -> None:
        assert audit._window_exits([("a", "q")], {}) == [{"probe": "a", "query": "q"}]
