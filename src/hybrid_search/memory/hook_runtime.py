"""Shared hook runtime for Claude Code and Codex memory hooks."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

_MAX_CONTEXT_CHARS = 360
_SESSION_TOPIC_LIMIT = 3
_PREFETCH_RESULT_LIMIT = 3
_ROUTER_ENV = "HYBRID_SEARCH_ROUTER"

_EXPLORATORY_TOKENS_KO = (
    "어떤", "어떻게", "무엇", "무슨", "왜", "어디",
    "설명", "정리", "알려", "보여", "소개",
    "구조", "구성", "흐름", "관계", "아키텍처", "전체",
    "기능", "역할",
)
_MEMORY_INTENT_TOKENS_KO = ("지난번", "이전에", "아까", "저번", "그때")
_EXPLORATORY_TOKENS_EN_RE = re.compile(
    r"\b(how|what|why|where|explain|describe|overview|summary|structure|"
    r"architecture|flow|related|tell\s+me|show\s+me|walk\s+me\s+through)\b",
    re.IGNORECASE,
)
_MEMORY_INTENT_TOKENS_EN_RE = re.compile(
    r"\b(previously|earlier|last\s+time|the\s+other\s+day|"
    r"what\s+did\s+(?:i|we|you)\s+(?:ask|say))\b",
    re.IGNORECASE,
)
_EXPLORATORY_MIN_CHARS = 12
_SKIP_PREFIXES = ("/", "!", "#")


def resolve_project_root(event: dict) -> Path | None:
    """Pick the project root from a hook payload's ``cwd``.

    Hooks often run with ``cwd`` set to the file/task subdirectory. Resolve to
    the enclosing git root first so memory is written once per project, not
    into arbitrary nested content folders.
    """
    cwd = event.get("cwd")
    if not cwd:
        return None
    try:
        cwd_path = Path(cwd).resolve()
    except (OSError, ValueError):
        return None
    for path in (cwd_path, *cwd_path.parents):
        if (path / ".git").exists():
            return path
    for path in (cwd_path, *cwd_path.parents):
        if (path / ".hybrid-search").exists():
            return path
    return None


def classify_prompt_for_memory(prompt: str) -> bool:
    """Return True when a user prompt should receive memory pre-fetch context."""
    p = (prompt or "").strip()
    if not p:
        return False
    if p.startswith(_SKIP_PREFIXES):
        return False
    if p.startswith("@") and " " not in p[:40]:
        return False
    if any(tok in p for tok in _MEMORY_INTENT_TOKENS_KO):
        return True
    if _MEMORY_INTENT_TOKENS_EN_RE.search(p):
        return True
    if len(p) < _EXPLORATORY_MIN_CHARS:
        return False
    if any(tok in p for tok in _EXPLORATORY_TOKENS_KO):
        return True
    if _EXPLORATORY_TOKENS_EN_RE.search(p):
        return True
    return False


def _format_session_start_context(indexes: list) -> str:
    if not indexes:
        return ""
    recent = []
    for idx in indexes[:_SESSION_TOPIC_LIMIT]:
        q = " ".join((idx.query or "").split())
        if len(q) > 42:
            q = q[:39] + "..."
        if q:
            recent.append(q)
    lines = [
        f"[hybrid-search memory] {len(indexes)} past turns available.",
        "Use mcp__hybrid-search__hybrid_search for recall/context.",
    ]
    if recent:
        lines.append("Recent: " + " | ".join(recent))
    return "\n".join(lines)


def build_session_context(project_root: Path, *, limit: int = _SESSION_TOPIC_LIMIT) -> str:
    """Build recent-memory context for a session-start hook."""
    try:
        from hybrid_search.memory import reader

        indexes = list(reader.iter_qa_indexes(project_root))
    except Exception:
        return ""
    return _format_session_start_context(indexes[:limit])[:_MAX_CONTEXT_CHARS]


def _router_enabled() -> bool:
    return os.environ.get(_ROUTER_ENV) != "0"


def _format_user_prompt_context(response, prompt: str | None = None) -> str:
    results = getattr(response, "results", []) or []
    if not results:
        return ""
    confidence = getattr(response, "confidence", "weak") or "weak"
    hint = getattr(response, "fallback_hint", None)
    confidence_line = f"pre-fetch confidence: {confidence}"
    if confidence == "weak" and hint:
        confidence_line += f" · {hint}"
    lines = []
    if prompt is not None and _router_enabled():
        from hybrid_search.memory.router import classify_prompt

        decision = classify_prompt(prompt)
        lines.append(f"[hybrid-search route] suggest {decision.tool} · {decision.reason}")

    lines.extend([
        f"[hybrid-search pre-fetch] {len(results)} hits. Top paths:",
        confidence_line,
    ])
    hit_lines = []
    for i, r in enumerate(results[:_PREFETCH_RESULT_LIMIT], start=1):
        fp = getattr(r, "file_path", "?") or "?"
        start = getattr(r, "start_line", None)
        loc = f":{start}" if start else ""
        hit_lines.append(f"{i}. `{fp}{loc}`")
    lines.extend(hit_lines)
    lines.append("Call hybrid_search for details if needed.")
    context = "\n".join(lines)
    if len(context) <= _MAX_CONTEXT_CHARS:
        return context

    # The route hint has priority. If it pushes the payload over the hook
    # budget, drop only the final hit row while preserving confidence and at
    # least two concrete result locations.
    if prompt is not None and _router_enabled() and len(hit_lines) > 2:
        lines.pop(-2)
        context = "\n".join(lines)
    return context[:_MAX_CONTEXT_CHARS]


def _run_programmatic_search(prompt: str, cwd: str):
    try:
        from hybrid_search.config import load_config
        from hybrid_search.index.embedder import Embedder
        from hybrid_search.project import ProjectRegistry
        from hybrid_search.search.orchestrator import SearchOrchestrator
    except Exception:
        return None

    try:
        cfg = load_config()
        registry = ProjectRegistry(cfg.global_dir)
        embedder = Embedder(cfg.embedding, cfg.models_dir)
        orch = SearchOrchestrator(config=cfg, registry=registry, embedder=embedder)
        return orch.hybrid_search(
            query=prompt,
            cwd=cwd,
            limit=10,
        )
    except Exception:
        return None


def build_user_prompt_context(
    project_root: Path,
    prompt: str,
    *,
    record_prefetch: bool = False,
) -> str:
    """Run the shared exploratory prompt pre-fetch and render context."""
    response = _run_programmatic_search(prompt, str(project_root))
    if response is None:
        return ""

    if record_prefetch:
        try:
            from hybrid_search.memory import qa_log

            qa_log.record(
                query=prompt,
                response=response,
                cwd=str(project_root),
                async_write=False,
                trigger="user_prompt_submit",
            )
        except Exception:
            pass

    return _format_user_prompt_context(response, prompt)


def record_completed_turn(
    project_root: Path,
    prompt: str,
    answer: str | None,
    *,
    trigger: str,
    tools_used: Iterable[str] = (),
    client: str | None = None,
) -> Path | None:
    """Persist a completed conversational turn if both prompt and answer exist."""
    query = (prompt or "").strip()
    final_answer = (answer or "").strip()
    if not query or not final_answer:
        return None
    try:
        from hybrid_search.memory import qa_log

        return qa_log.record_turn(
            query=query,
            cwd=str(project_root),
            tools_used=tuple(tools_used),
            answer_chars=len(final_answer),
            answer_excerpt=final_answer,
            trigger=trigger,
            client=client,
            async_write=False,
            dedup=True,
        )
    except Exception:
        return None
