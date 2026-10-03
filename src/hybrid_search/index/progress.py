"""Indexing progress for a person watching a terminal.

A first index embeds every file — minutes on a real project — and the old
callback printed one line per 50 files with no sense of how long was left,
which read as a hang. This reports position and an estimate of the time
remaining: redrawn in place on a terminal, as spaced-out lines when the
output is a pipe or a log.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, TextIO

# Seconds between lines when the output is not a terminal — frequent enough
# that an agent tailing the log sees movement, sparse enough not to flood it.
LOG_INTERVAL = 10.0
# Seconds between redraws on a terminal.
TTY_INTERVAL = 0.2
# No estimate before this much has been observed: the first files carry
# start-up cost, and an estimate from them is wrong by multiples.
MIN_ELAPSED_FOR_ETA = 3.0
MIN_FILES_FOR_ETA = 5
PATH_WIDTH = 48


def format_duration(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def estimate_remaining(current: int, total: int, elapsed: float) -> float | None:
    """Seconds left at the pace so far, or None while it is too early to say."""
    if current < MIN_FILES_FOR_ETA or elapsed < MIN_ELAPSED_FOR_ETA or current >= total:
        return None
    return elapsed / current * (total - current)


def _shorten(path: str, width: int = PATH_WIDTH) -> str:
    return path if len(path) <= width else "…" + path[-(width - 1):]


def format_line(current: int, total: int, elapsed: float, path: str) -> str:
    percent = int(current * 100 / total) if total else 100
    remaining = estimate_remaining(current, total, elapsed)
    eta = f" · ~{format_duration(remaining)} left" if remaining is not None else ""
    return f"  [{current}/{total}] {percent}%{eta} · {_shorten(path)}"


class ProgressReporter:
    """Callable matching the pipeline's ``ProgressCallback``."""

    def __init__(
        self,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._clock = clock
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._interval = TTY_INTERVAL if self._tty else LOG_INTERVAL
        self._started: float | None = None
        self._last_emit: float | None = None
        self._last_width = 0
        self._finished = False

    def __call__(self, current: int, total: int, path: str) -> None:
        if total <= 0:
            return
        if self._finished:
            if current >= total:
                return
            # The pipeline started over (a rebuild after a consistency
            # mismatch) — a second pass deserves its own progress.
            self._started = self._last_emit = None
            self._finished = False
        now = self._clock()
        if self._started is None:
            self._started = now
            self._announce(total)
        done = current >= total
        due = self._last_emit is None or now - self._last_emit >= self._interval
        if not (done or due):
            return
        self._last_emit = now
        self._emit(format_line(current, total, now - self._started, path))
        if done:
            self._finish(total, now - self._started)

    def _announce(self, total: int) -> None:
        self._stream.write(f"  {total} file(s) to index\n")
        self._stream.flush()

    def _emit(self, line: str) -> None:
        if self._tty:
            padding = " " * max(0, self._last_width - len(line))
            self._stream.write(f"\r{line}{padding}")
            self._last_width = len(line)
        else:
            self._stream.write(line + "\n")
        self._stream.flush()

    def _finish(self, total: int, elapsed: float) -> None:
        self._finished = True
        if self._tty:
            self._stream.write("\n")
        # The file loop is not the end: embeddings flush, then the call graph
        # and modules are built. Say so, or the pause after 100% looks hung.
        self._stream.write(
            f"  {total} file(s) read in {format_duration(elapsed)} — "
            "finishing embeddings, call graph and modules…\n"
        )
        self._stream.flush()
