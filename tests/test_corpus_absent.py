"""Corpus-absent confidence cap — StoreDB probe + weak demotion wiring."""

from __future__ import annotations

from pathlib import Path

from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


def _make_db(tmp_path: Path) -> StoreDB:
    db = StoreDB(tmp_path / "store.db")
    files = [
        FileRecord(id="f-code", project_id="p1", relative_path="src/billing.ts", file_hash="h1"),
        FileRecord(id="f-qa", project_id="p1", relative_path=".hybrid-search/qa/2026/07/x.md", file_hash="h2"),
    ]
    chunks = [
        ChunkRecord(
            id="c-code", file_id="f-code", project_id="p1",
            node_type="function", content="수강료 고지서 발급 처리 100% match",
        ),
        ChunkRecord(
            id="c-qa", file_id="f-qa", project_id="p1",
            node_type="qa_log", content="쿠폰 발급과 사용 처리 흐름 정리해줘",
        ),
    ]
    with db.transaction() as conn:
        for f in files:
            db.upsert_file(conn, f)
        db.insert_chunks(conn, chunks)
    return db


class TestSourceContainsSubstring:
    def test_code_content_is_found(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert db.source_contains_substring("p1", "발급") is True

    def test_qa_echo_does_not_count(self, tmp_path: Path) -> None:
        # "쿠폰" exists only inside a qa_log — a past *question* about the
        # absent topic. The probe must not count that echo as presence.
        db = _make_db(tmp_path)
        assert db.source_contains_substring("p1", "쿠폰") is False

    def test_like_wildcards_are_escaped(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        # Unescaped, "100%" would match "100<anything>".
        assert db.source_contains_substring("p1", "100% match") is True
        assert db.source_contains_substring("p1", "100%x") is False

    def test_other_project_not_visible(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert db.source_contains_substring("p2", "발급") is False


class TestCorpusAbsentCap:
    def _response(self, corpus_lacks):
        from tests.test_orchestrator import _make_orchestrator
        from hybrid_search.search.orchestrator import HybridResult

        orch = _make_orchestrator({})
        hit = HybridResult(
            chunk_id="a", rrf_score=0.016, bm25_rank=1, vector_rank=1,
            file_path="docs/billing.md", project="test", name="처리 흐름",
            qualified_name="a", node_type="section", start_line=1, end_line=2,
            content="고지서 발급 처리 흐름", snippet="",
        )
        return orch._make_response(
            query="쿠폰 발급과 사용 처리 흐름 정리해줘",
            results=[hit],
            query_type="KOREAN_NL",
            effective_bm25_weight=0.15,
            query_time_ms=1.0,
            total_chunks_searched=10,
            corpus_lacks=corpus_lacks,
        )

    def test_corpus_absent_term_caps_to_weak(self) -> None:
        resp = self._response(corpus_lacks=lambda terms: "쿠폰" if "쿠폰" in terms else None)
        assert resp.confidence == "weak"
        assert resp.fallback_hint  # weak must carry the fallback contract

    def test_corpus_present_terms_keep_confidence(self) -> None:
        resp = self._response(corpus_lacks=lambda terms: None)
        assert resp.confidence == "mixed"

    def test_no_callable_keeps_confidence(self) -> None:
        resp = self._response(corpus_lacks=None)
        assert resp.confidence == "mixed"
