"""ADV3 fix tests — KO→EN dual-query for the cross-language memory lane.

The ripgrep holdout ADV3 failure: a Korean probe over English qa memories
retrieves nothing. The fix retrieves the memory lane a second time with
an English translation of the query. Covered here:

- ``is_korean_dominant`` — the lane trigger.
- ``QueryTranslator`` — cache semantics and fail-open behavior (network
  is always faked via ``request_fn``; conftest force-disables the lane
  suite-wide so nothing else can reach it).
- ``SearchOrchestrator._cross_language_memory_results`` — wiring: uses
  the translated text for retrieval, applies the ambient rank gate,
  degrades to [] on every failure path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hybrid_search.project import ProjectInfo
from hybrid_search.search.orchestrator import HybridResult, SearchOrchestrator
from hybrid_search.search.translation import (
    QueryTranslator,
    is_enabled,
    is_korean_dominant,
)


# --- is_korean_dominant -------------------------------------------------------

class TestKoreanDominant:
    @pytest.mark.parametrize("query", [
        "우리 환불 기능에 대해 알려줘",
        "가장 최근 대화가 뭐였지?",
        "max_columns 기본값이 뭐지?",           # identifier + Korean carrier
        "SSLContext 설정 어디서 하지",
    ])
    def test_korean_queries_trigger(self, query: str) -> None:
        assert is_korean_dominant(query) is True

    @pytest.mark.parametrize("query", [
        "how does the refund flow work",
        "max_columns default value",
        "ripgrep --max-columns 150",
        "",
        "12345 !!",
    ])
    def test_english_and_empty_do_not(self, query: str) -> None:
        assert is_korean_dominant(query) is False


# --- QueryTranslator ----------------------------------------------------------

class TestQueryTranslator:
    def test_translates_via_request_fn(self, tmp_path: Path) -> None:
        tr = QueryTranslator(
            tmp_path / "cache.jsonl",
            request_fn=lambda q: "what is the max-columns default?",
        )
        assert tr.translate("max-columns 기본값이 뭐지?") == (
            "what is the max-columns default?"
        )

    def test_cache_hit_skips_request(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def fn(q: str) -> str:
            calls.append(q)
            return "translated"

        tr = QueryTranslator(tmp_path / "cache.jsonl", request_fn=fn)
        assert tr.translate("질문") == "translated"
        assert tr.translate("질문") == "translated"
        assert len(calls) == 1

    def test_cache_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.jsonl"
        QueryTranslator(path, request_fn=lambda q: "persisted").translate("질문")
        tr2 = QueryTranslator(
            path, request_fn=lambda q: pytest.fail("must hit the disk cache")
        )
        assert tr2.translate("질문") == "persisted"

    def test_request_failure_fails_open(self, tmp_path: Path) -> None:
        def fn(q: str) -> str:
            raise TimeoutError("slow API")

        tr = QueryTranslator(tmp_path / "cache.jsonl", request_fn=fn)
        assert tr.translate("질문") is None

    def test_empty_translation_fails_open(self, tmp_path: Path) -> None:
        tr = QueryTranslator(tmp_path / "cache.jsonl", request_fn=lambda q: "  ")
        assert tr.translate("질문") is None

    def test_corrupt_cache_line_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.jsonl"
        path.write_text('not json\n' + json.dumps({"k": "x", "v": "y"}) + "\n")
        tr = QueryTranslator(path, request_fn=lambda q: "fresh")
        assert tr.translate("질문") == "fresh"

    def test_suite_wide_toggle_is_off(self) -> None:
        """conftest force-disables the lane for every test."""
        assert is_enabled() is False


# --- orchestrator wiring -------------------------------------------------------

def _mk_result(chunk_id: str, bm25_rank=1, vector_rank=1) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id, rrf_score=0.02, bm25_rank=bm25_rank,
        vector_rank=vector_rank, file_path=f"qa/{chunk_id}.md", project="p",
        name=chunk_id, qualified_name=chunk_id, node_type="qa_log",
        start_line=1, end_line=5, content="body", snippet="s",
    )


def _mk_orch() -> SearchOrchestrator:
    config = MagicMock()
    config.search.rrf_k = 60
    registry = MagicMock()
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.1, 0.2]
    orch = SearchOrchestrator(config, registry, embedder)
    orch._search_single = MagicMock(
        return_value=(["en-1"], ["en-1"], 1, [], {}, {})
    )
    return orch


_PINFO = ProjectInfo(
    id="p1", name="p", path="/tmp/p",
    last_indexed_at=None, file_count=1, chunk_count=1,
)


class TestCrossLanguageLane:
    def _call(self, orch: SearchOrchestrator, *, memory_intent: bool = True):
        return orch._cross_language_memory_results(
            "가장 최근 대화가 뭐였지?", [_PINFO],
            depth=30,
            memory_node_types=["qa_log", "memory_card"],
            file_pattern=None,
            exclude_pattern=None,
            primary_project_id=None,
            memory_intent=memory_intent,
            limit=10,
        )

    def test_retrieves_with_translated_text(self, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()
        orch._translator = MagicMock()
        orch._translator.translate.return_value = "what was our latest conversation?"
        orch._enrich_results = MagicMock(return_value=[_mk_result("en-1")])

        results, state = self._call(orch)

        assert [r.chunk_id for r in results] == ["en-1"]
        assert state == "used"
        # Retrieval must run on the TRANSLATED text, not the Korean original.
        args = orch._search_single.call_args[0]
        assert args[1] == "what was our latest conversation?"
        orch._embedder.embed_query.assert_called_once_with(
            "what was our latest conversation?"
        )

    def test_ambient_gate_applies_when_no_memory_intent(self, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()
        orch._translator = MagicMock()
        orch._translator.translate.return_value = "translated"
        orch._enrich_results = MagicMock(return_value=[
            _mk_result("good", bm25_rank=3, vector_rank=None),
            _mk_result("junk", bm25_rank=25, vector_rank=30),
        ])
        results, state = self._call(orch, memory_intent=False)
        assert [r.chunk_id for r in results] == ["good"]
        assert state == "used"

    def test_no_translation_reports_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()
        orch._translator = MagicMock()
        orch._translator.translate.return_value = None
        assert self._call(orch) == ([], "skipped")
        orch._embedder.embed_query.assert_not_called()

    def test_kill_switch_reports_skipped(self) -> None:
        # conftest already sets HYBRID_SEARCH_TRANSLATION=0
        orch = _mk_orch()
        orch._translator = MagicMock()
        orch._translator.translate.return_value = "translated"
        assert self._call(orch) == ([], "skipped")
        orch._translator.translate.assert_not_called()

    def test_embed_failure_reports_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()
        orch._translator = MagicMock()
        orch._translator.translate.return_value = "translated"
        orch._embedder.embed_query.side_effect = RuntimeError("API down")
        assert self._call(orch) == ([], "skipped")


class TestEndToEndDeadline:
    """Round-2 fix 4: the WHOLE lane (translation + embedding +
    retrieval) is bounded — a slow embedding call must not stall the
    search past the deadline; the lane is dropped and reported skipped."""

    def _deadline_call(self, orch, deadline_s: float):
        return orch._cross_language_with_deadline(
            "가장 최근 대화가 뭐였지?", [_PINFO],
            deadline_s=deadline_s,
            depth=30,
            memory_node_types=["qa_log"],
            file_pattern=None,
            exclude_pattern=None,
            primary_project_id=None,
            memory_intent=True,
            limit=10,
        )

    def test_deadline_exceeded_returns_skipped_quickly(self, monkeypatch) -> None:
        import time as _time

        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()

        def slow_lane(*args, **kwargs):
            _time.sleep(0.5)
            return [_mk_result("late")], "used"

        orch._cross_language_memory_results = slow_lane
        start = _time.monotonic()
        result = self._deadline_call(orch, deadline_s=0.05)
        elapsed = _time.monotonic() - start
        assert result == ([], "skipped")
        assert elapsed < 0.4  # bounded well under the worker's 0.5s sleep

    def test_fast_lane_passes_through(self, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()
        orch._cross_language_memory_results = lambda *a, **k: (
            [_mk_result("fast")], "used",
        )
        results, state = self._deadline_call(orch, deadline_s=2.0)
        assert [r.chunk_id for r in results] == ["fast"]
        assert state == "used"

    def test_worker_exception_reports_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()

        def boom(*args, **kwargs):
            raise RuntimeError("lane crashed")

        orch._cross_language_memory_results = boom
        assert self._deadline_call(orch, deadline_s=1.0) == ([], "skipped")

    def test_repeated_timeouts_bound_orphan_workers(self, monkeypatch) -> None:
        """Round-2 re-review fix 4: timed-out workers can't be cancelled,
        only orphaned — repeated timeouts must not stack unbounded
        threads/API calls. The slot semaphore caps live workers; once
        saturated, further queries skip the lane instantly."""
        import threading as _threading
        import time as _time

        monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "1")
        orch = _mk_orch()
        active = 0
        peak = 0
        lock = _threading.Lock()

        def slow_lane(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                _time.sleep(0.3)
                return [], "used"
            finally:
                with lock:
                    active -= 1

        orch._cross_language_memory_results = slow_lane
        start = _time.monotonic()
        results = [self._deadline_call(orch, deadline_s=0.02) for _ in range(20)]
        elapsed = _time.monotonic() - start

        assert all(r == ([], "skipped") for r in results)
        assert peak <= orch._CROSS_LANGUAGE_MAX_WORKERS
        # 18 of the 20 calls hit the saturated circuit and return
        # instantly; only the slot-holding calls pay the deadline.
        assert elapsed < 2.0
        # Slots are released once orphans finish — the lane recovers.
        _time.sleep(0.5)
        orch._cross_language_memory_results = lambda *a, **k: ([], "used")
        assert self._deadline_call(orch, deadline_s=1.0) == ([], "used")
