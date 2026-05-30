#!/usr/bin/env python3
"""A1+A2 PoC demo — eyeball conversation chunks from Claude + Codex transcripts.

    python scripts/poc_transcript_chunks.py [PROJECT_PATH] [--show N] [--source claude|codex]

Prints discovery counts, per-chunk summaries, and a few full chunk bodies so we
can judge whether the "decision episode" boundary is useful before building
persistence/retrieval (Phase A3+).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hybrid_search.index.transcript_source import (  # noqa: E402
    ConvChunk,
    discover_claude_transcripts,
    discover_codex_sessions,
    parse_claude_transcript,
    parse_codex_session,
)


def _summarize(chunks: list[ConvChunk]) -> None:
    if not chunks:
        print("  (no chunks)")
        return
    lens = [c.char_len for c in chunks]
    lens.sort()
    n = len(lens)
    print(f"  chunks={n}  chars: min={lens[0]} median={lens[n // 2]} max={lens[-1]}")
    tool_counter: Counter[str] = Counter()
    with_tools = 0
    with_files = 0
    for c in chunks:
        if c.tools:
            with_tools += 1
        if c.files:
            with_files += 1
        for t in c.tools:
            tool_counter[t.tool] += 1
    print(f"  turns with tools={with_tools}/{n}  with files={with_files}/{n}")
    if tool_counter:
        top = ", ".join(f"{name}×{cnt}" for name, cnt in tool_counter.most_common(8))
        print(f"  top tools: {top}")


def _print_chunk(c: ConvChunk, width: int = 78) -> None:
    print("─" * width)
    print(f"[{c.source}] session={c.session_id[:12]} turn={c.turn_index} ts={c.timestamp}")
    print(f"USER: {c.user_prompt[:200]}")
    if c.assistant_excerpt:
        print(f"ASSISTANT: {c.assistant_excerpt[:200]}")
    if c.tools:
        tools = ", ".join(f"{t.tool}({t.target})" if t.target else t.tool for t in c.tools[:6])
        print(f"TOOLS: {tools}")
    if c.files:
        print(f"FILES: {', '.join(c.files[:8])}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", default=str(Path.cwd()))
    parser.add_argument("--show", type=int, default=6, help="full chunks to print")
    parser.add_argument("--source", choices=["claude", "codex", "both"], default="both")
    args = parser.parse_args()

    project_path = Path(args.project).resolve()
    print(f"Project: {project_path}\n")

    claude_files = discover_claude_transcripts(project_path) if args.source != "codex" else []
    codex_files = discover_codex_sessions(project_path) if args.source != "claude" else []
    print(f"Discovered: claude={len(claude_files)} transcripts, codex={len(codex_files)} sessions\n")

    claude_chunks: list[ConvChunk] = []
    for f in claude_files:
        claude_chunks.extend(parse_claude_transcript(f))
    codex_chunks: list[ConvChunk] = []
    for f in codex_files:
        codex_chunks.extend(parse_codex_session(f))

    if args.source != "codex":
        print("== Claude ==")
        _summarize(claude_chunks)
    if args.source != "claude":
        print("== Codex ==")
        _summarize(codex_chunks)

    all_chunks = claude_chunks + codex_chunks
    sample = [c for c in all_chunks if c.tools][: args.show] or all_chunks[: args.show]
    print(f"\n== Sample chunks (showing {len(sample)}, tool-bearing first) ==")
    for c in sample:
        _print_chunk(c)


if __name__ == "__main__":
    main()
