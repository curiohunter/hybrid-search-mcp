"""One definition of "this line is provenance, not an answer" (2026-09-08).

The pre-fetch renderer learned to skip ``- **key**: value`` bullets on
2026-09-05; the memory-card generator did not, and kept picking them up as a
card's summary. Every card in one project ended up summarising the search that
made it. The judgement now lives in one place and both callers use it.
"""

from __future__ import annotations

from hybrid_search.memory.cards import _summary_from_body
from hybrid_search.memory.quality import is_metadata_block, is_metadata_bullet


class TestMetadataBullet:
    def test_provenance_bullets(self):
        assert is_metadata_bullet("- **query_type**: KOREAN_NL")
        assert is_metadata_bullet("  - **time**: 2099.4 ms")

    def test_ordinary_bullets_are_not_metadata(self):
        assert not is_metadata_bullet("- 벌크 경로는 별도 캐시를 쓴다")
        assert not is_metadata_bullet("- **결정**한 것은 따로 적는다")  # no colon
        assert not is_metadata_bullet("")

    def test_block_needs_every_line(self):
        assert is_metadata_block("- **a**: 1\n- **b**: 2")
        assert is_metadata_block("## Summary\n- **a**: 1")
        assert not is_metadata_block("- **a**: 1\n실제 답이 여기 있다")
        assert not is_metadata_block("")


class TestCardSummary:
    QA = """# Q: widget lookup path

## Answer excerpt

- **query_type**: ENGLISH_NL
- **bm25_weight**: 0.4
- **chunks_searched**: 19618

조회는 캐시를 먼저 보고 없으면 원본을 읽는다.

## Top results
"""

    def test_summary_skips_the_metrics_block(self):
        assert _summary_from_body("q", self.QA) == (
            "조회는 캐시를 먼저 보고 없으면 원본을 읽는다."
        )

    def test_falls_back_to_the_query_when_there_is_only_metadata(self):
        only_meta = "## Answer excerpt\n\n- **time**: 1 ms\n"
        assert _summary_from_body("어떻게 되나", only_meta) == "어떻게 되나"
