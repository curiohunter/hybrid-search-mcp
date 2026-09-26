"""The query-grounded judge: which questions are judged, and how verdicts count.

docs/plans/2026-09-25-query-grounded-judgment.md — two top-10 lists per
question, the judge sees them blind as X/Y, twice with the sides swapped.
Every fixture here is synthetic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "query_judge.py"
_spec = importlib.util.spec_from_file_location("query_judge", _PATH)
qj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qj)


def _item(cid: str, content: str = "", node_type: str = "conv_turn") -> dict:
    return {"chunk_id": cid, "node_type": node_type, "file_path": f"f/{cid}",
            "content": content, "snippet": "", "file_mtime": None}


def _qa(question: str, answer: str | None, ts: str = "2026-09-20T01:00:00+00:00") -> str:
    body = f'---\nquery: "{question.splitlines()[0]}"\ntimestamp: {ts}\n---\n\n' \
           f"# Q: {question}\n\n- **query_type**: TURN\n\n"
    if answer is not None:
        body += f"## Answer excerpt\n\n{answer}\n\n"
    return body + "## Top results\n\n1. quoted ## Answer excerpt elsewhere\n"


class TestListsDiffer:
    def test_same_members_same_order_is_a_tie(self) -> None:
        assert not qj.lists_differ(["a", "b"], ["a", "b"])

    def test_order_alone_differs(self) -> None:
        assert qj.lists_differ(["a", "b"], ["b", "a"])

    def test_member_differs(self) -> None:
        assert qj.lists_differ(["a", "b"], ["a", "c"])

    def test_length_differs(self) -> None:
        assert qj.lists_differ(["a"], ["a", "b"])


class TestSideAssignment:
    def test_fixed_per_question(self) -> None:
        assert [qj.g_is_x("p:x1") for _ in range(3)] == [qj.g_is_x("p:x1")] * 3

    def test_independent_of_other_questions(self) -> None:
        # Seeded by id, not position: the order of the question file is irrelevant.
        ids = [f"p:{i}" for i in range(50)]
        assert [qj.g_is_x(i) for i in ids] == [qj.g_is_x(i) for i in reversed(ids)][::-1]

    def test_roughly_balanced(self) -> None:
        sides = [qj.g_is_x(f"p:{i}") for i in range(400)]
        assert 160 < sum(sides) < 240

    def test_seed_matters(self) -> None:
        ids = [f"p:{i}" for i in range(40)]
        assert [qj.g_is_x(i) for i in ids] != [qj.g_is_x(i, seed=1) for i in ids]


class TestUnblinding:
    @pytest.mark.parametrize("verdict,g_x,arm", [
        ("X", True, "G"), ("Y", True, "M"), ("X", False, "M"), ("Y", False, "G"),
        ("same", True, "same"), ("same", False, "same"),
    ])
    def test_to_arm(self, verdict: str, g_x: bool, arm: str) -> None:
        assert qj.to_arm(verdict, g_x) == arm

    def test_agreeing_passes_keep_the_arm(self) -> None:
        assert qj.combine("G", "G") == "G"

    @pytest.mark.parametrize("a1,a2", [("G", "M"), ("G", "same"), ("same", "M")])
    def test_any_disagreement_is_same(self, a1: str, a2: str) -> None:
        assert qj.combine(a1, a2) == "same"


class TestReference:
    def test_earlier_first_hit_wins(self) -> None:
        assert qj.reference_arm(2, 5) == "M"
        assert qj.reference_arm(None, 8) == "G"

    def test_equal_or_both_missing_is_same(self) -> None:
        assert qj.reference_arm(3, 3) == "same"
        assert qj.reference_arm(None, None) == "same"

    def test_first_hit_rank_reads_content_and_snippet(self) -> None:
        items = [_item("a", "nothing"), {**_item("b"), "snippet": "has PHRASE"}]
        assert qj.first_hit_rank(items, ["PHRASE"]) == 2
        assert qj.first_hit_rank(items, ["absent"]) is None

    def test_seen_rank_ignores_text_the_judge_is_not_shown(self) -> None:
        # The phrase sits deep in a long pasted question: in the content,
        # past the 200-char question window, outside the snippet.
        long_q = "x " * 300 + "DEEP PHRASE"
        items = [_item("a", _qa(long_q, "short"), node_type="qa_log"),
                 _item("b", "has DEEP PHRASE")]
        assert qj.first_hit_rank(items, ["DEEP PHRASE"]) == 1
        assert qj.seen_hit_rank(items, ["DEEP PHRASE"]) == 2

    def test_seen_rank_matches_across_collapsed_whitespace(self) -> None:
        items = [_item("a", "two\n  words")]
        assert qj.seen_hit_rank(items, ["two\nwords"]) == 1

    def test_gold_metrics(self) -> None:
        m = qj.gold_metrics([1, 4, None, 2])
        assert m == {"n": 4, "found": 0.75, "top3": 0.5, "mrr": round((1 + .25 + .5) / 4, 4)}


class TestRender:
    def test_qa_shows_whole_question_and_own_answer(self) -> None:
        text = qj.render_item(1, _item("q", _qa("line one\nline two", "the answer"),
                                        node_type="qa_log"))
        assert "line one line two" in text and "답: the answer" in text
        assert "2026-09-20" in text and "quoted" not in text

    def test_answerless_qa_says_so(self) -> None:
        # The quotation under Top results holds the heading string — it must not count.
        text = qj.render_item(1, _item("q", _qa("q only", None), node_type="qa_log"))
        assert text.rstrip().endswith("답: (답 없음)") and "당시" not in text

    def test_qa_window_carries_no_kind_metadata(self) -> None:
        # The leak found before judging: the search window sat on the
        # frontmatter and showed the trigger (answer vs pre-fetch) in the open.
        content = ('---\nquery: "how is the widget cached"\nquery_type: TURN\n'
                   "total_chunks_searched: 0\ntrigger: stop_hook\n"
                   'tools_used: ["Edit"]\nmemory_type: decision\n---\n\n'
                   "# Q: how is the widget cached\n\n- **query_type**: TURN\n"
                   "- **trigger**: stop_hook\n- **chunks_searched**: 0\n\n"
                   "## Answer excerpt\n\nThe widget cache is keyed by id.\n")
        text = qj.render_item(1, {**_item("q", content, node_type="qa_log"),
                                  "snippet": "total_chunks_searched: 0 trigger: stop_hook"},
                              "widget cache")
        for key in ("trigger", "stop_hook", "query_type", "total_chunks_searched",
                    "tools_used", "memory_type", "chunks_searched"):
            assert key not in text, key
        assert "스니펫:" in text and "widget cache is keyed" in text

    def test_quotation_in_the_records_own_text_stays(self) -> None:
        content = _qa("q", "see > [qa - stop_hook - decision - 1d ago] quoted")
        text = qj.render_item(1, _item("q", content, node_type="qa_log"), "quoted")
        assert "[qa - stop_hook" in text

    def test_non_qa_snippet_fallback_drops_bracket_head(self) -> None:
        item = {**_item("c", ""), "snippet": "[code - indexed]\nbody [sic]"}
        assert qj.render_item(1, item).endswith("body [sic]")

    def test_text_is_capped(self) -> None:
        text = qj.render_item(3, _item("c", "가" * 1000))
        assert text.startswith("3. [conv_turn] f/c")
        assert text.count("가") == qj.TEXT_CHARS


def _dump(results: dict[str, list[str]]) -> dict:
    return {"results": {q: [_item(c, f"text {c}") for c in ids]
                        for q, ids in results.items()}}


class TestBuildCases:
    QS = [
        {"id": "p:1", "set": "probe", "query": "q1", "probe_chunk": "a"},
        {"id": "p:2", "set": "probe", "query": "q2", "probe_chunk": "z"},
        {"id": "A:C1", "set": "A", "query": "g1", "any_of": ["text c"]},
    ]

    def test_only_differing_questions_become_cases(self) -> None:
        m = _dump({"p:1": ["a", "b"], "p:2": ["x", "y"], "A:C1": ["b", "c"]})
        g = _dump({"p:1": ["a", "b"], "p:2": ["y", "x"], "A:C1": ["c", "b"]})
        built = qj.build_cases(self.QS, m, g)
        assert [c["qid"] for c in built["cases"]] == ["p:2", "A:C1"]
        assert built["ties"] == ["p:1"]
        assert built["gold_ranks"]["A:C1"] == {"set": "A", "M": 2, "G": 1,
                                                "M_seen": 2, "G_seen": 1}
        assert built["self_found"]["p:1"] == {"M": True, "G": True}

    def test_noise_is_set_aside_not_judged(self) -> None:
        m = _dump({"p:1": ["a", "b"], "p:2": ["x", "y"], "A:C1": ["b"]})
        m2 = _dump({"p:1": ["b", "a"], "p:2": ["x", "y"], "A:C1": ["b"]})
        g = _dump({"p:1": ["a", "c"], "p:2": ["x", "y"], "A:C1": ["b"]})
        built = qj.build_cases(self.QS, m, g, noise=m2)
        assert built["noisy"] == ["p:1"] and built["cases"] == []

    def test_render_case_swaps_sides(self) -> None:
        case = {"cid": "c1", "query": "q", "g_is_x": True}
        m, g = [_item("mm", "from M")], [_item("gg", "from G")]
        p1 = qj.render_case(case, m, g, swapped=False)
        p2 = qj.render_case(case, m, g, swapped=True)
        assert p1.index("from G") < p1.index("### Y") < p1.index("from M")
        assert p2.index("from M") < p2.index("### Y") < p2.index("from G")


class TestScore:
    def _cases(self) -> dict:
        return {
            "cases": [
                {"cid": "c1", "qid": "p:1", "set": "probe", "query": "", "g_is_x": True},
                {"cid": "c2", "qid": "p:2", "set": "probe", "query": "", "g_is_x": False},
                {"cid": "c3", "qid": "p:3", "set": "probe", "query": "", "g_is_x": True},
                {"cid": "c4", "qid": "A:C1", "set": "A", "query": "", "g_is_x": True},
                {"cid": "c5", "qid": "A:C2", "set": "A", "query": "", "g_is_x": False},
            ],
            "ties": ["p:9", "A:C9"], "noisy": ["p:8"],
            "gold_ranks": {
                "A:C1": {"set": "A", "M": 4, "G": 1, "M_seen": 4, "G_seen": 1},
                "A:C2": {"set": "A", "M": 1, "G": None, "M_seen": 1, "G_seen": None},
                "A:C9": {"set": "A", "M": 2, "G": 2, "M_seen": 2, "G_seen": 2}},
            "self_found": {"p:1": {"M": True, "G": True}, "p:2": {"M": False, "G": True}},
        }

    def test_tally_unblinds_both_passes(self) -> None:
        v = {
            # c1: G is X in pass 1, Y in pass 2 — both pick G.
            # c2: G is Y in pass 1, X in pass 2 — both pick M.
            # c3: flips with the sides → same.
            "1": {"c1": "X", "c2": "X", "c3": "X", "c4": "X", "c5": "same"},
            "2": {"c1": "Y", "c2": "Y", "c3": "X", "c4": "Y", "c5": "X"},
        }
        r = qj.score(self._cases(), v)
        p = r["probes"]
        assert (p["G"], p["M"], p["same"], p["ties"], p["noisy"]) == (1, 1, 1, 1, 1)
        assert p["g_rate"] == 0.5 and p["order_consistency"] == round(2 / 3, 4)
        c = r["calibration"]
        # c4 agrees (G, reference G); c5 judge same vs reference M — a disagreement.
        assert (c["decisive_reference"], c["agree"], c["agreement"]) == (2, 1, 0.5)
        assert r["gold"]["A"]["G"]["found"] == round(2 / 3, 4)
        assert r["self_found"] == {"M": 1, "G": 2}

    def test_calibration_reads_the_rendered_ranks(self) -> None:
        cases = self._cases()
        # Content says M wins C1; what the judge was shown says neither does.
        cases["gold_ranks"]["A:C1"].update({"M": 1, "G": 3, "M_seen": None, "G_seen": None})
        v = {"1": {"c4": "X", "c5": "same"}, "2": {"c4": "Y", "c5": "X"}}
        c = qj.score(cases, v)["calibration"]
        assert [r["qid"] for r in c["rows"]] == ["A:C2"]
        assert c["hidden_answers"] == {"M": 1, "G": 1}

    def test_calibration_targets(self) -> None:
        assert qj.calibration_targets(self._cases()) == ["A:C1", "A:C2"]

    def test_missing_or_invalid_verdict_is_unjudged(self) -> None:
        r = qj.score(self._cases(), {"1": {"c1": "X"}, "2": {"c1": "maybe"}})
        assert r["probes"]["unjudged"] == 3 and r["probes"]["judged"] == 0

    def test_parse_verdicts_drops_invalid(self) -> None:
        text = '[{"q": "c1", "better": "X", "why": "ok"}, {"q": "c2", "better": "both"}]'
        assert qj.parse_verdicts(text) == {"c1": "X"}


def test_registered_prompt_matches_runner() -> None:
    """The judge prompt is pre-registered in the plan (§3.2); the runner must
    send exactly that text, or the registration means nothing."""
    import re
    plan = (_PATH.parents[1] / "docs" / "plans"
            / "2026-09-25-query-grounded-judgment.md").read_text(encoding="utf-8")
    registered = re.search(r"### 3\.2.*?```\n(.*?)```", plan, re.S).group(1)
    assert registered == qj.JUDGE_PROMPT


class TestDegradedDumps:
    def test_clean_dumps_pass(self) -> None:
        assert qj.degraded_dumps({"M": {"degraded": []}, "G": {"degraded": []}}) == {}

    def test_degraded_searches_are_counted(self) -> None:
        assert qj.degraded_dumps({"M": {"degraded": []}, "G": {"degraded": ["p:1", "p:2"]}}) == {"G": 2}

    def test_dump_without_the_field_is_unknown_not_clean(self) -> None:
        assert qj.degraded_dumps({"M": {"results": {}}}) == {"M": -1}
