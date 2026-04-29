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
    ])
    def test_korean_recall_phrases_trigger(self, query: str) -> None:
        assert _has_memory_intent(query) is True

    @pytest.mark.parametrize("query", [
        "previously discussed authentication flow",
        "what did I ask about the portal earlier",
        "the other day we looked at tuition",
        "before, I searched for admission_results",
        "last time you mentioned consultations",
        "WHAT DID WE ASK ABOUT portal",  # case-insensitive
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
