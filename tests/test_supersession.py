"""Which memory records may supersede which.

Supersession means "this record was answered again later". It is destructive
at full capacity — the splice replaces the stale hit — so getting the
direction wrong deletes an answer rather than updating it.
"""

from __future__ import annotations

from hybrid_search.memory.supersession import compute_supersession

class TestConsolidationIsNeverStale:
    """A Reflector note is not an older version of another Reflector note.

    Notes synthesise their own cluster of records, so two notes on adjacent
    topics are two answers, not an answer and its update — and the topic
    matcher pairs them readily, because one Reflector run writes many notes
    on one day about one area. Acting on such a pair is destructive: at full
    capacity the splice REPLACES the stale hit, so on 2026-09-09 the
    top-ranked note of a gold question left the results entirely and the
    question went unanswered.
    """

    def _note(self, ts: str, sources_hash: str, body: str) -> str:
        return (
            "---\n"
            f'query: "메인에 푸시했어?"\n'
            f"timestamp: {ts}\n"
            "trigger: reflector\n"
            "memory_type: consolidated\n"
            f"sources_hash: {sources_hash}\n"
            "---\n\n"
            f"## Answer excerpt\n\n{body}\n"
        )

    def _turn_log(self, ts: str, body: str) -> str:
        return (
            "---\n"
            f'query: "메인에 푸시했어?"\n'
            f"timestamp: {ts}\n"
            "trigger: stop_hook\n"
            "---\n\n"
            f"## Answer excerpt\n\n{body}\n"
        )

    def test_a_note_is_not_superseded_by_another_note(self):
        entries = [
            ("older", self._note("2026-09-04T10:00:00+00:00", "aaaa",
                                 "푸시 전에 겹치는 파일을 먼저 확인한다")),
            ("newer", self._note("2026-09-04T16:00:00+00:00", "bbbb",
                                 "푸시 전에 겹치는 파일을 먼저 확인한다")),
        ]
        assert compute_supersession(entries) == {}

    def test_a_note_still_supersedes_the_turn_logs_it_was_built_from(self):
        entries = [
            ("log", self._turn_log("2026-09-01T10:00:00+00:00",
                                   "푸시 전에 겹치는 파일을 먼저 확인한다")),
            ("note", self._note("2026-09-04T16:00:00+00:00", "bbbb",
                                "푸시 전에 겹치는 파일을 먼저 확인한다")),
        ]
        assert compute_supersession(entries) == {"log": "note"}

    def test_turn_logs_still_supersede_each_other(self):
        entries = [
            ("old", self._turn_log("2026-09-01T10:00:00+00:00",
                                   "푸시 전에 겹치는 파일을 먼저 확인한다")),
            ("new", self._turn_log("2026-09-05T10:00:00+00:00",
                                   "푸시 전에 겹치는 파일을 먼저 확인한다")),
        ]
        assert compute_supersession(entries) == {"old": "new"}
