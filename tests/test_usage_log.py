"""Every paid call leaves a local record.

Without one, "where did the money go" can only be answered by inference.
During the Gemini migration that produced three different explanations for
one day's spend, two of them wrong, because nothing on this side knew what
had actually been sent. A provider dashboard showing more than this log is
now evidence of usage from somewhere else, not a guess.
"""

from __future__ import annotations

import json
import time

import pytest

from hybrid_search import usage


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HYBRID_SEARCH_HOME", str(tmp_path))
    monkeypatch.delenv(usage.DISABLE_ENV, raising=False)


class TestRecording:
    def test_a_call_is_appended(self):
        usage.record(kind="embed", provider="gemini", model="m", items=100, tokens=1000)
        rows = [json.loads(x) for x in usage.log_path().read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["items"] == 100 and rows[0]["tokens"] == 1000

    def test_calls_accumulate(self):
        for _ in range(3):
            usage.record(kind="embed", provider="g", model="m", items=1, tokens=10)
        assert usage.summarize()["totals"][("embed", "m")]["calls"] == 3

    def test_no_content_is_stored(self):
        """Accounting, not a copy of the corpus."""
        usage.record(kind="embed", provider="g", model="m", items=1, tokens=5)
        row = json.loads(usage.log_path().read_text())
        assert set(row) == {"ts", "kind", "provider", "model", "items", "tokens", "pid"}

    def test_opt_out_is_honoured(self, monkeypatch):
        monkeypatch.setenv(usage.DISABLE_ENV, "1")
        usage.record(kind="embed", provider="g", model="m", items=1, tokens=1)
        assert not usage.log_path().exists()

    def test_an_unwritable_log_does_not_raise(self, monkeypatch):
        """Accounting must never be the reason indexing stops."""
        monkeypatch.setenv("HYBRID_SEARCH_HOME", "/proc/nonexistent-should-fail")
        usage.record(kind="embed", provider="g", model="m", items=1, tokens=1)


class TestSummary:
    def test_totals_split_by_kind_and_model(self):
        usage.record(kind="embed", provider="g", model="e", items=2, tokens=20)
        usage.record(kind="chat", provider="g", model="c", items=1, tokens=5)
        totals = usage.summarize()["totals"]
        assert totals[("embed", "e")]["tokens"] == 20
        assert totals[("chat", "c")]["tokens"] == 5

    def test_the_window_excludes_older_calls(self):
        usage.record(kind="embed", provider="g", model="m", items=1, tokens=7)
        assert usage.summarize(since=time.time() + 60)["totals"] == {}

    def test_a_torn_line_does_not_hide_the_rest(self):
        usage.record(kind="embed", provider="g", model="m", items=1, tokens=7)
        with open(usage.log_path(), "a") as fh:
            fh.write("{not json\n")
        usage.record(kind="embed", provider="g", model="m", items=1, tokens=7)
        assert usage.summarize()["totals"][("embed", "m")]["calls"] == 2

    def test_no_log_summarizes_to_nothing(self):
        assert usage.summarize()["totals"] == {}
