"""Memory cards whose body is search telemetry (2026-09-08).

The card generator wrote the metrics block of the query that produced the
card where the answer belongs, so the body is provenance — a file list and a
"when to use" line — with nothing anyone asked about. ``memory_card`` outranks
``qa_log`` in the memory head, so the highest-priority answer unit held no
answers: three of them took ranks 1-3 of a rationale question and pushed the
document that answers it to rank 7.
"""

from __future__ import annotations

from hybrid_search.memory.quality import card_has_no_answer

TELEMETRY = """---
type: memory_card
query: "widget lookup path"
summary: "- **query_type**: ENGLISH_NL - **bm25_weight**: 0.4 - **time**: 2099.4 ms - **chunks_searched**: 19618"
decisions: []
followups: []
---

## Summary

- **query_type**: ENGLISH_NL - **bm25_weight**: 0.4 - **chunks_searched**: 19618

## Files

- `services/widget-lookup.ts`
"""

REAL = """---
type: memory_card
query: "왜 캐시를 두 층으로 두나"
summary: "핫패스와 벌크가 같은 캐시를 쓰면 벌크가 핫패스를 밀어낸다"
decisions: ["벌크 경로는 별도 캐시를 쓴다"]
followups: []
---

## Summary

경합의 원인은 크기가 아니라 공유다.
"""


class TestCardHasNoAnswer:
    def test_telemetry_card_is_flagged(self):
        assert card_has_no_answer(TELEMETRY)

    def test_card_with_a_real_summary_is_not(self):
        assert not card_has_no_answer(REAL)

    def test_recorded_decisions_save_a_telemetry_looking_card(self):
        # Conservative on purpose: a card that recorded a decision has
        # something to say even if its summary line is poor.
        assert not card_has_no_answer(
            TELEMETRY.replace('decisions: []', 'decisions: ["hooks stay sync"]')
        )

    def test_needs_two_telemetry_markers(self):
        one = TELEMETRY.replace("**bm25_weight**", "bm25 weight").replace(
            "**chunks_searched**", "chunks searched"
        )
        assert not card_has_no_answer(one)

    def test_empty_content_is_not_flagged(self):
        assert not card_has_no_answer("")
        assert not card_has_no_answer(None)
