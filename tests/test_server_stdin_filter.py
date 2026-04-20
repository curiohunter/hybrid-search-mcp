"""Q3 — blank-line filter between MCP client and JSON-RPC parser.

`_filter_blank_stdin` mutates fd 0 globally, which is unsafe to run inside
pytest's main process. These tests spawn a subprocess that installs the
filter, then prove that blank lines are dropped and real JSON-RPC frames
pass through intact.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


RELAY_SCRIPT = textwrap.dedent(
    """
    import sys
    from hybrid_search.server import _filter_blank_stdin
    _filter_blank_stdin()
    # After the filter, reading sys.stdin should see non-blank lines only.
    for line in sys.stdin:
        sys.stdout.write(line)
        sys.stdout.flush()
    """
).strip()


def _run_filter_on(input_text: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", RELAY_SCRIPT],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestFilterBlankStdin:
    def test_drops_blank_lines_between_frames(self) -> None:
        frames = '{"jsonrpc":"2.0","id":1}\n\n{"jsonrpc":"2.0","id":2}\n'
        out = _run_filter_on(frames)
        # Two non-blank lines survive; the blank between them is dropped.
        lines = [ln for ln in out.split("\n") if ln]
        assert lines == [
            '{"jsonrpc":"2.0","id":1}',
            '{"jsonrpc":"2.0","id":2}',
        ]

    def test_drops_leading_and_trailing_blanks(self) -> None:
        out = _run_filter_on('\n\n{"ok":true}\n\n')
        assert out.strip() == '{"ok":true}'

    def test_whitespace_only_lines_dropped(self) -> None:
        """Tabs/spaces-only lines must also be dropped."""
        out = _run_filter_on('   \n\t\n{"x":1}\n')
        assert out.strip() == '{"x":1}'

    def test_passthrough_preserves_content(self) -> None:
        """No blanks → output matches input exactly."""
        frames = '{"jsonrpc":"2.0","id":1}\n{"jsonrpc":"2.0","id":2}\n'
        assert _run_filter_on(frames) == frames
