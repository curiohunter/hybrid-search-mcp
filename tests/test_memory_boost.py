"""Memory Layer ranking tests — time-decay + intent boost on qa_log chunks.

These test the two orchestrator helpers that turn the passive qa_log
corpus into the active "learn from usage" loop:

- ``_has_memory_intent`` — detects the explicit-recall queries that
  should get the strong boost ("지난번에", "previously", ...).
- ``_apply_memory_boost`` — re-ranks the result list so that fresh
  qa_log chunks float above same-score code chunks, and stale qa_log
  chunks fade toward their pre-boost score.

Non-qa_log chunks must pass through untouched; the boost is a
qa_log-specific nudge, not a global re-ranker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hybrid_search.search.orchestrator import (
    HybridResult,
    _apply_memory_boost,
    _has_memory_intent,
    _merge_memory_results,
    _order_qa_by_recency,
    _unanchored_terms,
    _parse_mtime_days_ago,
)


# --- _has_memory_intent ----------------------------------------------------

class TestMemoryIntent:
    @pytest.mark.parametrize("query", [
        "지난번에 portal-v3 관련해서 뭐라고 했지",
        "아까 물어봤던 tuition 관련 답이 뭐였나",
        "방금 전에 본 admissions SQL",
        "전에 봤던 remote-room 구조",
        "이전에 했던 질문 중에 entrance test",
        "저번에 알려준 consultations 컴포넌트",
        "그때 보여준 homework analysis 경로",
        "Codex hook 저장 구조 어떻게 결정했지?",
        "Claude와 Codex memory hook 차이가 뭐였지?",
        "메모리 레이어에서 qa_log와 memory card는 어떻게 달라?",
        # History-shaped feature-genesis questions (2026-07-10 gap A)
        "in-flight overlay 기능은 어떻게 만들었어",
        "confidence weak 판정 로직은 어떻게 바뀌었어",
        "이 기능 왜 만들게 됐어",
        "레인 분리 경위가 궁금해",
        # Superlative-recency phrasings (2026-07-15 Codex field check —
        # the cross-agent handoff loop's most common question shape)
        "클로드 코드와 내가 가장 최근에 한 일이 뭐지",
        "최근 작업 내용 알려줘",
        "최신 진행 상황이 어떻게 되지",
    ])
    def test_korean_recall_phrases_trigger(self, query: str) -> None:
        assert _has_memory_intent(query) is True

    @pytest.mark.parametrize("query", [
        "previously discussed authentication flow",
        "what did I ask about the portal earlier",
        "what is the most recent thing we worked on",
        "show me the latest progress",
        "what did we do recently",
        "the other day we looked at tuition",
        "before, I searched for admission_results",
        "last time you mentioned consultations",
        "WHAT DID WE ASK ABOUT portal",  # case-insensitive
        "how was the overlay feature built",
        "why did the confidence logic change",
        "history of the routing hook",
    ])
    def test_english_recall_phrases_trigger(self, query: str) -> None:
        assert _has_memory_intent(query) is True

    @pytest.mark.parametrize("query", [
        "portal-v3가 뭐야",
        "학부모 학생 포털",
        "how does authentication work",
        "",
        "TuitionChargeSection 컴포넌트",
    ])
    def test_non_recall_queries_do_not_trigger(self, query: str) -> None:
        assert _has_memory_intent(query) is False


# --- _parse_mtime_days_ago -------------------------------------------------

class TestParseMtimeDaysAgo:
    def test_parses_iso_string(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        age = _parse_mtime_days_ago("2026-04-20T00:00:00+00:00", now=now)
        assert age is not None and abs(age - 2.0) < 1e-6

    def test_naive_mtime_treated_as_utc(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        age = _parse_mtime_days_ago("2026-04-22T00:00:00", now=now)
        assert age is not None and age == pytest.approx(0.0, abs=1e-6)

    def test_future_mtime_clamped_to_zero(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        age = _parse_mtime_days_ago("2026-05-01T00:00:00+00:00", now=now)
        assert age == 0.0

    def test_invalid_returns_none(self) -> None:
        assert _parse_mtime_days_ago("not-a-date") is None
        assert _parse_mtime_days_ago("") is None
        assert _parse_mtime_days_ago(None) is None


# --- _apply_memory_boost ---------------------------------------------------

def _mk(
    chunk_id: str, node_type: str, rrf: float, mtime: str | None = None, content: str | None = None,
) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id,
        rrf_score=rrf,
        bm25_rank=1,
        vector_rank=1,
        file_path=f"{chunk_id}.md",
        project="p",
        name=chunk_id,
        qualified_name=chunk_id,
        node_type=node_type,
        start_line=1,
        end_line=2,
        content=content,
        snippet="s",
        file_mtime=mtime,
    )


class TestApplyMemoryBoost:
    def test_no_qa_logs_passes_through(self) -> None:
        results = [_mk("c1", "function", 1.0), _mk("c2", "section", 0.9)]
        out = _apply_memory_boost(results, memory_intent=False)
        assert out is results  # short-circuit: same list reference

    def test_empty_input(self) -> None:
        assert _apply_memory_boost([], memory_intent=False) == []
        assert _apply_memory_boost([], memory_intent=True) == []

    def test_fresh_qa_gets_ambient_boost(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        qa = _mk("q", "qa_log", rrf=1.0, mtime="2026-04-22T00:00:00+00:00")
        out = _apply_memory_boost([qa], memory_intent=False, now=now)
        # fresh (age=0) → decay=1.0 → factor = 1 + 0.20 = 1.20
        assert out[0].rrf_score == pytest.approx(1.20, abs=1e-6)

    def test_stale_qa_gets_much_weaker_boost(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        # 30 days old → half-life kicks in → decay=0.5
        qa = _mk("q", "qa_log", rrf=1.0, mtime="2026-03-23T00:00:00+00:00")
        out = _apply_memory_boost([qa], memory_intent=False, now=now)
        # factor = 1 + 0.20 * 0.5 = 1.10
        assert out[0].rrf_score == pytest.approx(1.10, abs=1e-6)

    def test_intent_boost_much_stronger(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        qa = _mk("q", "qa_log", rrf=1.0, mtime="2026-04-22T00:00:00+00:00")
        out = _apply_memory_boost([qa], memory_intent=True, now=now)
        # fresh + intent → factor = 1 + 1.00 = 2.00
        assert out[0].rrf_score == pytest.approx(2.00, abs=1e-6)

    def test_intent_boost_still_decays_with_age(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        # 30 days old
        qa = _mk("q", "qa_log", rrf=1.0, mtime="2026-03-23T00:00:00+00:00")
        out = _apply_memory_boost([qa], memory_intent=True, now=now)
        # factor = 1 + 1.00 * 0.5 = 1.50
        assert out[0].rrf_score == pytest.approx(1.50, abs=1e-6)

    def test_missing_mtime_treated_as_fresh(self) -> None:
        # Newly-written qa_log whose mtime hasn't reached the index yet
        # should still get the boost — no penalty for missing data.
        qa = _mk("q", "qa_log", rrf=1.0, mtime=None)
        out = _apply_memory_boost([qa], memory_intent=False)
        assert out[0].rrf_score == pytest.approx(1.20, abs=1e-6)

    def test_non_qa_chunks_pass_through_untouched(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        qa = _mk("q", "qa_log", rrf=1.0, mtime="2026-04-22T00:00:00+00:00")
        code = _mk("c", "function", rrf=1.0)
        doc = _mk("d", "section", rrf=1.0)
        out = _apply_memory_boost([qa, code, doc], memory_intent=True, now=now)
        scored = {r.chunk_id: r.rrf_score for r in out}
        assert scored["q"] == pytest.approx(2.0, abs=1e-6)
        assert scored["c"] == pytest.approx(1.0, abs=1e-6)
        assert scored["d"] == pytest.approx(1.0, abs=1e-6)

    def test_fresh_qa_outranks_same_score_code_under_intent(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        qa = _mk("q", "qa_log", rrf=0.5, mtime="2026-04-22T00:00:00+00:00")
        code = _mk("c", "function", rrf=0.9)
        out = _apply_memory_boost([code, qa], memory_intent=True, now=now)
        # qa boosted to 1.0, code stays 0.9 — qa should lead.
        assert out[0].chunk_id == "q"
        assert out[1].chunk_id == "c"

    def test_ambient_boost_does_not_flip_large_code_lead(self) -> None:
        # Without explicit recall intent, a qa_log with much lower
        # base score must not jump ahead of a strong code chunk.
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        qa = _mk("q", "qa_log", rrf=0.5, mtime="2026-04-22T00:00:00+00:00")
        code = _mk("c", "function", rrf=1.0)
        out = _apply_memory_boost([code, qa], memory_intent=False, now=now)
        # qa → 0.6, code → 1.0. Code stays on top.
        assert out[0].chunk_id == "c"

    def test_memory_card_boosts_above_raw_qa_on_memory_intent(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        card = _mk("card", "memory_card", rrf=0.5, mtime="2026-04-22T00:00:00+00:00")
        qa = _mk("qa", "qa_log", rrf=0.5, mtime="2026-04-22T00:00:00+00:00")
        out = _apply_memory_boost([qa, card], memory_intent=True, now=now)
        assert out[0].chunk_id == "card"
        assert out[0].rrf_score > out[1].rrf_score

    def test_domain_term_boosts_above_generic_memory_card(self) -> None:
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        term = _mk("term", "domain_term", rrf=0.5, mtime="2026-04-22T00:00:00+00:00")
        card = _mk("card", "memory_card", rrf=0.5, mtime="2026-04-22T00:00:00+00:00")
        out = _apply_memory_boost([card, term], memory_intent=True, now=now)
        assert out[0].chunk_id == "term"

    def test_superseded_memory_is_downweighted(self) -> None:
        active = _mk("active", "memory_card", rrf=0.5)
        stale = _mk(
            "stale",
            "memory_card",
            rrf=1.0,
            content="---\ntype: memory_card\nstatus: superseded\n---\n\n## Summary\n\nOld.",
        )

        out = _apply_memory_boost([stale, active], memory_intent=True)

        assert out[0].chunk_id == "active"


class TestMergeMemoryResults:
    def test_memory_lane_promotes_cards_before_regular_chunks(self) -> None:
        code = _mk("code", "function", rrf=1.0)
        doc = _mk("doc", "section", rrf=0.9)
        card = _mk("card", "memory_card", rrf=0.8)
        qa = _mk("qa", "qa_log", rrf=1.2)

        out = _merge_memory_results([code, doc], [card, qa], limit=10)

        assert [r.chunk_id for r in out[:4]] == ["card", "qa", "code", "doc"]

    def test_memory_lane_promotes_domain_terms_first(self) -> None:
        code = _mk("code", "function", rrf=1.0)
        card = _mk("card", "memory_card", rrf=0.9)
        term = _mk("term", "domain_term", rrf=0.8)

        out = _merge_memory_results([code], [card, term], limit=10)

        assert [r.chunk_id for r in out[:3]] == ["term", "card", "code"]

    def test_memory_lane_deduplicates_existing_chunks(self) -> None:
        code = _mk("code", "function", rrf=1.0)
        card = _mk("card", "memory_card", rrf=0.8)

        out = _merge_memory_results([card, code], [card], limit=10)

        assert [r.chunk_id for r in out] == ["card", "code"]


def _qa(chunk_id: str, query: str, answer: str, mtime: str | None) -> HybridResult:
    content = (
        f'---\nquery: "{query}"\ntimestamp: {mtime or ""}\n---\n\n'
        f"# Q: {query}\n\n## Answer excerpt\n\n{answer}\n\n## Top results\n"
    )
    r = _mk(chunk_id, "qa_log", rrf=1.0, mtime=mtime, content=content)
    return r.__class__(**{**r.__dict__, "name": query})


class TestOrderQaByRecency:
    def test_same_topic_stale_qa_yields_slot_to_newer(self) -> None:
        # Same topic, conflicting facts (bench v2 U6 shape): the older qa
        # out-lexicals the newer one, but within a topic group the newer
        # fact must take the earlier slot.
        code = _mk("code", "function", 1.0)
        old_qa = _qa(
            "old", "학생 숙제 제출 파일은 어디 저장되나",
            "숙제 제출 파일은 Supabase Storage homework 버킷, 최대 파일 크기 10MB입니다.",
            "2026-04-01T00:00:00+00:00",
        )
        new_qa = _qa(
            "new", "숙제 파일 크기 제한 상향",
            "숙제 파일 최대 크기를 10MB에서 100MB로 올렸습니다. 대용량은 resumable upload.",
            "2026-07-09T00:00:00+00:00",
        )
        out = _order_qa_by_recency([old_qa, code, new_qa])
        assert [r.chunk_id for r in out] == ["new", "code", "old"]

    def test_adversarial_fresh_adjacent_topic_does_not_displace_exact(self) -> None:
        # The failure topic-awareness exists to prevent: an OLD qa exactly
        # matching the probe topic vs a FRESH qa on an adjacent topic that
        # shares generic nouns. Relevance order must survive.
        exact_old = _qa(
            "exact", "학생 숙제 제출 파일은 어디 저장되나",
            "숙제 제출 파일은 Supabase Storage homework 버킷에 저장됩니다.",
            "2026-04-01T00:00:00+00:00",
        )
        adjacent_new = _qa(
            "adjacent", "학생 출결 파일 업로드 오류",
            "출결 명단 CSV 업로드가 인코딩 문제로 실패, EUC-KR을 UTF-8로 변환해 해결.",
            "2026-07-09T00:00:00+00:00",
        )
        out = _order_qa_by_recency([exact_old, adjacent_new])
        assert [r.chunk_id for r in out] == ["exact", "adjacent"]

    def test_missing_excerpts_need_strong_query_overlap(self) -> None:
        # No answer text on either side → only a near-identical question
        # groups (0.6 bar); two-generic-noun overlap must not.
        a = _mk("a", "qa_log", rrf=1.0, mtime="2026-04-01T00:00:00+00:00")
        b = _mk("b", "qa_log", rrf=0.5, mtime="2026-07-09T00:00:00+00:00")
        a = a.__class__(**{**a.__dict__, "name": "학생 숙제 파일 저장 위치"})
        b = b.__class__(**{**b.__dict__, "name": "학생 출결 파일 업로드"})
        out = _order_qa_by_recency([a, b])
        assert [r.chunk_id for r in out] == ["a", "b"]

    def test_non_qa_positions_untouched(self) -> None:
        card = _mk("card", "memory_card", rrf=0.4, mtime="2026-01-01T00:00:00+00:00")
        old_qa = _qa("old", "정산 배치 시각", "새벽 2시 실행", "2026-04-01T00:00:00+00:00")
        new_qa = _qa("new", "정산 배치 시각 변경", "새벽 2시에서 4시로 변경, 실행 스케줄 조정", "2026-07-01T00:00:00+00:00")
        out = _order_qa_by_recency([old_qa, card, new_qa])
        assert out[1].chunk_id == "card"
        assert [out[0].chunk_id, out[2].chunk_id] == ["new", "old"]

    def test_single_qa_passthrough(self) -> None:
        results = [_mk("qa", "qa_log", rrf=0.9), _mk("c", "function", 1.0)]
        assert _order_qa_by_recency(results) is results


class TestMergeMemoryPlacement:
    def test_ambient_head_inserted_at_third_slot(self) -> None:
        code1 = _mk("c1", "function", 1.0)
        code2 = _mk("c2", "function", 0.9)
        code3 = _mk("c3", "function", 0.8)
        qa = _mk("qa", "qa_log", rrf=0.5, mtime="2026-07-01T00:00:00+00:00")
        out = _merge_memory_results(
            [code1, code2, code3], [qa], limit=10, head_limit=1, insert_at=2
        )
        assert [r.chunk_id for r in out] == ["c1", "c2", "qa", "c3"]

    def test_ambient_head_different_topics_ranked_by_relevance(self) -> None:
        code = _mk("c1", "function", 1.0)
        # No excerpts + unrelated names → different topic groups; the
        # HIGHER-SCORING group wins the slot regardless of age.
        qa1 = _mk("q1", "qa_log", rrf=0.9, mtime="2026-07-01T00:00:00+00:00")
        qa2 = _mk("q2", "qa_log", rrf=0.8, mtime="2026-07-02T00:00:00+00:00")
        out = _merge_memory_results([code], [qa1, qa2], limit=10, head_limit=1, insert_at=2)
        assert [r.chunk_id for r in out] == ["c1", "q1"]

    def test_ambient_head_prefers_old_exact_topic_over_fresh_adjacent_topic(self) -> None:
        # The review's blocking case: a fresh adjacent-topic Q&A must not
        # win the guaranteed slot from an old exact-topic one during HEAD
        # SELECTION — the final reorder cannot resurrect a dropped result.
        exact_old = _qa(
            "exact", "학생 숙제 제출 파일은 어디 저장되나",
            "숙제 제출 파일은 Supabase Storage homework 버킷에 저장됩니다.",
            "2026-04-01T00:00:00+00:00",
        )
        exact_old = exact_old.__class__(**{**exact_old.__dict__, "rrf_score": 0.9})
        adjacent_new = _qa(
            "adjacent", "학생 출결 파일 업로드 오류",
            "출결 명단 CSV 업로드가 인코딩 문제로 실패, EUC-KR을 UTF-8로 변환해 해결.",
            "2026-07-09T00:00:00+00:00",
        )
        adjacent_new = adjacent_new.__class__(**{**adjacent_new.__dict__, "rrf_score": 0.5})
        out = _merge_memory_results([], [exact_old, adjacent_new], limit=10, head_limit=1, insert_at=0)
        assert [r.chunk_id for r in out] == ["exact"]

    def test_ambient_head_same_topic_group_represented_by_newest(self) -> None:
        # Same topic: the group's relevance is its best score (old, 0.9) but
        # the NEWEST member represents it — supersession at selection time.
        old = _qa(
            "old", "학생 숙제 제출 파일은 어디 저장되나",
            "숙제 제출 파일은 Supabase Storage homework 버킷, 최대 파일 크기 10MB입니다.",
            "2026-04-01T00:00:00+00:00",
        )
        old = old.__class__(**{**old.__dict__, "rrf_score": 0.9})
        new = _qa(
            "new", "숙제 파일 크기 제한 상향",
            "숙제 파일 최대 크기를 10MB에서 100MB로 올렸습니다. 대용량은 resumable upload.",
            "2026-07-09T00:00:00+00:00",
        )
        new = new.__class__(**{**new.__dict__, "rrf_score": 0.5})
        unrelated = _qa(
            "unrelated", "알림톡 발송 채널 구성",
            "알림톡은 카카오 비즈메시지 단일 채널로 발송됩니다.",
            "2026-07-08T00:00:00+00:00",
        )
        unrelated = unrelated.__class__(**{**unrelated.__dict__, "rrf_score": 0.7})
        out = _merge_memory_results([], [old, new, unrelated], limit=10, head_limit=1, insert_at=0)
        # Group(old,new) relevance 0.9 beats unrelated 0.7; newest represents.
        assert [r.chunk_id for r in out] == ["new"]

    def test_head_cards_stay_score_ordered_over_qa_recency(self) -> None:
        card = _mk("card", "memory_card", rrf=0.4, mtime="2026-01-01T00:00:00+00:00")
        qa = _mk("qa", "qa_log", rrf=0.9, mtime="2026-07-01T00:00:00+00:00")
        out = _merge_memory_results([], [qa, card], limit=10)
        assert [r.chunk_id for r in out] == ["card", "qa"]

    def test_insert_at_beyond_body_appends(self) -> None:
        qa = _mk("qa", "qa_log", rrf=0.5, mtime="2026-07-01T00:00:00+00:00")
        out = _merge_memory_results([], [qa], limit=10, head_limit=1, insert_at=2)
        assert [r.chunk_id for r in out] == ["qa"]


class TestUnanchoredTerms:
    def test_absent_head_noun_is_unanchored(self) -> None:
        # A7: a 쿠폰 query whose hits only share generic process words.
        hit = _mk("doc", "section", 1.0, content="수강료 고지서 발급과 처리 흐름")
        missing = _unanchored_terms("쿠폰 발급과 사용 처리 흐름 정리해줘", [hit])
        assert "쿠폰" in missing
        assert "정리해줘" not in missing  # instruction suffix filtered

    def test_hangul_prefix_sheds_josa(self) -> None:
        hit = _mk("doc", "section", 1.0, content="시스템 구성 개요")
        assert _unanchored_terms("시스템은 어떻게 구성되어 있나", [hit]) == []

    def test_ascii_prefix_needs_four_chars(self) -> None:
        hit = _mk("doc", "section", 1.0, content="카프카 아님: kaboom architecture")
        missing = _unanchored_terms("kafka consumer group", [hit])
        assert "kafka" in missing  # "kabo" != "kafk"

    def test_empty_results_returns_nothing(self) -> None:
        assert _unanchored_terms("쿠폰 발급", []) == []
