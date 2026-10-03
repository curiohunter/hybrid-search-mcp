"""Indexing progress: position, an honest estimate, and no flood in a log."""

from __future__ import annotations

import io

from hybrid_search.index.progress import (
    LOG_INTERVAL,
    ProgressReporter,
    estimate_remaining,
    format_duration,
    format_line,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_format_duration_scales_units() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(59.4) == "59s"
    assert format_duration(220) == "3m 40s"
    assert format_duration(3725) == "1h 02m"


def test_no_estimate_until_enough_is_observed() -> None:
    assert estimate_remaining(2, 100, 10.0) is None  # too few files
    assert estimate_remaining(50, 100, 1.0) is None  # too little time
    assert estimate_remaining(100, 100, 60.0) is None  # nothing left


def test_estimate_follows_the_pace_so_far() -> None:
    assert estimate_remaining(400, 1600, 60.0) == 180.0


def test_line_shows_position_and_time_left() -> None:
    line = format_line(400, 1600, 60.0, "src/app.py")
    assert "[400/1600]" in line
    assert "25%" in line
    assert "~3m 00s left" in line
    assert "src/app.py" in line


def test_long_paths_are_cut_from_the_left() -> None:
    line = format_line(1, 10, 0.0, "a/" * 60 + "leaf.py")
    assert line.endswith("leaf.py")
    assert "…" in line


def test_log_output_is_throttled_by_time() -> None:
    clock, stream = _Clock(), io.StringIO()
    report = ProgressReporter(stream=stream, clock=clock)
    for i in range(1, 1000):
        clock.now = i * 0.1  # 99.9s in total
        report(i, 1000, f"f{i}.py")
    progress_lines = [ln for ln in stream.getvalue().splitlines() if "[" in ln]
    assert 2 <= len(progress_lines) <= 100 / LOG_INTERVAL + 2
    assert "\r" not in stream.getvalue()


def test_first_call_announces_the_total() -> None:
    stream = io.StringIO()
    ProgressReporter(stream=stream, clock=_Clock())(1, 1600, "a.py")
    assert stream.getvalue().splitlines()[0].strip() == "1600 file(s) to index"


def test_last_file_reports_what_happens_next() -> None:
    clock, stream = _Clock(), io.StringIO()
    report = ProgressReporter(stream=stream, clock=clock)
    report(1, 2, "a.py")
    clock.now = 4.0
    report(2, 2, "b.py")
    out = stream.getvalue()
    assert "[2/2] 100%" in out
    assert "finishing embeddings" in out


def test_terminal_redraws_one_line() -> None:
    clock, stream = _Clock(), _Tty()
    report = ProgressReporter(stream=stream, clock=clock)
    for i in range(1, 4):
        clock.now = float(i)
        report(i, 3, f"f{i}.py")
    out = stream.getvalue()
    assert out.count("\r") == 3
    assert out.rstrip().endswith("modules…")


def test_second_pass_reports_again() -> None:
    clock, stream = _Clock(), io.StringIO()
    report = ProgressReporter(stream=stream, clock=clock)
    report(2, 2, "b.py")
    report(1, 2, "a.py")
    assert stream.getvalue().count("file(s) to index") == 2


def test_empty_run_prints_nothing() -> None:
    stream = io.StringIO()
    ProgressReporter(stream=stream, clock=_Clock())(0, 0, "")
    assert stream.getvalue() == ""
