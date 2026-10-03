"""Reading real-usage records back: who asked, and what counts as a miss.

Every fixture here is synthetic.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "replay_harvested.py"
_spec = importlib.util.spec_from_file_location("replay_harvested", _PATH)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

_NOTE = "<task-notification>\n<task-id>a1</task-id>\n</task-notification>"
_T0 = "2026-01-10T00:00:00+00:00"
_T1 = "2026-01-20T00:00:00+00:00"


def _event(query: str, verdict: str, *, source: str = "prefetch", ts: str = _T1,
           outside: list[str] | None = None, greps: int = 0) -> dict:
    return {"ts": ts, "query": query, "verdict": verdict, "source": source,
            "outside_reads": outside or [], "greps_after": greps}


class TestUsageCounts:
    def test_harness_prompts_are_not_counted_as_questions(self) -> None:
        rows = [
            _event(_NOTE, "betrayed", outside=["src/a.py"]),
            _event("환불 흐름 설명해줘", "adopted"),
        ]
        out = rh.usage_counts(rows, None)
        assert out["harness_excluded"] == 1
        assert out["lanes"]["prefetch"]["total"] == 1
        assert out["lanes"]["prefetch"]["adopted"] == 1

    def test_mixed_counts_as_adopted(self) -> None:
        out = rh.usage_counts([_event("q one two", "mixed")], None)
        assert out["lanes"]["prefetch"]["adopted"] == 1

    def test_betrayal_on_unindexable_files_is_set_apart(self) -> None:
        rows = [
            _event("q scratch", "betrayed", outside=["external:/tmp/x/shot.png"]),
            _event("q real", "betrayed", outside=["src/a.py", "external:/tmp/y"]),
            # It also searched on its own — that part the index could have served.
            _event("q grep", "betrayed", outside=["external:/tmp/z"], greps=2),
        ]
        lane = rh.usage_counts(rows, None)["lanes"]["prefetch"]
        assert lane["unservable"] == 1
        assert lane["betrayed"] == 2

    def test_window_starts_at_since(self) -> None:
        rows = [_event("old q", "adopted", ts=_T0), _event("new q", "no_followup")]
        since = datetime(2026, 1, 15, tzinfo=timezone.utc)
        lane = rh.usage_counts(rows, since)["lanes"]["prefetch"]
        assert lane == {"total": 1, "adopted": 0, "betrayed": 0,
                        "unservable": 0, "no_followup": 1}

    def test_rows_without_source_are_tool_lane(self) -> None:
        row = {"ts": _T1, "query": "legacy q", "verdict": "adopted"}
        assert rh.usage_counts([row], None)["lanes"]["tool"]["adopted"] == 1


class TestBuildItems:
    def test_keeps_only_gold_the_index_holds(self) -> None:
        rows = [{"query": "q", "gold_paths": ["src/a.py", "tmp/gone.txt"]}]
        items, aside = rh.build_items(rows, {"src/a.py"})
        assert items == [{"query": "q", "gold": ["src/a.py"]}]
        assert aside["unindexed"] == 0

    def test_item_with_no_indexed_gold_is_a_coverage_gap(self) -> None:
        rows = [{"query": "q", "gold_paths": ["notes/skill.md"]}]
        items, aside = rh.build_items(rows, {"src/a.py"})
        assert items == []
        assert aside["unindexed"] == 1

    def test_external_only_gold_is_not_usable(self) -> None:
        rows = [{"query": "q", "gold_paths": ["external:/tmp/a.png"]}]
        items, aside = rh.build_items(rows, set())
        assert items == []
        assert aside["no_usable_gold"] == 1

    def test_harness_rows_are_dropped(self) -> None:
        rows = [{"query": _NOTE, "gold_paths": ["src/a.py"]}]
        items, aside = rh.build_items(rows, {"src/a.py"})
        assert items == []
        assert aside["harness"] == 1

    def test_same_question_twice_is_one_item(self) -> None:
        rows = [
            {"query": "q", "gold_paths": ["src/a.py"]},
            {"query": "q", "gold_paths": ["src/a.py", "src/b.py"]},
        ]
        items, _ = rh.build_items(rows, {"src/a.py", "src/b.py"})
        assert items == [{"query": "q", "gold": ["src/a.py", "src/b.py"]}]


class TestScoring:
    def test_rank_of_first_gold_hit(self) -> None:
        assert rh.gold_rank(["x.py", "src/a.py"], ["src/a.py"]) == 2
        assert rh.gold_rank(["x.py"], ["src/a.py"]) is None

    def test_metrics(self) -> None:
        out = rh.replay_metrics([1, 4, None, None])
        assert out == {"n": 4, "found": 0.5, "top3": 0.25, "top5": 0.5, "mrr": 0.3125}

    def test_empty_set_reports_only_its_size(self) -> None:
        assert rh.replay_metrics([]) == {"n": 0}


class TestParseSince:
    def test_date_only_is_midnight_utc(self) -> None:
        assert rh.parse_since("2026-01-15") == datetime(2026, 1, 15, tzinfo=timezone.utc)

    def test_none_stays_none(self) -> None:
        assert rh.parse_since(None) is None


def _prompt(text: str, stamp: str) -> dict:
    return {"type": "user", "timestamp": stamp, "message": {"content": text}}


def _tool(name: str, tool_input: dict, stamp: str = _T0) -> dict:
    return {"type": "assistant", "timestamp": stamp, "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": tool_input}]}}


class TestColdGold:
    """Gold the session had already opened is not something a search missed."""

    def _index(self) -> dict:
        return rh.session_index([
            _prompt("첫 질문 one two", "2026-01-10T00:00:00Z"),
            _tool("Read", {"file_path": "/repo/src/a.py"}),
            _prompt("둘째 질문 three four", "2026-01-10T00:05:00Z"),
            _tool("mcp__hybrid-search__hybrid_search", {"query": "agent wrote this"}),
            _tool("Edit", {"file_path": "/repo/src/b.py"}),
        ])

    def test_file_opened_earlier_in_the_session_is_warm(self) -> None:
        rows = [{"ts": _T1, "query": "둘째 질문 three four", "gold_paths": ["src/a.py"]}]
        kept, aside = rh.cold_rows(rows, self._index())
        assert kept == []
        assert aside == {"unmatched": 0, "warm": 1}

    def test_only_the_new_files_stay_gold(self) -> None:
        rows = [{"ts": _T1, "query": "둘째 질문 three four",
                 "gold_paths": ["src/a.py", "src/b.py"]}]
        kept, _ = rh.cold_rows(rows, self._index())
        assert [r["gold_paths"] for r in kept] == [["src/b.py"]]

    def test_first_question_of_a_session_is_all_cold(self) -> None:
        rows = [{"ts": _T1, "query": "첫 질문 one two", "gold_paths": ["src/a.py"]}]
        kept, _ = rh.cold_rows(rows, self._index())
        assert kept[0]["gold_paths"] == ["src/a.py"]

    def test_tool_lane_rows_match_on_the_search_query(self) -> None:
        rows = [{"ts": _T1, "query": "agent wrote this", "gold_paths": ["src/a.py", "src/b.py"]}]
        kept, _ = rh.cold_rows(rows, self._index())
        # a.py was read before the search; b.py came after it.
        assert kept[0]["gold_paths"] == ["src/b.py"]

    def test_question_no_transcript_holds_is_left_out(self) -> None:
        rows = [{"ts": _T1, "query": "아무 데도 없는 질문", "gold_paths": ["src/a.py"]}]
        kept, aside = rh.cold_rows(rows, self._index())
        assert kept == []
        assert aside["unmatched"] == 1

    def test_repeated_question_uses_the_asking_before_the_row(self) -> None:
        index = rh.session_index([
            _prompt("같은 질문 again", "2026-01-10T00:00:00Z"),
            _tool("Read", {"file_path": "/repo/src/a.py"}),
            _prompt("같은 질문 again", "2026-01-12T00:00:00Z"),
        ])
        early = {"ts": "2026-01-10T00:10:00+00:00", "query": "같은 질문 again",
                 "gold_paths": ["src/a.py"]}
        late = {"ts": "2026-01-12T00:10:00+00:00", "query": "같은 질문 again",
                "gold_paths": ["src/a.py"]}
        kept, aside = rh.cold_rows([early, late], index)
        assert len(kept) == 1 and kept[0]["ts"] == early["ts"]
        assert aside["warm"] == 1
