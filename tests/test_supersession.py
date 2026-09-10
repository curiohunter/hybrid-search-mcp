"""Which memory records may supersede which.

Supersession means "this record was answered again later". It is destructive
at full capacity — the splice replaces the stale hit — so getting the
direction wrong deletes an answer rather than updating it.
"""

from __future__ import annotations

import pytest

from hybrid_search.search import qa_topics
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
            f'query: "스키마 마이그레이션 순서가 뭐였지?"\n'
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
            f'query: "스키마 마이그레이션 순서가 뭐였지?"\n'
            f"timestamp: {ts}\n"
            "trigger: stop_hook\n"
            "---\n\n"
            f"## Answer excerpt\n\n{body}\n"
        )

    # The fixture bodies carry TWO distinctive content words on purpose
    # (마이그레이션, 스키마). `_MIN_DISTINCTIVE_SHARED` is 2, and under the
    # morphological backend a sentence of process vocabulary alone —
    # 푸시·파일·확인 are all in the low-information tier — has only one, so
    # a thinner body would test the tokenizer's floor instead of the
    # supersession contract these cases are about.
    def test_a_note_is_not_superseded_by_another_note(self):
        entries = [
            ("older", self._note("2026-09-04T10:00:00+00:00", "aaaa",
                                 "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다")),
            ("newer", self._note("2026-09-04T16:00:00+00:00", "bbbb",
                                 "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다")),
        ]
        assert compute_supersession(entries) == {}

    def test_a_note_still_supersedes_the_turn_logs_it_was_built_from(self):
        entries = [
            ("log", self._turn_log("2026-09-01T10:00:00+00:00",
                                   "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다")),
            ("note", self._note("2026-09-04T16:00:00+00:00", "bbbb",
                                "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다")),
        ]
        assert compute_supersession(entries) == {"log": "note"}

    def test_turn_logs_still_supersede_each_other(self):
        entries = [
            ("old", self._turn_log("2026-09-01T10:00:00+00:00",
                                   "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다")),
            ("new", self._turn_log("2026-09-05T10:00:00+00:00",
                                   "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다")),
        ]
        assert compute_supersession(entries) == {"old": "new"}


class TestTopicBackendStamp:
    """The stored map is only data for the tokenization that built it.

    Installing or removing the optional [korean] extra changes how qa
    text is tokenized without touching a single file, so no mtime and no
    content hash notices. A map read across that boundary would splice
    corrections computed from tokens the running matcher never produces.
    """

    def _db(self, tmp_path):
        from hybrid_search.storage.db import StoreDB

        return StoreDB(tmp_path / "store.db")

    def test_map_written_by_the_running_backend_is_current(self, tmp_path):
        db = self._db(tmp_path)
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", {"old": "new"})
            assert db.qa_supersession_is_current()
            assert db.get_qa_superseding(["old"]) == {"old": "new"}
        finally:
            db.close()

    def test_map_from_the_other_backend_is_not_current(self, tmp_path):
        db = self._db(tmp_path)
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", {"old": "new"})
            db.set_meta(db.QA_TOPIC_BACKEND_KEY, "ko-somethingelse-9")
            assert not db.qa_supersession_is_current()
        finally:
            db.close()

    def test_an_unstamped_map_is_treated_as_the_prefix_backend(self, tmp_path):
        # Indexes written before 2026-09-09 carry no stamp; they were
        # necessarily built by the prefix path, which is still what an
        # install without the extra runs.
        db = self._db(tmp_path)
        try:
            with db.transaction() as conn:
                conn.execute(
                    "DELETE FROM index_meta WHERE key = ?", (db.QA_TOPIC_BACKEND_KEY,)
                )
            expected = qa_topics.topic_backend() == "ko-prefix-1"
            assert db.qa_supersession_is_current() is expected
        finally:
            db.close()

class TestAnswerlessRecords:
    """How records with no ``## Answer excerpt`` take part.

    `same_topic` falls back to question overlap ALONE for them
    (_QUERY_ONLY_OVERLAP = 0.6), which corpus-wide is far too little: two
    turns whose questions share only the project's own name clear that
    easily, since the name sits in every shell prompt and worktree path in
    its own corpus. Measured on the dogfood corpus (2026-09-09), 15% of qa
    records carry no excerpt and they were involved in 45% of all
    mappings; one evicted the answer to a gold question outright, because
    the splice REPLACES a stale hit at full capacity.

    Banning them outright was tried and was wrong: a BARE turn log
    superseded by the consolidated note built from it is the design — that
    mapping is how the note reaches someone who hit the raw turn, and
    removing it cost a different gold question. So the rules are narrower:
    the winner must carry an answer, and an answer-less pair must agree on
    nearly the same question TEXT rather than merely the same project.
    """

    def _entry(self, ts: str, query: str, answer: str | None) -> str:
        body = f"\n## Answer excerpt\n\n{answer}\n" if answer else "\n"
        return (
            "---\n"
            f'query: "{query}"\n'
            f"timestamp: {ts}\n"
            "trigger: stop_hook\n"
            "---\n"
            f"{body}"
        )

    QUESTION = "스키마 마이그레이션 순서가 뭐였지?"
    ANSWER = "스키마를 먼저 올리고 마이그레이션을 적용한다"

    def test_two_answered_records_still_supersede(self) -> None:
        entries = [
            ("old", self._entry("2026-09-01T10:00:00+00:00", self.QUESTION, self.ANSWER)),
            ("new", self._entry("2026-09-05T10:00:00+00:00", self.QUESTION, self.ANSWER)),
        ]
        assert compute_supersession(entries) == {"old": "new"}

    def test_a_bare_turn_is_superseded_by_the_answer_on_its_question(self) -> None:
        # The design path: hitting the raw turn should surface the record
        # that actually answers it.
        entries = [
            ("bare", self._entry("2026-09-01T10:00:00+00:00", self.QUESTION, None)),
            ("answered", self._entry("2026-09-05T10:00:00+00:00", self.QUESTION, self.ANSWER)),
        ]
        assert compute_supersession(entries) == {"bare": "answered"}

    def test_an_answerless_record_never_wins(self) -> None:
        # Newer, but empty: replacing a real answer with it would delete
        # the answer from the results.
        entries = [
            ("answered", self._entry("2026-09-01T10:00:00+00:00", self.QUESTION, self.ANSWER)),
            ("bare", self._entry("2026-09-05T10:00:00+00:00", self.QUESTION, None)),
        ]
        assert compute_supersession(entries) == {"bare": "answered"}

    def test_a_shared_project_name_is_not_enough_for_a_bare_turn(self) -> None:
        # The 2026-09-09 case, in miniature: a bare turn and an answered
        # one whose questions have nothing in common but the project.
        # Question overlap here is well under the answer-less bar.
        entries = [
            ("bare", self._entry("2026-09-01T10:00:00+00:00",
                                 "적재 쪽 얘기인데 acme-webapp-80", None)),
            ("other", self._entry("2026-09-05T10:00:00+00:00",
                                  "acme-webapp 워크트리에서 개발 서버 띄우는 명령어",
                                  "포트 3001로 개발 서버를 띄운다")),
        ]
        assert compute_supersession(entries) == {}
