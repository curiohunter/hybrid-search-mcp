"""selfeval v1.2 — shell reads (defect A) and quoted-hit adoption (defect B).

Plan: docs/plans/2026-09-27-selfeval-v1.2.md. All transcripts are synthetic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hybrid_search.memory import hook_runtime, selfeval
from hybrid_search.memory.selfeval_evidence import (
    is_quoted_path,
    quoted_adopted,
    shell_actions,
)

SEARCH_TOOL = "mcp__hybrid-search__hybrid_search"
HASH = "3f9c2ab71d"


def _tool_call(name: str, tool_input: dict, uid: str = "x") -> dict:
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": uid, "name": name, "input": tool_input},
        ]},
    }


def _tool_result(uid: str, text: str) -> dict:
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": uid,
             "content": [{"type": "text", "text": text}]},
        ]},
    }


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _search(uid: str, query: str, results: list[dict]) -> list[dict]:
    return [
        _tool_call(SEARCH_TOOL, {"query": query}, uid),
        _tool_result(uid, json.dumps({"results": results})),
    ]


def _prefetch_row(tmp_path, turn, *, paths=("src/a.py",), quoted=None, prompt="q"):
    selfeval.record_prefetch(
        tmp_path, query=prompt, paths=list(paths), session_key="s", quoted=quoted
    )
    selfeval.record_turn(tmp_path, turn, session_key="s", prompt=prompt)
    rows = [json.loads(line) for line in
            (tmp_path / ".hybrid-search/selfeval/events.jsonl").read_text().splitlines()]
    return rows[-1]


class TestShellActions:
    @pytest.mark.parametrize("command, files, searches", [
        ("grep -n foo src/a.py", ["src/a.py"], 0),
        ("sed -n 1,80p src/a.py", ["src/a.py"], 0),
        ("cd repo && sed -n '10,20p' src/a.py", ["src/a.py"], 0),
        ("grep -A 3 foo a.py b.md", ["a.py", "b.md"], 0),
        ("grep -e foo -e bar a.py", ["a.py"], 0),
        ("head -n 50 README.md", ["README.md"], 0),
        ("cat a.py | grep foo", ["a.py"], 0),
        ("grep -rn foo src/", [], 1),
        ("rg foo", [], 1),
        ("rg -g '*.py' foo", [], 1),
        ("grep foo *.py", [], 1),
        ("find . -name a.py", [], 1),
        ("grep -n foo src/a.py src/", ["src/a.py"], 1),
        ("git log --oneline | grep fix", [], 0),
        ("npm run build", [], 0),
        ("grep -n x a.py 2>/dev/null", ["a.py"], 0),
        ("cat a.py > out.txt", ["a.py"], 0),
        ("cat > /tmp/build.py <<'EOF'\nimport os.path\nopen(a.txt)\nEOF\n"
         "python3 /tmp/build.py", [], 0),
        ("cd x && cat <<EOF > notes.md\nsee config.yaml\nEOF", [], 0),
        ("sed -n 1,5p a.py\ngrep -rn y src/", ["a.py"], 1),
        ('grep "a;b" a.py', ["a.py"], 0),
    ])
    def test_classifies(self, command, files, searches) -> None:
        got_files, got_searches = shell_actions(command)
        assert got_files == files
        assert len(got_searches) == searches

    def test_unparseable_command_falls_back_to_prefix_rule(self) -> None:
        files, searches = shell_actions('grep "unterminated src/a.py')
        assert files == [] and len(searches) == 1


class TestDefectAShellReads:
    def test_grep_on_a_result_file_is_adoption(self, tmp_path) -> None:
        row = _prefetch_row(
            tmp_path, [_tool_call("Bash", {"command": "grep -n refund src/b.py"})],
            paths=("src/a.py", "src/b.py"),
        )
        assert row["verdict"] == "adopted"
        assert row["adopted_rank"] == 2
        assert row["greps_after"] == 0

    def test_sed_on_a_result_file_is_adoption(self, tmp_path) -> None:
        row = _prefetch_row(tmp_path, [_tool_call("Bash", {"command": "sed -n 1,40p src/a.py"})])
        assert row["verdict"] == "adopted"

    def test_grep_on_an_outside_file_is_betrayal(self, tmp_path) -> None:
        row = _prefetch_row(tmp_path, [_tool_call("Bash", {"command": "grep -n x src/other.py"})])
        assert row["verdict"] == "betrayed"
        assert row["outside_reads"] == ["src/other.py"]

    def test_global_search_is_still_betrayal(self, tmp_path) -> None:
        row = _prefetch_row(tmp_path, [_tool_call("Bash", {"command": "grep -rn x src/"})])
        assert row["verdict"] == "betrayed"
        assert row["greps_after"] == 1

    def test_result_read_plus_global_search_is_mixed(self, tmp_path) -> None:
        row = _prefetch_row(tmp_path, [
            _tool_call("Bash", {"command": "grep -n x src/a.py"}),
            _tool_call("Bash", {"command": "rg y"}),
        ])
        assert row["verdict"] == "mixed"

    def test_cat_outside_results_now_counts_like_read(self, tmp_path) -> None:
        """Documented side effect: symmetric with the Read tool."""
        row = _prefetch_row(tmp_path, [_tool_call("Bash", {"command": "cat docs/x.md"})])
        assert row["verdict"] == "betrayed"
        assert row["outside_reads"] == ["docs/x.md"]

    def test_tool_lane_scores_shell_reads_too(self, tmp_path) -> None:
        turn = _search("u1", "q", [{"file_path": "src/a.py"}]) + [
            _tool_call("Bash", {"command": "grep -n foo src/a.py"}),
        ]
        selfeval.record_turn(tmp_path, turn)
        row = json.loads(
            (tmp_path / ".hybrid-search/selfeval/events.jsonl").read_text().splitlines()[-1]
        )
        assert row["verdict"] == "adopted"


class TestQuotedAdoptedRule:
    def test_id_in_answer_adopts(self) -> None:
        assert quoted_adopted(f"커밋 {HASH} 에서 게이트를 넣음", f"{HASH} 에서 바뀌었습니다", "q")

    def test_id_from_the_prompt_does_not_count(self) -> None:
        assert not quoted_adopted(f"커밋 {HASH}", f"{HASH} 맞습니다", f"{HASH} 뭐였지?")

    def test_id_also_in_other_tool_output_does_not_count(self) -> None:
        assert not quoted_adopted(f"커밋 {HASH}", f"{HASH} 입니다", f"q\n{HASH} [fix] x")

    def test_two_shared_phrases_adopt(self) -> None:
        excerpt = (
            "the widget cache expires after thirty minutes "
            "because the upstream quota resets hourly"
        )
        answer = "It expires after thirty minutes because the upstream quota resets hourly."
        assert quoted_adopted(excerpt, answer, "why does it expire")

    def test_one_phrase_is_not_enough(self) -> None:
        """A 3-word run is one phrase; a 4-word run would already be two."""
        excerpt = "the widget cache expires after thirty minutes"
        answer = "Yes, it expires after thirty days."
        assert not quoted_adopted(excerpt, answer, "q")

    def test_paraphrase_is_missed_by_design(self) -> None:
        assert not quoted_adopted(
            "the widget cache expires after thirty minutes",
            "Entries live for half an hour.", "q",
        )

    @pytest.mark.parametrize("path, quoted", [
        (".git-history/commits", True),
        (".hybrid-search/qa/2026/01/01-000000-abcd.md", True),
        (".conversations/claude/s.jsonl", True),
        ("./.conversations/claude/s.jsonl", True),
        (".hybrid-search/wiki/index.md", False),
        ("src/a.py", False),
    ])
    def test_quoted_paths(self, path, quoted) -> None:
        assert is_quoted_path(path) is quoted


class TestDefectBQuotedHits:
    def test_prefetch_quoted_hit_used_in_answer(self, tmp_path) -> None:
        row = _prefetch_row(
            tmp_path, [_say(f"지난 결정은 {HASH} 커밋에 있습니다.")],
            paths=(".git-history/commits",),
            quoted=[{"rank": 1, "excerpt": f"[fix] 게이트 추가 ({HASH})"}],
        )
        assert row["quoted_served"] == 1
        assert row["quoted_adopted_rank"] == 1
        assert row["verdict"] == "no_followup", "verdict stays the file-lane judgement"

    def test_prefetch_quoted_hit_unused(self, tmp_path) -> None:
        row = _prefetch_row(
            tmp_path, [_say("모르겠습니다.")],
            paths=(".git-history/commits",),
            quoted=[{"rank": 1, "excerpt": f"[fix] 게이트 추가 ({HASH})"}],
        )
        assert row["quoted_served"] == 1
        assert row["quoted_adopted_rank"] is None

    def test_pre_v12_pending_row_has_no_quotes(self, tmp_path) -> None:
        row = _prefetch_row(tmp_path, [_say(HASH)], paths=(".git-history/commits",))
        assert row["quoted_served"] == 0

    def test_tool_lane_reads_quotes_from_result_json(self, tmp_path) -> None:
        results = [
            {"file_path": "src/a.py", "node_type": "function", "content": "def a(): ..."},
            {"file_path": ".git-history/commits", "node_type": "commit",
             "content": f"[fix] 게이트 추가 ({HASH})"},
        ]
        turn = _search("u1", "q", results) + [
            _search("u2", "q2", [{"file_path": "src/b.py"}])[0],
            _tool_result("u2", json.dumps({"results": []})),
            _say(f"{HASH} 에서 게이트가 들어갔습니다."),
        ]
        selfeval.record_turn(tmp_path, turn)
        rows = [json.loads(line) for line in
                (tmp_path / ".hybrid-search/selfeval/events.jsonl").read_text().splitlines()]
        first = rows[0]
        assert first["quoted_served"] == 1
        assert first["quoted_adopted_rank"] == 2, "answer after a later search still counts"

    def test_summary_counts_quoted_separately(self, tmp_path) -> None:
        _prefetch_row(
            tmp_path, [_say(f"{HASH} 입니다")], paths=(".git-history/commits",),
            quoted=[{"rank": 1, "excerpt": f"커밋 {HASH}"}],
        )
        stats = selfeval.summarize(tmp_path)
        assert stats["quoted_served"] == 1 and stats["quoted_adopted"] == 1
        assert stats["lanes"]["prefetch"]["quoted_adopted"] == 1
        assert "quoted 1/1" in selfeval.format_summary_line(tmp_path)


class TestQuotedExcerptsFromPrefetch:
    def test_only_virtual_hits_with_rank(self) -> None:
        results = [
            SimpleNamespace(
                node_type="function", file_path="src/a.py", snippet="def a", content=""
            ),
            SimpleNamespace(node_type="commit", file_path=".git-history/commits",
                            content=f"[fix] 게이트 추가 ({HASH})", snippet="", trust_meta=""),
        ]
        got = hook_runtime.quoted_excerpts(SimpleNamespace(results=results))
        assert [q["rank"] for q in got] == [2]
        assert HASH in got[0]["excerpt"]
