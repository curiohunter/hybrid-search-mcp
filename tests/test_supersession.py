"""Which memory records may supersede which.

Supersession means "this record was answered again later". It is destructive
at full capacity — the splice replaces the stale hit — so getting the
direction wrong deletes an answer rather than updating it.
"""

from __future__ import annotations

from hybrid_search.memory import supersession
from hybrid_search.memory.supersession import compute_supersession
from hybrid_search.search import qa_topics


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

    def _note(
        self,
        ts: str,
        sources_hash: str,
        body: str,
        sources: tuple[str, ...] = (),
    ) -> str:
        listed = "".join(f"  - {src}\n" for src in sources)
        return (
            "---\n"
            f'query: "스키마 마이그레이션 순서가 뭐였지?"\n'
            f"timestamp: {ts}\n"
            "trigger: reflector\n"
            "memory_type: consolidated\n"
            f"sources_hash: {sources_hash}\n"
            + (f"sources:\n{listed}" if sources else "")
            + "---\n\n"
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

    BODY = "푸시 전에 적용하지 않은 마이그레이션이 남았는지 스키마를 본다"

    # The fixture bodies carry TWO distinctive content words on purpose
    # (마이그레이션, 스키마). `_MIN_DISTINCTIVE_SHARED` is 2, and under the
    # morphological backend a sentence of process vocabulary alone —
    # 푸시·파일·확인 are all in the low-information tier — has only one, so
    # a thinner body would test the tokenizer's floor instead of the
    # supersession contract these cases are about.
    def test_a_note_is_not_superseded_by_another_note(self):
        entries = [
            ("older", self._note("2026-09-04T10:00:00+00:00", "aaaa", self.BODY)),
            ("newer", self._note("2026-09-04T16:00:00+00:00", "bbbb", self.BODY)),
        ]
        assert compute_supersession(entries) == {}

    def test_a_note_still_supersedes_the_turn_logs_it_was_built_from(self):
        entries = [
            ("log", self._turn_log("2026-09-01T10:00:00+00:00", self.BODY)),
            ("note", self._note(
                "2026-09-04T16:00:00+00:00", "bbbb", self.BODY,
                sources=("2026/09/01-100000-aaaaaaaa.md",),
            )),
        ]
        assert compute_supersession(entries) == {"log": "note"}

    def test_both_source_forms_resolve_to_a_record_key(self):
        """A note lists turn logs by date-time and notes by cluster id.

        Both are resolvable from CONTENT alone, which is all this module
        gets: a turn log repeats its filename's instant in `timestamp:`
        (237/237 on the dogfood corpus) and a note carries its cluster id
        in `sources_hash:`.
        """
        note = self._note(
            "2026-09-04T16:00:00+00:00", "bbbb", self.BODY,
            sources=(
                "2026/09/01-100000-aaaaaaaa.md",
                "consolidated/2026-09-01-c9fa7450.md",
            ),
        )
        assert supersession._consolidation_sources(note) == frozenset(
            {"2026/09/01-100000", "consolidated/c9fa7450"}
        )
        log = self._turn_log("2026-09-01T10:00:00+00:00", self.BODY)
        assert supersession._record_key(log) == "2026/09/01-100000"
        inner = self._note("2026-09-01T10:00:00+00:00", "c9fa7450", self.BODY)
        assert supersession._record_key(inner) == "consolidated/c9fa7450"

    def test_a_sources_list_does_not_swallow_later_frontmatter_keys(self):
        """The list ends where the next key begins."""
        note = (
            "---\n"
            'query: "스키마 마이그레이션 순서가 뭐였지?"\n'
            "sources:\n"
            "  - 2026/09/01-100000-aaaaaaaa.md\n"
            "memory_type: consolidated\n"
            "other:\n"
            "  - 2026/07/07-070000-dddddddd.md\n"
            "---\n\n## Answer excerpt\n\n본문\n"
        )
        assert supersession._consolidation_sources(note) == frozenset(
            {"2026/09/01-100000"}
        )

    def test_turn_logs_still_supersede_each_other(self):
        entries = [
            ("old", self._turn_log("2026-09-01T10:00:00+00:00", self.BODY)),
            ("new", self._turn_log("2026-09-05T10:00:00+00:00", self.BODY)),
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

class TestProjectNameIsNotEnough:
    """The corpus's own name must not group two records on its own.

    2026-09-09: a gold question's answer was mapped as superseded by an
    unrelated shell paste, on nothing but the project name — which sits in
    every shell prompt and every worktree path in its own corpus, and
    which `_is_identifier` weights 3x for being snake_case. The answer
    then left the results, because the splice REPLACES a stale hit at full
    capacity.

    The fixture keeps the real shape: one side has no ``## Answer
    excerpt``, so the pair is judged on question text alone.
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

    def _mapping(self, project_name: str | None) -> dict[str, str]:
        entries = [
            ("bare", self._entry("2026-09-01T10:00:00+00:00",
                                 "acme_webapp 확인", None)),
            ("other", self._entry("2026-09-05T10:00:00+00:00",
                                  "acme_webapp 재시작",
                                  "개발 서버를 3001 포트로 다시 띄웠습니다")),
        ]
        return compute_supersession(entries, project_name=project_name)

    def test_the_fixture_reproduces_the_bug_without_the_name(self) -> None:
        # Guards the guard: if this stops grouping for some other reason,
        # the assertion below would prove nothing.
        assert self._mapping(None) == {"bare": "other"}

    def test_the_project_name_alone_does_not_supersede(self) -> None:
        assert self._mapping("acme_webapp") == {}

class TestARatioNeedsSomethingToBeARatioOf:
    """A two-token question clears any overlap bar trivially.

    With no answer to corroborate, the whole decision rests on the
    question text — and sharing 2 tokens out of 2 is "100% overlap"
    while being no evidence at all. Measured on the dogfood corpus
    (2026-09-10): of the 112 mappings the answer-less path produced,
    question mass had a median of 7.0 but a 10th percentile of 2.0, and
    a quarter sat under 4.0. One paired a generic question with another
    generic question at ratio 1.00 on ['가장', '좋겠'].

    Below the bar the pair is not judged at all — the same refusal the
    undated branch makes: when the evidence cannot support a decision,
    decline rather than decide badly.
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

    ANSWER = "정산 배치를 새벽 4시로 옮겼습니다"

    def test_a_two_word_question_is_not_evidence(self) -> None:
        entries = [
            ("bare", self._entry("2026-09-01T10:00:00+00:00", "가장 좋겠니", None)),
            ("answered", self._entry("2026-09-05T10:00:00+00:00", "가장 좋겠니",
                                     self.ANSWER)),
        ]
        assert compute_supersession(entries) == {}

    def test_a_substantial_question_still_maps(self) -> None:
        # The bar is on mass, not on being answer-less: the same shape
        # with a question carrying real content is still mapped.
        question = "수강료 정산 배치 스케줄러 설정을 어디서 바꾸는지 알려줘"
        entries = [
            ("bare", self._entry("2026-09-01T10:00:00+00:00", question, None)),
            ("answered", self._entry("2026-09-05T10:00:00+00:00", question,
                                     self.ANSWER)),
        ]
        assert compute_supersession(entries) == {"bare": "answered"}


class TestTheMapIsMonotonicInThePredicate:
    """A stricter predicate must never CREATE a mapping.

    It used to. Successors were read off greedy complete-link groups, so
    removing one edge split a group and a record that had been its
    group's winner could land in another group and acquire a successor it
    never pairwise matched. Measured 2026-09-10 on the dogfood corpus:
    tightening the predicate produced 35 brand-new mappings and
    retargeted 33 — and one of the new ones deleted a gold question's
    answer. That is why every threshold move in this line kept costing
    the same question whichever way it went.

    Successors are now decided pairwise, which makes the map monotonic:
    tightening can only take a successor away.
    """

    def _entry(self, cid: str, ts: str, query: str, answer: str) -> tuple[str, str]:
        return (cid, (
            "---\n"
            f'query: "{query}"\n'
            f"timestamp: {ts}\n"
            "trigger: stop_hook\n"
            "---\n"
            f"\n## Answer excerpt\n\n{answer}\n"
        ))

    def test_tightening_only_removes(self) -> None:
        entries = [
            self._entry("a", "2026-09-01T10:00:00+00:00",
                        "수강료 정산 배치 스케줄러 설정 위치",
                        "스케줄러 설정에서 cron 을 바꿉니다"),
            self._entry("b", "2026-09-03T10:00:00+00:00",
                        "수강료 정산 배치 스케줄러 설정 어디",
                        "스케줄러 설정에서 cron 을 바꿉니다"),
            self._entry("c", "2026-09-05T10:00:00+00:00",
                        "정산 배치 스케줄러 cron 표현식",
                        "cron 표현식은 0 4 * * * 입니다"),
        ]
        loose = compute_supersession(entries)
        saved = supersession._MIN_SYMMETRIC_QUESTION_OVERLAP
        supersession._MIN_SYMMETRIC_QUESTION_OVERLAP = 0.99
        try:
            strict = compute_supersession(entries)
        finally:
            supersession._MIN_SYMMETRIC_QUESTION_OVERLAP = saved
        # The property that matters: tightening never ADDS a key. A
        # record that had no successor cannot acquire one, and a record
        # that had one can only lose it or fall back to an older match it
        # already paired with — never to something it never matched.
        # (Falling back is expected and fine; measured at 80 of 253 on
        # the dogfood corpus. Acquiring one was the bug: 35 of 203.)
        for old in strict:
            assert old in loose, f"tightening invented a mapping for {old}"


class TestSameWordsDifferentWork:
    """Two records that name no artifact in common did different work.

    Decided by the tool's owner on 2026-09-12. "메인에 머지하고 푸시" recurs
    for months in this corpus and merges a different branch each time;
    reading the later one as an update of the earlier deletes the earlier,
    and words alone cannot tell the two readings apart. With the rule in
    the criterion, hand labels and an independent judge went from Cohen's
    κ 0.50 to 0.80 over the same 31 cases.
    """

    BODY = "메인에 머지하고 푸시했다. 빌드 확인 후 워크트리를 정리했다"

    def _turn(self, ts: str, extra: str = "", body: str | None = None) -> str:
        return (
            "---\n"
            'query: "메인에 머지하고 푸시"\n'
            f"timestamp: {ts}\n"
            "trigger: stop_hook\n"
            f"{extra}"
            "---\n\n"
            f"## Answer excerpt\n\n{body or self.BODY}\n"
        )

    def test_disjoint_branches_are_not_the_same_topic(self):
        entries = [
            ("old", self._turn("2026-08-26T10:00:00+00:00",
                               'branch: "feat/alpha"\nhead: "911a88d"\n')),
            ("new", self._turn("2026-09-04T10:00:00+00:00",
                               'branch: "feat/beta"\nhead: "fd37238"\n')),
        ]
        assert compute_supersession(entries) == {}

    def test_the_same_branch_still_supersedes(self):
        entries = [
            ("old", self._turn("2026-08-26T10:00:00+00:00", 'branch: "feat/alpha"\n')),
            ("new", self._turn("2026-09-04T10:00:00+00:00", 'branch: "feat/alpha"\n')),
        ]
        assert compute_supersession(entries) == {"old": "new"}

    def test_silence_on_either_side_leaves_the_pair_alone(self):
        """Absence means unknown, never different."""
        entries = [
            ("old", self._turn("2026-08-26T10:00:00+00:00")),
            ("new", self._turn("2026-09-04T10:00:00+00:00", 'branch: "feat/beta"\n')),
        ]
        assert compute_supersession(entries) == {"old": "new"}

    def test_the_default_branch_is_where_not_what(self):
        """Both turns ended in the main checkout, at different commits.

        The shape of all six damage cases on 2026-09-23: recorded identity
        `{main, <head>}` on each side, so the intersection was `main` alone
        and the rule never fired. A default branch says where a turn
        finished, not what it worked on.
        """
        entries = [
            ("old", self._turn("2026-09-16T10:00:00+00:00",
                               'branch: "main"\nhead: "da5a1eb"\n')),
            ("new", self._turn("2026-09-18T10:00:00+00:00",
                               'branch: "main"\nhead: "5f33be2"\n')),
        ]
        assert compute_supersession(entries) == {}

    def test_the_same_commit_on_main_still_supersedes(self):
        entries = [
            ("old", self._turn("2026-09-16T10:00:00+00:00",
                               'branch: "master"\nhead: "da5a1eb"\n')),
            ("new", self._turn("2026-09-18T10:00:00+00:00",
                               'branch: "master"\nhead: "da5a1eb"\n')),
        ]
        assert compute_supersession(entries) == {"old": "new"}

    def test_recorded_identity_beats_prose(self):
        """`touched:` is exact; scanning the body is only the fallback."""
        rec = self._turn(
            "2026-09-04T10:00:00+00:00",
            'branch: "feat/beta"\ntouched: ["src/a.py"]\n',
            body="다른 파일 src/zzz.py 를 언급만 한다",
        )
        assert supersession._artifacts(rec) == frozenset({"feat/beta", "src/a.py"})

    def test_the_sources_list_is_provenance_not_work(self):
        """A note lists the qa files it was built from — it did not edit them.

        Reading that list as work made every note disjoint from every turn
        and cost Set A a question before it was caught (2026-09-12).
        """
        note = (
            "---\n"
            'query: "메인에 머지하고 푸시"\n'
            "timestamp: 2026-09-04T10:00:00+00:00\n"
            "memory_type: consolidated\n"
            "sources_hash: c9fa7450\n"
            "sources:\n"
            "  - 2026/08/24-051500-fcfdb050.md\n"
            "---\n\n## Answer excerpt\n\n푸시 절차를 정리한다\n"
        )
        assert supersession._artifacts(note) == frozenset()
