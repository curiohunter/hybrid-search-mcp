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


# ── v1.1: the pre-fetch lane ──────────────────────────────────────────


def _read_events(root) -> list[dict]:
    path = root / ".hybrid-search/selfeval/events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


class TestPrefetchLane:
    """The pre-fetch injects without a tool call, so it joins via a sidecar."""

    def test_records_pending_and_scores_it_on_stop(self, tmp_path) -> None:
        assert selfeval.record_prefetch(
            tmp_path, query="환불 흐름", paths=["a.py", "b.py"],
            qa_record_id="04-101010-abcd1234", session_key="s1",
        )
        turn = [_tool_call("Read", {"file_path": str(tmp_path / "b.py")})]

        written = selfeval.record_turn(
            tmp_path, turn, session_key="s1", prompt="환불 흐름"
        )

        assert written == 1
        row = _read_events(tmp_path)[-1]
        assert row["source"] == "prefetch"
        assert row["verdict"] == "adopted"
        assert row["adopted_rank"] == 2
        assert row["qa_record_id"] == "04-101010-abcd1234"

    def test_whole_turn_is_the_attribution_window(self, tmp_path) -> None:
        """No search call precedes the reads — the tool lane would score none."""
        selfeval.record_prefetch(
            tmp_path, query="q", paths=["hit.py"], session_key="s1"
        )
        turn = [
            _tool_call("Read", {"file_path": "elsewhere.py"}),
            _tool_call("Grep", {"pattern": "def foo"}),
        ]

        selfeval.record_turn(tmp_path, turn, session_key="s1", prompt="q")

        row = _read_events(tmp_path)[-1]
        assert row["verdict"] == "betrayed"
        assert row["greps_after"] == 1
        assert row["outside_reads"] == ["elsewhere.py"]

    def test_non_matching_prompt_claims_nothing(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="A", paths=["a.py"], session_key="s1")

        selfeval.record_turn(tmp_path, [], session_key="s1", prompt="B")

        assert _read_events(tmp_path) == []
        pending = tmp_path / ".hybrid-search/selfeval/pending/s1.jsonl"
        assert len(pending.read_text().splitlines()) == 1, "row must stay queued"

    def test_repeated_question_consumes_fifo(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="같은 질문", paths=["first.py"],
                                 qa_record_id="one", session_key="s1")
        selfeval.record_prefetch(tmp_path, query="같은 질문", paths=["second.py"],
                                 qa_record_id="two", session_key="s1")

        selfeval.record_turn(tmp_path, [], session_key="s1", prompt="같은 질문")
        selfeval.record_turn(tmp_path, [], session_key="s1", prompt="같은 질문")

        ids = [r["qa_record_id"] for r in _read_events(tmp_path)]
        assert ids == ["one", "two"], "oldest match first, in turn order"

    def test_sessions_do_not_consume_each_other(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"],
                                 qa_record_id="mine", session_key="s1")
        selfeval.record_prefetch(tmp_path, query="q", paths=["b.py"],
                                 qa_record_id="theirs", session_key="s2")

        selfeval.record_turn(tmp_path, [], session_key="s1", prompt="q")

        assert [r["qa_record_id"] for r in _read_events(tmp_path)] == ["mine"]
        other = tmp_path / ".hybrid-search/selfeval/pending/s2.jsonl"
        assert len(other.read_text().splitlines()) == 1

    def test_expired_pending_is_dropped_and_logged(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"], session_key="s1")
        pending = tmp_path / ".hybrid-search/selfeval/pending/s1.jsonl"
        row = json.loads(pending.read_text().splitlines()[0])
        row["ts"] = "2000-01-01T00:00:00+00:00"
        pending.write_text(json.dumps(row) + "\n")

        assert selfeval.prune_pending(tmp_path) == 1

        assert pending.read_text().strip() == ""
        misses = (tmp_path / ".hybrid-search/selfeval/misses.jsonl").read_text()
        assert "pending_expired" in misses

    def test_summarize_splits_lanes(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"], session_key="s1")
        selfeval.record_turn(
            tmp_path,
            _search_call("u1", "코드", ["x.py"]) + [_tool_call("Read", {"file_path": "x.py"})],
            session_key="s1", prompt="q",
        )

        stats = selfeval.summarize(tmp_path, days=7)

        assert stats["total"] == 2
        assert stats["lanes"]["tool"]["total"] == 1
        assert stats["lanes"]["prefetch"]["total"] == 1
        assert "prefetch 1" in selfeval.format_summary_line(tmp_path)

    def test_legacy_rows_without_source_count_as_tool(self, tmp_path) -> None:
        events = tmp_path / ".hybrid-search/selfeval/events.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(json.dumps({
            "ts": selfeval.datetime.now(selfeval.timezone.utc).isoformat(),
            "verdict": "adopted",
        }) + "\n")

        stats = selfeval.summarize(tmp_path, days=7)

        assert stats["lanes"]["tool"]["total"] == 1
        assert stats["lanes"]["prefetch"]["total"] == 0


class TestGoldPathFolding:
    """Gold paths must survive the worktree that produced them."""

    def test_folds_path_that_exists_under_project_root(self, tmp_path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs/plan.md").write_text("x")
        worktree = tmp_path.parent / "proj-ws-kcplan/docs/plan.md"

        assert selfeval._fold_path(str(worktree), tmp_path) == "docs/plan.md"

    def test_tags_unresolvable_path_as_external(self, tmp_path) -> None:
        folded = selfeval._fold_path("/Users/x/.claude/projects/p/memory/a.md", tmp_path)

        assert folded.startswith("external:")
        assert not selfeval.is_usable_gold(folded)

    def test_strips_project_prefix(self, tmp_path) -> None:
        assert selfeval._fold_path(str(tmp_path / "src/a.py"), tmp_path) == "src/a.py"

    def test_migration_rewrites_and_counts(self, tmp_path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src/hit.py").write_text("x")
        harvested = tmp_path / ".hybrid-search/selfeval/harvested.jsonl"
        harvested.parent.mkdir(parents=True)
        harvested.write_text(
            json.dumps({"query": "q", "gold_paths": [
                str(tmp_path.parent / "proj-ws-x/src/hit.py"),
                "/Users/x/.claude/projects/p/memory/a.md",
            ]}) + "\n"
        )

        stats = selfeval.migrate_harvested(tmp_path)

        assert stats == {"rows": 1, "rewritten": 1, "usable_rows": 1, "dropped_paths": 1}
        row = json.loads(harvested.read_text().splitlines()[0])
        assert row["gold_paths"][0] == "src/hit.py"
        assert row["gold_paths"][1].startswith("external:")


class TestRetroScan:
    """Pre-v1.1 pre-fetches are recoverable from the qa logs on disk."""

    def _write_qa(self, root, stem: str, query: str, paths: list[str]) -> None:
        qa = root / ".hybrid-search/qa/2026/09"
        qa.mkdir(parents=True, exist_ok=True)
        body = [
            "---", f'query: "{query}"', "trigger: user_prompt_submit", "---",
            f"# Q: {query}", "", "## Top results", "",
        ]
        for i, p in enumerate(paths, start=1):
            body.append(f"### {i}. `{p}` — x")
        (qa / f"{stem}.md").write_text("\n".join(body) + "\n")

    def test_scores_from_qa_log_and_is_idempotent(self, tmp_path, monkeypatch) -> None:
        self._write_qa(tmp_path, "04-101010-aaaa1111", "환불 흐름", ["a.py:1-9", "b.py"])
        monkeypatch.setattr(
            selfeval, "_transcript_turn_index",
            lambda root: {selfeval._match_key("환불 흐름"): {"reads": ["b.py"], "greps": []}},
        )

        first = selfeval.retro_scan(tmp_path)
        second = selfeval.retro_scan(tmp_path)

        assert first["parsed_md"] == 1 and first["scored"] == 1
        assert first["adopted"] == 1
        assert second["scored"] == 0 and second["skipped_existing"] == 1
        row = _read_events(tmp_path)[-1]
        assert row["source"] == "prefetch"
        assert row["top_paths"] == ["a.py", "b.py"], "line suffix stripped"

    def test_unmatched_turn_is_logged_as_a_miss(self, tmp_path, monkeypatch) -> None:
        self._write_qa(tmp_path, "04-101010-bbbb2222", "없는 질문", ["a.py"])
        monkeypatch.setattr(selfeval, "_transcript_turn_index", lambda root: {})

        stats = selfeval.retro_scan(tmp_path)

        assert stats["parsed_md"] == 1 and stats["scored"] == 0
        misses = (tmp_path / ".hybrid-search/selfeval/misses.jsonl").read_text()
        assert "retro_no_transcript_turn" in misses

    def test_ignores_non_prefetch_logs(self, tmp_path) -> None:
        qa = tmp_path / ".hybrid-search/qa/2026/09"
        qa.mkdir(parents=True)
        (qa / "x.md").write_text('---\nquery: "q"\ntrigger: stop_hook\n---\n### 1. `a.py` — x\n')

        assert selfeval.retro_scan(tmp_path)["parsed_md"] == 0


class TestFollowupEvidence:
    """Read is not the only way a turn uses (or abandons) a search result."""

    def test_editing_a_served_file_counts_as_adoption(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="q", paths=["svc.ts"], session_key="s")
        turn = [_tool_call("Edit", {"file_path": "svc.ts", "old_string": "a"})]

        selfeval.record_turn(tmp_path, turn, session_key="s", prompt="q")

        row = _read_events(tmp_path)[-1]
        assert row["verdict"] == "adopted"
        assert row["adopted_rank"] == 1

    def test_shell_search_counts_as_betrayal(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"], session_key="s")
        turn = [_tool_call("Bash", {"command": "rg 'refund' src/"})]

        selfeval.record_turn(tmp_path, turn, session_key="s", prompt="q")

        assert _read_events(tmp_path)[-1]["verdict"] == "betrayed"

    def test_ordinary_bash_is_not_a_search(self, tmp_path) -> None:
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"], session_key="s")
        turn = [_tool_call("Bash", {"command": "npm run build"})]

        selfeval.record_turn(tmp_path, turn, session_key="s", prompt="q")

        assert _read_events(tmp_path)[-1]["verdict"] == "no_followup"

    def test_piping_into_grep_is_not_a_code_search(self, tmp_path) -> None:
        """Filtering a command's output is not looking for the answer."""
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"], session_key="s")
        turn = [_tool_call("Bash", {"command": "git log --oneline | grep fix"})]

        selfeval.record_turn(tmp_path, turn, session_key="s", prompt="q")

        assert _read_events(tmp_path)[-1]["verdict"] == "no_followup"

    def test_tool_lane_sees_the_same_evidence(self, tmp_path) -> None:
        turn = _search_call("u1", "q", ["mod.ts"]) + [
            _tool_call("Write", {"file_path": "mod.ts"}),
        ]

        selfeval.record_turn(tmp_path, turn)

        row = _read_events(tmp_path)[-1]
        assert row["source"] == "tool" and row["verdict"] == "adopted"

    def test_search_after_cd_still_counts(self, tmp_path) -> None:
        """`cd x && rg y` is the same act as `rg y`."""
        selfeval.record_prefetch(tmp_path, query="q", paths=["a.py"], session_key="s")
        turn = [_tool_call("Bash", {"command": "cd src && rg 'refund'"})]

        selfeval.record_turn(tmp_path, turn, session_key="s", prompt="q")

        assert _read_events(tmp_path)[-1]["verdict"] == "betrayed"
