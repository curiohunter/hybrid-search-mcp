"""miss CLI — missed-recall records land outside the repo. Synthetic data only."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hybrid_search.memory import misses

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat(timespec="seconds")


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("HYBRID_SEARCH_HOME", str(h))
    return h


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "shop"
    base = root / ".hybrid-search" / "selfeval"
    _write(base / "events.jsonl", [
        {"ts": _ago(10), "query": "too old", "verdict": "betrayed"},
        {"ts": _ago(2), "query": "refund policy why", "source": "prefetch",
         "top_paths": ["a.py"], "verdict": "no_followup"},
        {"ts": _ago(1), "query": "refund decision", "verdict": "betrayed"},
        {"ts": _ago(0.5), "query": "refund decision", "verdict": "betrayed"},
    ])
    _write(base / "pending" / "s1.jsonl", [
        {"ts": _ago(0.1), "query": "why did we drop partial refunds", "top_paths": ["b.py"]},
    ])
    _write(base / "pending" / "s1.claimed.jsonl", [
        {"ts": _ago(0.2), "query": "claimed row is already an event"},
    ])
    return root


class TestRecordMiss:
    def test_writes_outside_the_project(self, home, project) -> None:
        path, row = misses.record_miss(project, "  partial refund   decision ", now=NOW)

        assert path == home / "benchmarks" / "misses-shop.jsonl"
        assert not str(path).startswith(str(project))
        stored = json.loads(path.read_text().splitlines()[-1])
        assert stored == row
        assert row["question"] == "partial refund decision"
        assert row["project"] == "shop"
        assert row["ts"] == NOW.isoformat(timespec="seconds")

    def test_appends(self, home, project) -> None:
        misses.record_miss(project, "one", now=NOW)
        path, _ = misses.record_miss(project, "two", now=NOW)
        assert [json.loads(line)["question"] for line in path.read_text().splitlines()] == [
            "one", "two"]

    def test_recent_queries_newest_first_deduped_windowed(self, home, project) -> None:
        _, row = misses.record_miss(project, "q", now=NOW)
        queries = [r["query"] for r in row["recent_queries"]]
        assert queries == [
            "why did we drop partial refunds",  # pending pre-fetch, not yet scored
            "refund decision",                  # twice → once
            "refund policy why",
        ]
        assert "too old" not in queries
        assert "claimed row is already an event" not in queries
        assert row["recent_queries"][0]["source"] == "prefetch"

    def test_empty_question_is_rejected(self, home, project) -> None:
        with pytest.raises(ValueError):
            misses.record_miss(project, "   ", now=NOW)
        assert not (home / "benchmarks").exists()

    def test_long_question_is_clipped(self, home, project) -> None:
        _, row = misses.record_miss(project, "x" * 5000, now=NOW)
        assert len(row["question"]) == misses.MAX_QUESTION_CHARS

    def test_project_without_selfeval_data(self, home, tmp_path) -> None:
        _, row = misses.record_miss(tmp_path / "fresh", "q", now=NOW)
        assert row["recent_queries"] == []

    def test_unsafe_project_name_is_sanitized(self, home) -> None:
        assert misses.misses_path("a/b c").name == "misses-a-b-c.jsonl"


class TestCli:
    def _run(self, home: Path, cwd: Path, *argv: str) -> subprocess.CompletedProcess:
        src = Path(__file__).resolve().parents[1] / "src"
        return subprocess.run(
            [sys.executable, "-c", "from hybrid_search.cli import main; main()", *argv],
            cwd=cwd, capture_output=True, text=True,
            env={"HYBRID_SEARCH_HOME": str(home), "PYTHONPATH": str(src),
                 "HOME": str(home), "PATH": "/usr/bin:/bin"},
        )

    def test_cli_records(self, home, project) -> None:
        project.mkdir(exist_ok=True)
        res = self._run(home, project, "miss", "partial", "refund", "decision")
        assert res.returncode == 0, res.stderr
        rows = (home / "benchmarks" / "misses-shop.jsonl").read_text().splitlines()
        assert json.loads(rows[-1])["question"] == "partial refund decision"
        assert "recorded miss for shop" in res.stdout

    def test_cli_rejects_blank(self, home, project) -> None:
        project.mkdir(exist_ok=True)
        res = self._run(home, project, "miss", " ")
        assert res.returncode == 2
        assert "miss:" in res.stderr
