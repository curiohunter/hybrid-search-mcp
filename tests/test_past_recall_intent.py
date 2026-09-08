"""Korean past-recall endings as a memory-intent signal (2026-09-08).

``_MEMORY_INTENT_KO`` is a list of literal forms, and Korean inflection
defeats a literal list: it carries "했지" but not "했었지", "뭐였지" but not
"뭐였더라", and nothing for "나눴지" or "골라냈지". A topical classification is
not merely a worse ranking — the conversation lane never runs, so the turns
holding the answer are never retrieved.
"""

from __future__ import annotations

import pytest

from hybrid_search.search.orchestrator import (
    _has_memory_intent,
    _has_past_recall_ending,
)


class TestPastRecallEnding:
    @pytest.mark.parametrize("q", [
        "메인에 올리기 전에 뭘 확인하기로 했었지",
        "데스크 단계 구조를 결국 어떻게 확정했었지",
        "같은 폴더를 여럿이 쓸 때 내 산출물만 어떻게 골라냈지",
        "건수가 너무 많아 다 못 볼 때 역할을 어떻게 나눴지",
        "개발 서버를 직접 띄울 때 걸렸던 문제가 뭐였더라",
        "임베딩을 아예 안 읽고 있던 게 어디였더라",
        "청록색 제거에서 예외로 둔 게 있었지",
        "그때 뭐라고 했던가",
    ])
    def test_recall_shaped_questions(self, q):
        assert _has_past_recall_ending(q)
        assert _has_memory_intent(q)

    @pytest.mark.parametrize("q", [
        # Present tense — asking about now, not about what was decided.
        "그 조회용 스킬이 DB를 고치기도 하나",
        "이 함수가 어디에 있지",
        # ㅆ-final but not a past marker.
        "테스트 픽스처가 있지",
        "그런 설정은 없지",
        "이걸 지금 고치겠지",
        # A past clause, not a question ending on one.
        "그건 했지만 이건 아직이다",
        "확인했었지만 다시 본다",
        # No Hangul at all.
        "FusedResult",
        "",
    ])
    def test_not_recall_shaped(self, q):
        assert not _has_past_recall_ending(q)

    @pytest.mark.parametrize("q", [
        "뭐였더라?", "어떻게 했었지?？", "정했었지 ", "확정했었지...",
    ])
    def test_trailing_punctuation_is_stripped(self, q):
        assert _has_past_recall_ending(q)

    def test_na_is_not_a_recall_ending(self):
        """"-나" asks about state, not about a past decision.

        Including it moved a rationale question ("왜 세워졌나") onto the
        memory lanes, which handed six of ten slots to records about other
        subjects and pushed the answering document from rank 1 to 7.
        """
        assert not _has_past_recall_ending("entrance test 관리 플랜은 왜 세워졌나")
        assert not _has_past_recall_ending("그림 후보 자동 선택 결과를 그대로 썼었나")

    def test_literal_list_still_works(self):
        # The new rule is additive — nothing the old list caught may drop.
        assert _has_memory_intent("지난번에 뭐였지")
        assert _has_memory_intent("최근 작업이 뭐야")
        assert _has_memory_intent("why was this built earlier")
