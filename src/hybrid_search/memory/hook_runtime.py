"""Shared hook runtime for Claude Code and Codex memory hooks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from hybrid_search.project import canonical_project_root

_CONV_INDEX_ENV = "HYBRID_SEARCH_CONV_INDEX"


def _build_conv_index_command(
    transcript_path: "Path | str",
    project_root: "Path | str",
    source: str,
) -> list[str]:
    """The detached CLI invocation for single-transcript indexing (pure)."""
    return [
        sys.executable, "-m", "hybrid_search.cli", "index-conversations",
        "--transcript", str(transcript_path),
        "--cwd", str(project_root),
        "--source", source,
    ]


def spawn_conversation_index(
    transcript_path: "Path | str",
    project_root: "Path | str",
    source: str,
) -> None:
    """Fire-and-forget background indexing of one transcript after a turn ends.

    Spawns a detached ``index-conversations --transcript`` process so the Stop
    hook returns immediately. Incremental indexing embeds only the new turn,
    so per-turn cost is one tiny embedding call. Disable with
    ``HYBRID_SEARCH_CONV_INDEX=0``. Never blocks or raises.
    """
    toggle = os.environ.get(_CONV_INDEX_ENV, "1").strip().lower()
    if toggle in ("0", "false", "no", "off"):
        return
    # Stay hermetic under pytest — never launch real indexing subprocesses
    # (which would hit the embedder and the real index) during the test suite.
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        subprocess.Popen(
            _build_conv_index_command(transcript_path, project_root, source),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


_MAX_CONTEXT_CHARS = 360
_SESSION_TOPIC_LIMIT = 3

# The pre-fetch fires ONCE per user turn, not once per tool call, so it does
# not share the PreToolUse budget (whose 360/800 caps exist because a chatty
# session can fire Grep twenty times). Sizing the single best shot of the
# memory layer like a per-Grep nudge is a category error, and the 2026-09-04
# selfeval numbers show what it cost: of 156 scored pre-fetches, exactly ONE
# was adopted. At 360 chars the injection could only ever be a route line, a
# confidence line and three bare paths — no content, nothing to judge
# relevance by, and a closing nudge to call the tool instead. An agent has no
# reason to open a path it knows nothing about.
_PREFETCH_MAX_CONTEXT_CHARS = 2400
_PREFETCH_RESULT_LIMIT = 6

# Memory chunk types have no file to open — the retrieved text IS the answer,
# so it must be quoted here or the hit is worthless. Everything else is a
# real file and gets a locator plus one line of what it contains.
_VIRTUAL_NODE_TYPES = frozenset({"qa_log", "conv_turn", "memory_card", "commit"})
_MEMORY_EXCERPT_CHARS = 320
_CODE_SNIPPET_CHARS = 120
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
    """Pick the project root from a hook payload's ``cwd``."""
    return canonical_project_root(event.get("cwd"))


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


def _format_session_start_context(indexes: list, *, client: str = "claude") -> str:
    if not indexes:
        return ""
    recent = []
    for idx in indexes[:_SESSION_TOPIC_LIMIT]:
        q = " ".join((idx.query or "").split())
        if len(q) > 42:
            q = q[:39] + "..."
        if q:
            recent.append(q)
    tool_line = "Use mcp__hybrid-search__hybrid_search for recall/context."
    if client == "claude":
        # Tool Search (default-on) defers MCP schemas in tool-heavy setups;
        # without this hint a direct call fails and the agent drifts to Grep.
        tool_line = (
            "Use mcp__hybrid-search__hybrid_search for recall/context"
            ' (if deferred, ToolSearch "select:mcp__hybrid-search__hybrid_search" first).'
        )
    lines = [
        f"[hybrid-search memory] {len(indexes)} past turns available.",
        tool_line,
    ]
    if recent:
        lines.append("Recent: " + " | ".join(recent))
    return "\n".join(lines)


def build_session_context(
    project_root: Path,
    *,
    limit: int = _SESSION_TOPIC_LIMIT,
    client: str = "claude",
) -> str:
    """Build recent-memory context for a session-start hook."""
    try:
        from hybrid_search.memory import reader

        indexes = list(reader.iter_qa_indexes(project_root))
    except Exception:
        return ""
    ctx = _format_session_start_context(indexes[:limit], client=client)

    # Surface the usage scorecard where the user already looks — no extra
    # command to run. format_summary_line is '' when there is no data.
    try:
        from hybrid_search.memory import selfeval

        scoreline = selfeval.format_summary_line(project_root)
    except Exception:
        scoreline = ""
    if scoreline:
        ctx = f"{ctx}\n{scoreline}" if ctx else scoreline
    # Degraded-rate line: how often recent pre-fetches ran BM25-only.
    # Availability was fixed (deadline → fail-open); THIS is the
    # effectiveness number — a high rate means the semantic lane is
    # silently absent and the contention source needs fixing, not the
    # fallback. Silent when nothing degraded.
    try:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent = [
            i for i in indexes
            if i.trigger == "user_prompt_submit"
            and i.timestamp is not None and i.timestamp >= cutoff
        ]
        n_degraded = sum(1 for i in recent if i.degraded)
        if n_degraded:
            line = (
                f"[prefetch 7d] {n_degraded}/{len(recent)} served BM25-only "
                "(vector lane degraded — semantic matching was off for those turns)"
            )
            ctx = f"{ctx}\n{line}" if ctx else line
    except Exception:
        pass
    # The measurement cycle's verdict, where the next session already looks.
    # A loop that has to be asked for its result is a script; this is the
    # half that makes it a loop.
    line = _cycle_line(project_root)
    if line:
        ctx = f"{ctx}\n{line}" if ctx else line
    return ctx[:_MAX_CONTEXT_CHARS]


# How long a measurement may go stale before the session is told to run one.
# A week is roughly 2,500 new dogfood records — past that the last reading
# describes a corpus that no longer exists, and the holdout has refilled
# enough to be worth spending.
_CYCLE_STALE_DAYS = 7


def _cycle_line(project_root: Path) -> str:
    """One line about the last `benchmarks/cycle.py` run. Silent when clean.

    Only in the project that OWNS the runner. The cycle measures the
    dogfood corpus, but the command that re-runs it lives in this tool's
    own repo — telling someone to type it while they are working in the
    measured project is an instruction they cannot follow, in a session
    that is not about the tool at all.

    Reads the record directly rather than importing the runner: benchmarks
    are not shipped in the wheel, and a hook must never depend on them.
    """
    if not (project_root / "benchmarks" / "cycle.py").is_file():
        return ""
    try:
        import json
        from datetime import date

        cdir = Path("~/.hybrid-search/benchmarks/cycle").expanduser()
        runs = sorted(cdir.glob("20*.json"))
        if not runs:
            return ""
        now = json.loads(runs[-1].read_text(encoding="utf-8"))
        prev = (
            json.loads(runs[-2].read_text(encoding="utf-8"))
            if len(runs) > 1 else None
        )
        bits: list[str] = []
        if prev:
            for path, name in (
                ("set_a.answer_in_top3", "Set A top3"),
                ("set_a.answer_found", "Set A found"),
                ("set_b.answer_found", "Set B found"),
                ("code.primary_top5", "코드축 top5"),
            ):
                cur, old = _cycle_dig(now, path), _cycle_dig(prev, path)
                if cur is not None and old is not None and cur < old:
                    bits.append(f"{name} {old}→{cur}")
        pending = sum(
            len(d.get("unlabelled") or [])
            for d in (now.get("displacement") or {}).values()
        )
        if pending:
            bits.append(f"판정 대기 {pending}건")
        try:
            age = (date.today() - date.fromisoformat(now["date"])).days
        except (KeyError, ValueError):
            age = 0
        stale = age >= _CYCLE_STALE_DAYS
        if stale:
            bits.append(f"마지막 측정 {age}일 전")
        if not bits:
            return ""
        line = f"[cycle {now.get('date','?')}] " + " · ".join(bits)
        if stale:
            # Addressed to the agent, not the user. Nobody schedules this:
            # the measurement is a few thousand searches against the same
            # embedding backend the session is using, so a cron would fight
            # the user's own work. The agent knows when the session is idle.
            line += ("\n  → 이 세션에 여유가 생기면 `python benchmarks/cycle.py` 를 "
                     "돌리고 결과를 보고할 것 (20~40분, 백그라운드로).")
        return line
    except Exception:
        return ""


def _cycle_dig(obj: dict, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _router_enabled() -> bool:
    return os.environ.get(_ROUTER_ENV) != "0"


def _clip_one_line(text: str, limit: int) -> str:
    """Collapse to a single line and clip — injected context must stay scannable."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


_FRONTMATTER_RE = __import__("re").compile(r"\A---\s*\n.*?\n---\s*\n", __import__("re").S)
# Leading provenance tags the search layer prepends to snippets. Matched by
# vocabulary rather than by shape: a bare `\A\[...\]` would also eat a code
# snippet that happens to open with an index expression.
_LEADING_TAG_RE = __import__("re").compile(
    r"\A\s*\[(?:in-flight|conversation|qa|card|code|commit|memory)\b[^\]]{0,120}\]\s*",
    __import__("re").IGNORECASE,
)
# One definition, in quality: the card generator needs the same judgement.
from hybrid_search.memory.quality import is_metadata_bullet as _is_meta_bullet


def _clean_body(text: str, path: str = "") -> str:
    """Strip what the reader already knows, so the excerpt is all signal.

    Snippets arrive with the trust tag repeated at the front and, for qa
    notes, a YAML frontmatter block. Both are pure overhead here: the tag is
    already rendered on the heading line, and the frontmatter pushes the
    note's actual body out of a bounded excerpt — which is how a memory hit
    ends up quoting ``timestamp:`` and ``sources_hash:`` instead of the
    answer it was retrieved for.
    """
    body = _LEADING_TAG_RE.sub("", text or "", count=1)
    # In-flight snippets repeat the file path, which the heading already shows.
    if path and body.lstrip().startswith(path):
        body = body.lstrip()[len(path):]
    body = _FRONTMATTER_RE.sub("", body, count=1)
    # Drop the machine-written preamble every qa log carries: the "# Q: …"
    # echo of the query we already matched on, the structural headings, and
    # the metadata bullets (`- **bm25_weight**: 0.15`). None of it answers
    # anything, and inside a bounded excerpt it crowds out what does.
    kept: list[str] = []
    seen_prose = False
    for ln in body.splitlines():
        s = ln.strip()
        if not seen_prose:
            if not s or s.startswith("# Q: ") or s.startswith("#") or _is_meta_bullet(s):
                continue
            seen_prose = True
        kept.append(ln)
    return "\n".join(kept)


def _memory_tag(r) -> str:
    """Compact provenance label for a hit with no file to open."""
    raw = (getattr(r, "trust_meta", "") or "").strip()
    inner = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    parts = [seg.strip() for seg in inner.split(" - ") if seg.strip()]
    if not parts:
        return (getattr(r, "node_type", "") or "memory").strip()
    return _clip_one_line(" · ".join(parts[:3]), 48)


def _render_hit(index: int, r) -> str:
    """One pre-fetch hit, rendered so the agent can act on it without a tool call.

    Memory hits are quoted because there is nothing to open; file hits get a
    ``path:line`` locator plus one line of content so the decision to Read is
    an informed one rather than a coin flip.
    """
    node_type = (getattr(r, "node_type", "") or "").strip()
    virtual = node_type in _VIRTUAL_NODE_TYPES
    tag = _memory_tag(r) if virtual else ""
    # Virtual hits prefer full content: their snippet is a window that often
    # lands inside the note's frontmatter, so the excerpt would quote
    # `timestamp:` and `sources_hash:` instead of the answer. File hits keep
    # the snippet — it is already the matched region.
    raw_body = (
        (getattr(r, "content", "") or getattr(r, "snippet", "") or "")
        if virtual
        else (getattr(r, "snippet", "") or getattr(r, "content", "") or "")
    )
    body = _clip_one_line(
        _clean_body(raw_body, "" if virtual else (getattr(r, "file_path", "") or "")),
        _MEMORY_EXCERPT_CHARS if virtual else _CODE_SNIPPET_CHARS,
    )
    if virtual:
        head = f"{index}. [{tag}] quoted — no file to open"
    else:
        fp = getattr(r, "file_path", "?") or "?"
        start = getattr(r, "start_line", None)
        name = getattr(r, "name", None)
        label = f" ({node_type} {name})" if node_type and name else ""
        head = f"{index}. `{fp}{f':{start}' if start else ''}`{label}"
    return f"{head}\n   {body}" if body else head


def _format_user_prompt_context(response, prompt: str | None = None) -> str:
    results = getattr(response, "results", []) or []
    if not results:
        return ""
    confidence = getattr(response, "confidence", "weak") or "weak"
    hint = getattr(response, "fallback_hint", None)
    header = f"[hybrid-search pre-fetch] {len(results)} hits · confidence {confidence}"
    if confidence == "weak" and hint:
        header += f" · {hint}"

    head = []
    if prompt is not None and _router_enabled():
        from hybrid_search.memory.router import classify_prompt

        decision = classify_prompt(prompt)
        head.append(f"[hybrid-search route] suggest {decision.tool} · {decision.reason}")
    head.append(header)
    footer = (
        "Quoted hits above are already the answer — do not Read those paths. "
        "For file hits, Read the locator; call hybrid_search only if these miss."
    )

    hits = [_render_hit(i, r) for i, r in enumerate(results[:_PREFETCH_RESULT_LIMIT], start=1)]
    # Trim from the tail: the lowest-ranked hits are the cheapest to lose, and
    # rebuilding each time keeps the closing instruction intact — a footer
    # truncated mid-sentence would read as an instruction to Read everything.
    while hits:
        context = "\n".join(head + hits + [footer])
        if len(context) <= _PREFETCH_MAX_CONTEXT_CHARS or len(hits) == 1:
            return context[:_PREFETCH_MAX_CONTEXT_CHARS]
        hits.pop()
    return "\n".join(head + [footer])[:_PREFETCH_MAX_CONTEXT_CHARS]


# Embed budget for the BLOCKING pre-fetch. The hook's external timeout
# (10s) discards the WHOLE context on expiry, so the slowest dependency —
# a query embed queued behind a bulk batch on the shared Ollama — must
# be bounded well inside it. On expiry the embedder raises and the
# orchestrator's fail-open serves BM25-only: degraded beats discarded.
# Overridable; "0" disables the pre-fetch deadline entirely.
_PREFETCH_EMBED_DEADLINE_ENV = "HYBRID_SEARCH_EMBED_DEADLINE"
_PREFETCH_EMBED_DEADLINE_DEFAULT = "2.5"


def _run_programmatic_search(prompt: str, cwd: str):
    try:
        from hybrid_search.config import load_config
        from hybrid_search.index.embedder import Embedder
        from hybrid_search.project import ProjectRegistry
        from hybrid_search.search.orchestrator import SearchOrchestrator
    except Exception:
        return None

    # Scoped to THIS search only (set/restore): a process-wide default
    # would leak into the Stop hook's detached conversation indexing,
    # whose bulk embeds legitimately take longer than any hook budget.
    prev = os.environ.get(_PREFETCH_EMBED_DEADLINE_ENV)
    if prev is None:
        os.environ[_PREFETCH_EMBED_DEADLINE_ENV] = _PREFETCH_EMBED_DEADLINE_DEFAULT
    elif prev == "0":
        os.environ.pop(_PREFETCH_EMBED_DEADLINE_ENV, None)
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
    finally:
        if prev is None:
            os.environ.pop(_PREFETCH_EMBED_DEADLINE_ENV, None)
        else:
            os.environ[_PREFETCH_EMBED_DEADLINE_ENV] = prev


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
