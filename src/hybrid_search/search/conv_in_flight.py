"""Query-time overlay for in-flight (not-yet-indexed) conversation turns.

Sibling to ``in_flight.py``. The per-turn Stop-hook conversation indexer is
detached and asynchronous, so the freshest turns of a live session can lag the
store by a few seconds — and the turn in progress is never indexed until it
ends. On recall-shaped queries this overlay reads the most recent transcripts
for the cwd project, keeps only turns whose conv chunk id is absent from the
store, scores them locally, and hands them to the conversation lane. Nothing is
written to any persistent index.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hybrid_search.index.conversation_indexer import (
    CONV_NODE_TYPE,
    _chunk_id,
    _conv_rel_path,
    conv_file_id,
)
from hybrid_search.index.transcript_source import (
    ConvChunk,
    Source,
    discover_recent_transcripts,
    parse_claude_transcript,
    parse_codex_session,
)
from hybrid_search.search.in_flight import _query_tokens, _tokens
from hybrid_search.storage.db import StoreDB

MAX_FILES = 4
MAX_TURNS_PER_SESSION = 6
SNIPPET_CHARS = 500
PROMPT_PREVIEW_CHARS = 120
NAME_MAX = 80
_IDENT_WEIGHT = 0.006
_PLAIN_WEIGHT = 0.003
_RECENCY_WEIGHT = 0.00002


@dataclass(frozen=True)
class ConvInFlightTurn:
    """A live-session turn that is not yet in the store."""

    chunk_id: str
    source: Source
    session_id: str
    chunk: ConvChunk


def collect_conv_in_flight(
    project_path: Path,
    project_id: str,
    db: StoreDB,
    *,
    max_files: int = MAX_FILES,
    max_turns: int = MAX_TURNS_PER_SESSION,
    claude_root: Path | None = None,
    codex_root: Path | None = None,
) -> list[ConvInFlightTurn]:
    """Recent transcript turns whose chunk id is absent from the store.

    Reuses the indexer's ``_chunk_id`` so a turn already embedded is dropped —
    only the genuine tail lag survives. Bounded by ``max_files`` transcripts and
    ``max_turns`` newest turns per session; never raises on a bad transcript.
    """
    out: list[ConvInFlightTurn] = []
    transcripts = discover_recent_transcripts(
        project_path,
        max_files=max_files,
        claude_root=claude_root,
        codex_root=codex_root,
    )
    for source, path in transcripts:
        try:
            chunks = (
                parse_codex_session(path)
                if source == "codex"
                else parse_claude_transcript(path)
            )
        except Exception:
            continue
        if not chunks:
            continue
        session_id = path.stem
        existing = set(db.get_chunk_ids_by_file(
            conv_file_id(project_id, _conv_rel_path(source, session_id))
        ))
        fresh = [
            ConvInFlightTurn(
                chunk_id=cid, source=source, session_id=session_id, chunk=c,
            )
            for c in chunks
            if (cid := _chunk_id(source, session_id, c)) not in existing
        ]
        # The lag is at the tail: older unindexed turns are rare and low-value,
        # so keep only the newest few per session. Guard the slice — a max_turns
        # of 0 must mean "none", not fresh[-0:] (which is the whole list).
        out.extend(fresh[-max_turns:] if max_turns > 0 else [])
    return out


def score_conv_in_flight(
    turns: list[ConvInFlightTurn],
    *,
    query: str,
    project_name: str,
    project_id: str,
    limit: int = 3,
) -> list:
    """Score in-flight turns by token overlap and return ``HybridResult`` rows."""
    from hybrid_search.search.orchestrator import HybridResult

    query_tokens = _query_tokens(query)
    if not query_tokens or not turns:
        return []

    scored: list[tuple[float, ConvInFlightTurn]] = []
    for turn in turns:
        text_tokens = _tokens(turn.chunk.text)
        score = 0.0
        for token, is_identifier in query_tokens.items():
            if token in text_tokens:
                score += _IDENT_WEIGHT if is_identifier else _PLAIN_WEIGHT
        if score <= 0:
            continue
        # Recency nudge so a live tail turn edges out an equally-matching
        # earlier in-flight turn — the freshest turn is usually the answer.
        score += min(turn.chunk.turn_index, 50) * _RECENCY_WEIGHT
        scored.append((score, turn))

    scored.sort(key=lambda pair: (-pair[0], -pair[1].chunk.turn_index))
    results = []
    for rank, (score, turn) in enumerate(scored[:limit], start=1):
        chunk = turn.chunk
        prompt_preview = (chunk.user_prompt or "")[:PROMPT_PREVIEW_CHARS]
        snippet = (
            f"[in-flight conversation - {turn.source}] {prompt_preview}\n"
            f"{chunk.text[:SNIPPET_CHARS]}"
        )
        results.append(
            HybridResult(
                chunk_id=turn.chunk_id,
                rrf_score=round(score, 6),
                bm25_rank=rank,
                vector_rank=None,
                file_path=_conv_rel_path(turn.source, turn.session_id),
                project=project_name,
                name=(chunk.user_prompt or "")[:NAME_MAX],
                qualified_name=f"{turn.source}:{turn.session_id}#{chunk.turn_index}",
                node_type=CONV_NODE_TYPE,
                start_line=None,
                end_line=None,
                content=chunk.text,
                snippet=snippet,
                trust_meta=(
                    f"[conversation - {turn.source}; in-flight, not yet indexed "
                    "— turn content is in this result]"
                ),
            )
        )
    return results
