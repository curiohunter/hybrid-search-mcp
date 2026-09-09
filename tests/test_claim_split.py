"""Structure-aware claim splitting for the memory lanes (2026-09-08).

The code lane never had the retrieval problem the memory lanes have because
tree-sitter gave it boundaries. This module gives the memory lanes theirs. The
tests fix the two decisions that measurement forced: evidence is stripped
before claims are cut, and a structured memory file contributes only its body.
"""

from __future__ import annotations

from hybrid_search.index.claim_split import (
    CLAIM_MIN_CHARS,
    body_for_claims,
    split_claims,
)


def kinds(text: str, **kw) -> list[tuple[str, str]]:
    return [(c.kind, c.text) for c in split_claims(text, **kw)]


class TestEvidenceIsNotAClaim:
    def test_fenced_code_is_dropped(self):
        text = (
            "**결론은 캐시를 두 층으로 두는 것입니다.**\n"
            "```python\nprint('이것은 주장이 아니라 증거다')\n```\n"
        )
        out = [t for _, t in kinds(text)]
        assert any("캐시를 두 층" in t for t in out)
        assert not any("print" in t for t in out)

    def test_shell_transcript_lines_are_dropped(self):
        text = "$ git status\n앞의 명령은 증거이고 이 문장이 주장입니다.\n"
        out = [t for _, t in kinds(text)]
        assert out == ["앞의 명령은 증거이고 이 문장이 주장입니다."]

    def test_turn_envelope_and_image_boilerplate_go(self):
        # Every screenshot turn shares the same banner; it clustered unrelated
        # turns together in the 2026-09-04 Reflector run.
        text = ("[claude turn] [Image: original 1500x3033, displayed at 700x1400.] "
                "그림을 붙였을 때의 실제 결론은 이것입니다.")
        out = [t for _, t in kinds(text)]
        assert len(out) == 1
        assert "Image:" not in out[0] and "claude turn" not in out[0]


class TestGrammarOfThisCorpus:
    def test_a_bolded_assertion_keeps_its_line(self):
        text = "원인을 보면 **공유 캐시가 문제입니다** 라고 정리됩니다.\n"
        kind, body = kinds(text)[0]
        assert kind == "bold"
        assert "공유 캐시가 문제입니다" in body and "정리됩니다" in body

    def test_list_items_and_table_rows_are_separate_claims(self):
        text = ("1. 벌크 경로는 별도 캐시를 쓴다\n"
                "2. 핫패스는 기존 캐시를 유지한다\n"
                "| 구분 | 벌크는 별도 캐시 | 핫패스는 공용 |\n")
        out = kinds(text)
        assert [k for k, _ in out] == ["list", "list", "table"]

    def test_table_rule_rows_are_not_claims(self):
        assert kinds("| 구분 | 값 |\n|---|---|\n") == []  # both rows are too short

    def test_prose_groups_by_paragraph_not_by_sentence(self):
        # A claim keeps the clause that qualifies it.
        text = ("첫 문장이 있고. 그 조건이 이어집니다.\n\n"
                "다른 문단은 따로 떨어져 나옵니다.\n")
        out = [t for _, t in kinds(text)]
        assert len(out) == 2
        assert "그 조건이" in out[0]

    def test_the_user_request_is_its_own_claim(self):
        out = kinds("답변 본문이 여기에 있습니다.", request="왜 이렇게 만들었는지 알려줘")
        assert out[0][0] == "request"

    def test_duplicates_within_a_chunk_collapse(self):
        line = "같은 문장이 두 번 나오면 한 번만 남습니다\n"
        assert len(kinds(line + line)) == 1

    def test_too_short_is_not_a_claim(self):
        assert kinds("네 알겠습니다\n") == []
        assert len("네 알겠습니다") < CLAIM_MIN_CHARS


class TestBodyForClaims:
    QA = ("---\nquery: \"x\"\ntimestamp: 1\n---\n\n"
          "## Answer excerpt\n\n조회는 캐시를 먼저 보고 없으면 원본을 읽는다.\n\n"
          "## Top results\n\n1. `services/widget-lookup.ts`\n2. `lib/cache.ts`\n")

    def test_qa_log_keeps_only_the_answer(self):
        """The receipt is not the answer.

        Splitting a whole qa log yielded one claim per result path — 26 claims
        a log, `list` dominant — which is how the first P1 measurement got its
        number.
        """
        body = body_for_claims(self.QA, "qa_log")
        assert "캐시를 먼저 보고" in body
        assert "widget-lookup" not in body
        assert "timestamp" not in body

    def test_conversation_turns_are_all_body(self):
        turn = "턴은 절이 없으므로 본문 전체가 대상입니다."
        assert body_for_claims(turn, "conv_turn") == turn

    def test_unknown_node_type_passes_through_minus_frontmatter(self):
        assert body_for_claims(self.QA, "section").lstrip().startswith("## Answer")

    def test_missing_section_falls_back_to_the_whole_text(self):
        plain = "절 제목이 없는 기록입니다."
        assert body_for_claims(plain, "qa_log") == plain
