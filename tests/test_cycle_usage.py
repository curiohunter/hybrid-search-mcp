"""The cycle report's real-usage section. Fixtures are synthetic."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "cycle.py"
_spec = importlib.util.spec_from_file_location("cycle", _PATH)
cycle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cycle)


def _usage(found: float, carried: dict | None = None) -> dict:
    lane = {"total": 10, "adopted": 2, "betrayed": 5, "unservable": 1, "no_followup": 2}
    empty = {"total": 0, "adopted": 0, "betrayed": 0, "unservable": 0, "no_followup": 0}
    replay = {"all": {"n": 8, "found": found, "top3": 0.25, "top5": 0.25, "mrr": 0.2}}
    if carried:
        replay["carried"] = carried
    return {"proj": {"usage": {"lanes": {"tool": empty, "prefetch": lane},
                               "harness_excluded": 4},
                     "replay": replay, "set_aside": {"unindexed": 3}}}


class TestUsageLines:
    def test_silent_without_usage(self) -> None:
        assert cycle.usage_lines({"date": "2026-01-02"}, None) == []

    def test_empty_lanes_are_not_printed(self) -> None:
        text = "\n".join(cycle.usage_lines({"usage": _usage(0.5)}, None))
        assert "| proj | prefetch | 10 | 2 (20%) | 5 (50%) | 1 | 2 |" in text
        assert "| proj | tool |" not in text

    def test_carried_set_is_paired_with_the_previous_reading(self) -> None:
        now = {"usage": _usage(0.6, carried={"n": 8, "found": 0.625})}
        prev = {"usage": _usage(0.5)}
        text = "\n".join(cycle.usage_lines(now, prev))
        assert "0.5 → 0.625 (n=8)" in text

    def test_usage_never_fails_the_cycle(self) -> None:
        now = {"date": "2026-01-09", "code_sha": "b",
               "usage": _usage(0.1, carried={"n": 8, "found": 0.1})}
        prev = {"date": "2026-01-02", "code_sha": "a", "usage": _usage(0.9)}
        _, regressed = cycle.report(now, prev)
        assert regressed is False

    def test_cold_set_has_its_own_column(self) -> None:
        usage = _usage(0.5)
        usage["proj"]["replay"]["cold"] = {"n": 4, "found": 0.25}
        text = "\n".join(cycle.usage_lines({"usage": usage}, None))
        assert "| 0.25 (n=4) |" in text
