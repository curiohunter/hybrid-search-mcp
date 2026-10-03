"""Cases for judging whether a turn used its pre-fetch. Fixtures are synthetic."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "prefetch_use_judge.py"
_spec = importlib.util.spec_from_file_location("prefetch_use_judge", _PATH)
pj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pj)

_BLOCK = "[hybrid-search pre-fetch] 2 hits\n1. `src/refund.py:10-40` (function refund)\n   body"


def _prompt(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _inject(text: str = _BLOCK, event: str = "UserPromptSubmit") -> dict:
    return {"type": "attachment",
            "attachment": {"type": "hook_additional_context", "hookEvent": event,
                           "content": [text]}}


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool(name: str, tool_input: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": tool_input}]}}


class TestTurns:
    def test_turn_without_a_prefetch_block_is_not_a_case(self) -> None:
        records = [_prompt("환불 흐름 설명"), _say("답")]
        assert pj.turns(records) == []

    def test_other_hooks_context_is_not_a_prefetch(self) -> None:
        records = [_prompt("환불 흐름 설명"), _inject("route hint only"), _say("답")]
        assert pj.turns(records) == []

    def test_harness_prompt_is_dropped(self) -> None:
        records = [_prompt("<task-notification>x</task-notification>"), _inject(), _say("답")]
        assert pj.turns(records) == []

    def test_quiet_turn_is_no_followup(self) -> None:
        turn = pj.turns([_prompt("환불 흐름 설명"), _inject(), _say("답")])[0]
        assert pj.kind_of(turn) == "no_followup"
        assert turn["answer"] == ["답"]

    def test_opening_a_served_file_is_adopted(self) -> None:
        turn = pj.turns([_prompt("환불 흐름 설명"), _inject(),
                         _tool("Read", {"file_path": "/repo/src/refund.py"})])[0]
        assert pj.kind_of(turn) == "adopted"

    def test_opening_something_else_is_other(self) -> None:
        turn = pj.turns([_prompt("환불 흐름 설명"), _inject(),
                         _tool("Read", {"file_path": "/repo/src/else.py"})])[0]
        assert pj.kind_of(turn) == "other"


def _pool() -> list[dict]:
    pool = []
    for i in range(8):
        pool.append({"position": i, "prompt": f"q{i}", "injected": f"block {i}", "reads": [],
                     "searches": [], "actions": [], "answer": [f"a{i}"],
                     "kind": "no_followup", "source": f"s{i}.jsonl"})
    pool.append({"position": 9, "prompt": "qp", "injected": "block p", "reads": ["x"],
                 "searches": [], "actions": ["Read x"], "answer": ["ap"],
                 "kind": "adopted", "source": "sp.jsonl"})
    return pool


class TestBuildCases:
    def test_kinds_and_determinism(self) -> None:
        cases, key = pj.build_cases(_pool(), n=4, x=2)
        again, key2 = pj.build_cases(_pool(), n=4, x=2)
        assert cases == again and key == key2
        kinds = sorted(k["kind"] for k in key.values())
        assert kinds == ["N", "N", "N", "N", "P", "X", "X"]

    def test_negative_case_carries_another_turns_block(self) -> None:
        cases, key = pj.build_cases(_pool(), n=4, x=2)
        by_id = {c["id"]: c for c in cases}
        for case_id, row in key.items():
            if row["kind"] == "X":
                own = f"block {row['position']}"
                assert by_id[case_id]["injected"] != own

    def test_key_is_kept_out_of_the_case(self) -> None:
        cases, _ = pj.build_cases(_pool(), n=4, x=2)
        assert all(set(c) == {"id", "question", "injected", "actions", "answer"} for c in cases)


class TestSettle:
    _RECORDS = [_prompt("예전 질문"), _say("정산은 매월 말일에 닫는다"), _prompt("새 질문")]

    def _records_for(self, _source: str) -> list[dict]:
        return self._RECORDS

    def test_yes_with_evidence_new_to_the_session_stays_yes(self) -> None:
        verdict = {"used": "yes", "evidence": "환불은 3일 안에 처리"}
        row = {"source": "s", "position": 2}
        assert pj.settle(verdict, row, self._records_for) == "yes"

    def test_yes_on_something_already_said_is_downgraded(self) -> None:
        verdict = {"used": "yes", "evidence": "정산은 매월 말일에 닫는다"}
        row = {"source": "s", "position": 2}
        assert pj.settle(verdict, row, self._records_for) == "already_in_session"

    def test_garbage_verdict_is_unclear(self) -> None:
        assert pj.settle({"used": "maybe"}, {"source": "s", "position": 0},
                         self._records_for) == "unclear"


class TestTally:
    _KEY = {"a": {"kind": "N"}, "b": {"kind": "N"}, "c": {"kind": "P"}, "d": {"kind": "X"}}

    def test_rates_and_validity(self) -> None:
        out = pj.tally({"a": "yes", "b": "no", "c": "yes", "d": "no"}, self._KEY)
        assert out["n_yes_rate"] == 0.5
        assert out["p_yes_rate_raw"] == 1.0 and out["x_yes_rate_raw"] == 0.0
        assert out["judge_valid"] is True

    def test_lenient_judge_is_invalid(self) -> None:
        key = {"a": {"kind": "N"}, "d": {"kind": "X"}}
        assert pj.tally({"a": "yes", "d": "yes"}, key)["judge_valid"] is False

    def test_session_check_does_not_count_against_the_judge(self) -> None:
        settled = {"a": "already_in_session", "b": "no", "c": "already_in_session", "d": "no"}
        said = {"a": "yes", "b": "no", "c": "yes", "d": "no"}
        out = pj.tally(settled, self._KEY, said)
        assert out["judge_valid"] is True
        assert out["n_yes_rate"] == 0.0 and out["n_yes_rate_raw"] == 0.5
