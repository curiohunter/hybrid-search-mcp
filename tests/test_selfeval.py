"""selfeval — turn-slice scoring of hybrid_search adoption vs betrayal."""

from __future__ import annotations

import json

from hybrid_search.memory import selfeval

SEARCH_TOOL = "mcp__hybrid-search__hybrid_search"


def _search_call(uid: str, query: str, paths: list[str]) -> list[dict]:
    """A tool_use + its tool_result, the way Claude transcripts record them."""
    payload = json.dumps({"results": [{"file_path": p} for p in paths]})
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": uid, "name": SEARCH_TOOL,
                     "input": {"query": query}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": uid,
                     "content": [{"type": "text", "text": payload}]},
                ]
            },
        },
    ]


def _tool_call(name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "x", "name": name, "input": tool_input},
            ]
        },
    }


class TestExtractAndScore:
    def test_adopted_records_rank(self) -> None:
        records = _search_call("t1", "환불 흐름 알려줘", ["src/a.py", "src/b.py"]) + [
            _tool_call("Read", {"file_path": "/repo/src/b.py"}),
        ]
        events = selfeval.extract_turn_events(records)
        assert len(events) == 1
        row = selfeval.score_event(events[0])
        assert row["verdict"] == "adopted"
        assert row["adopted_rank"] == 2
        assert row["outside_reads"] == []

    def test_read_outside_results_is_betrayal(self) -> None:
        records = _search_call("t1", "환불 흐름 알려줘", ["src/a.py"]) + [
            _tool_call("Read", {"file_path": "/repo/src/other.py"}),
        ]
        row = selfeval.score_event(selfeval.extract_turn_events(records)[0])
        assert row["verdict"] == "betrayed"
        assert row["outside_reads"] == ["/repo/src/other.py"]

    def test_grep_after_search_is_betrayal(self) -> None:
        records = _search_call("t1", "환불 흐름 알려줘", ["src/a.py"]) + [
            _tool_call("Grep", {"pattern": "refund"}),
        ]
        row = selfeval.score_event(selfeval.extract_turn_events(records)[0])
        assert row["verdict"] == "betrayed"
        assert row["greps_after"] == 1

    def test_adopted_plus_outside_read_is_mixed(self) -> None:
        records = _search_call("t1", "q", ["src/a.py"]) + [
            _tool_call("Read", {"file_path": "/repo/src/a.py"}),
            _tool_call("Read", {"file_path": "/repo/src/other.py"}),
        ]
        row = selfeval.score_event(selfeval.extract_turn_events(records)[0])
        assert row["verdict"] == "mixed"
        assert row["adopted_rank"] == 1

    def test_no_followup(self) -> None:
        records = _search_call("t1", "q", ["src/a.py"])
        row = selfeval.score_event(selfeval.extract_turn_events(records)[0])
        assert row["verdict"] == "no_followup"

    def test_followups_attribute_to_most_recent_search(self) -> None:
        records = (
            _search_call("t1", "first", ["src/a.py"])
            + _search_call("t2", "second", ["src/b.py"])
            + [_tool_call("Read", {"file_path": "/repo/src/b.py"})]
        )
        events = selfeval.extract_turn_events(records)
        assert len(events) == 2
        first = selfeval.score_event(events[0])
        second = selfeval.score_event(events[1])
        assert first["verdict"] == "no_followup"
        assert second["verdict"] == "adopted"

    def test_reads_before_any_search_are_ignored(self) -> None:
        records = [_tool_call("Read", {"file_path": "/repo/src/pre.py"})] + _search_call(
            "t1", "q", ["src/a.py"]
        )
        row = selfeval.score_event(selfeval.extract_turn_events(records)[0])
        assert row["verdict"] == "no_followup"

    def test_unparseable_result_scores_empty_paths(self) -> None:
        records = [
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": SEARCH_TOOL,
                     "input": {"query": "q"}},
                ]},
            },
            {
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "Error: server crashed"},
                ]},
            },
            _tool_call("Read", {"file_path": "/repo/src/x.py"}),
        ]
        row = selfeval.score_event(selfeval.extract_turn_events(records)[0])
        assert row["n_results"] == 0
        assert row["verdict"] == "betrayed"


class TestRecordTurn:
    def test_persists_events_and_harvests_betrayals(self, tmp_path) -> None:
        records = _search_call("t1", "환불 흐름", ["src/a.py"]) + [
            _tool_call("Read", {"file_path": str(tmp_path / "src/gold.py")}),
        ]
        written = selfeval.record_turn(tmp_path, records)
        assert written == 1

        events_file = tmp_path / ".hybrid-search/selfeval/events.jsonl"
        harvested_file = tmp_path / ".hybrid-search/selfeval/harvested.jsonl"
        assert events_file.is_file()
        row = json.loads(events_file.read_text().splitlines()[0])
        assert row["verdict"] == "betrayed"
        gold = json.loads(harvested_file.read_text().splitlines()[0])
        assert gold["query"] == "환불 흐름"
        # Stored root-relative so the regression set survives a repo move.
        assert gold["gold_paths"] == ["src/gold.py"]

    def test_adopted_turn_is_not_harvested(self, tmp_path) -> None:
        records = _search_call("t1", "q", ["src/a.py"]) + [
            _tool_call("Read", {"file_path": "/repo/src/a.py"}),
        ]
        selfeval.record_turn(tmp_path, records)
        assert not (tmp_path / ".hybrid-search/selfeval/harvested.jsonl").exists()

    def test_no_search_writes_nothing(self, tmp_path) -> None:
        records = [_tool_call("Read", {"file_path": "/repo/src/a.py"})]
        assert selfeval.record_turn(tmp_path, records) == 0
        assert not (tmp_path / ".hybrid-search/selfeval").exists()

    def test_empty_query_skipped(self, tmp_path) -> None:
        records = _search_call("t1", "", ["src/a.py"])
        assert selfeval.record_turn(tmp_path, records) == 0

    def test_never_raises_on_garbage(self, tmp_path) -> None:
        assert selfeval.record_turn(tmp_path, [{"type": None}, {}, {"message": 3}]) == 0


class TestSummarize:
    def test_counts_and_scoreline(self, tmp_path) -> None:
        selfeval.record_turn(
            tmp_path,
            _search_call("t1", "q1", ["src/a.py"])
            + [_tool_call("Read", {"file_path": "/repo/src/a.py"})],
        )
        selfeval.record_turn(
            tmp_path,
            _search_call("t2", "q2", ["src/a.py"])
            + [_tool_call("Read", {"file_path": "/repo/src/gold.py"})],
        )
        stats = selfeval.summarize(tmp_path)
        assert stats is not None
        assert stats["total"] == 2
        assert stats["adopted"] == 1
        assert stats["betrayed"] == 1
        assert stats["harvested_total"] == 1

        line = selfeval.format_summary_line(tmp_path)
        assert line.startswith("[selfeval")
        assert "harvested 1" in line

    def test_no_data_is_silent(self, tmp_path) -> None:
        assert selfeval.summarize(tmp_path) is None
        assert selfeval.format_summary_line(tmp_path) == ""


class TestPathMatching:
    def test_relative_result_matches_absolute_read(self) -> None:
        assert selfeval._paths_match("/repo/src/hybrid_search/cli.py", "src/hybrid_search/cli.py")

    def test_basename_alone_does_not_match(self) -> None:
        assert not selfeval._paths_match("/repo/other/cli.py", "src/hybrid_search/cli.py")
