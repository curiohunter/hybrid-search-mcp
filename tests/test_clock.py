"""A frozen snapshot must carry its own clock.

Regression for the 2026-09-23 instrument gap: ``_apply_memory_boost`` aged
records against the wall clock, so re-measuring the same frozen snapshot on
a later day moved the numbers with code and corpus unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hybrid_search import clock
from hybrid_search.search.orchestrator import (
    HybridResult,
    _apply_memory_boost,
    _parse_mtime_days_ago,
)


@pytest.fixture(autouse=True)
def _no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(clock.NOW_ENV, raising=False)


def _qa(mtime: str) -> HybridResult:
    return HybridResult(
        chunk_id="q", rrf_score=1.0, bm25_rank=1, vector_rank=1,
        file_path=".hybrid-search/qa/x.md", project="p", name="q",
        qualified_name="q", node_type="qa_log", start_line=1, end_line=1,
        content=None, snippet="", file_mtime=mtime,
    )


class TestNow:
    def test_wall_clock_without_pin(self) -> None:
        before = datetime.now(timezone.utc)
        assert before <= clock.now() <= datetime.now(timezone.utc)

    def test_pin_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "2026-09-12T00:00:00+00:00")
        assert clock.now() == datetime(2026, 9, 12, tzinfo=timezone.utc)

    def test_bare_date_is_utc_midnight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "2026-09-12")
        assert clock.now() == datetime(2026, 9, 12, tzinfo=timezone.utc)

    def test_bad_pin_falls_back_to_wall_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "next tuesday")
        assert clock.now().year >= 2026


class TestRankingReadsThePin:
    def test_age_uses_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "2026-09-12T00:00:00+00:00")
        assert _parse_mtime_days_ago("2026-09-02T00:00:00+00:00") == pytest.approx(10.0)

    def test_same_snapshot_scores_same_on_any_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "2026-09-12T00:00:00+00:00")
        pinned = _apply_memory_boost([_qa("2026-08-13T00:00:00+00:00")],
                                     memory_intent=False)
        # 30 days at the pin → decay 0.5 → 1 + 0.20 * 0.5
        assert pinned[0].rrf_score == pytest.approx(1.10)

    def test_explicit_now_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "2030-01-01")
        out = _apply_memory_boost([_qa("2026-09-12T00:00:00+00:00")],
                                  memory_intent=False,
                                  now=datetime(2026, 9, 12, tzinfo=timezone.utc))
        assert out[0].rrf_score == pytest.approx(1.20)


class TestSnapshotClock:
    def test_stamp_then_pin_round_trips(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "config.toml").write_text("", encoding="utf-8")
        stamped = clock.stamp_snapshot(
            tmp_path, datetime(2026, 9, 12, 3, 4, 5, tzinfo=timezone.utc))
        assert clock.pin_to_snapshot(tmp_path / "config.toml") == stamped
        assert clock.now() == datetime(2026, 9, 12, 3, 4, 5, tzinfo=timezone.utc)

    def test_unstamped_snapshot_pins_nothing(self, tmp_path) -> None:
        assert clock.pin_to_snapshot(tmp_path / "config.toml") is None
        assert clock.pin_to_snapshot(None) is None

    def test_explicit_env_beats_stamp(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock.stamp_snapshot(tmp_path, datetime(2026, 9, 12, tzinfo=timezone.utc))
        monkeypatch.setenv(clock.NOW_ENV, "2026-09-01")
        assert clock.pin_to_snapshot(tmp_path / "config.toml") == "2026-09-01"

    def test_bad_env_fails_the_measurement(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(clock.NOW_ENV, "next tuesday")
        with pytest.raises(ValueError):
            clock.pin_to_snapshot(tmp_path / "config.toml")

    def test_corrupt_stamp_fails_the_measurement(self, tmp_path) -> None:
        (tmp_path / clock.SNAPSHOT_CLOCK_FILE).write_text("garbage", encoding="utf-8")
        with pytest.raises(ValueError):
            clock.pin_to_snapshot(tmp_path / "config.toml")
