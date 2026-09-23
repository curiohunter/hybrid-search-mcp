"""What a qa record owns (2026-09-23).

A question-only record that quotes an answered one carried the string
``## Answer excerpt`` inside its quotation, so a substring check read it as
answered and ``split()`` returned the quoted list as its answer. Records are
built with the real writer so the fixtures have the shape disk has.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.memory.qa_log import QARecord, _format_record
from hybrid_search.memory.qa_shape import answer_excerpt, owns_answer


def _record(*, answer: str | None, results: list[dict] | None = None,
            trigger: str = "stop_hook") -> str:
    return _format_record(QARecord(
        query="캐시 무효화는 언제 일어나나",
        query_type="TURN",
        effective_bm25_weight=0.0,
        query_time_ms=0.0,
        total_chunks_searched=0,
        results=results or [],
        timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        project_root=Path("/tmp/demo"),
        trigger=trigger,
        answer_chars=len(answer) if answer else None,
        answer_excerpt=answer,
    ))


ANSWERED = _record(answer="쓰기 경로가 끝날 때 키 단위로 무효화한다.\n\n두 번째 문단.")

# A pre-fetch record whose top hit is the answered record above. A search
# snippet of a qa hit is a window of its body — metadata bullets, then the
# answer heading — and the writer flattens it, heading and all, onto one
# quoted line.
QUOTING = _record(
    answer=None,
    trigger="user_prompt_submit",
    results=[{
        "chunk_id": "c1",
        "file_path": ".hybrid-search/qa/2026/09/01-000000-aaaa.md",
        "snippet": "[qa - stop_hook] " + ANSWERED[ANSWERED.index("- **answer_chars**"):],
    }],
)


class TestOwnsAnswer:
    def test_answered_record(self):
        assert owns_answer(ANSWERED)

    def test_question_only_record(self):
        assert not owns_answer(_record(answer=None, trigger="user_prompt_submit"))

    def test_quotation_is_not_ownership(self):
        assert "## Answer excerpt" in QUOTING  # the trap the substring fell in
        assert not owns_answer(QUOTING)

    def test_reflector_note_shape(self):
        note = "---\nquery: q\ntrigger: reflector\n---\n\n## Answer excerpt\n\n종합한 답.\n"
        assert owns_answer(note)

    def test_empty(self):
        assert not owns_answer("")
        assert not owns_answer(None)


class TestAnswerExcerpt:
    def test_stops_at_results(self):
        text = answer_excerpt(ANSWERED)
        assert text.startswith("쓰기 경로가 끝날 때")
        assert "두 번째 문단." in text
        assert "Top results" not in text

    def test_quotation_yields_nothing(self):
        assert answer_excerpt(QUOTING) == ""

    def test_note_without_results_section(self):
        note = "---\nquery: q\n---\n\n## Answer excerpt\n\n종합한 답.\n"
        assert answer_excerpt(note) == "종합한 답."
