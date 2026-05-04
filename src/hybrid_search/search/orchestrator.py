"""Search orchestrator — query classification + BM25/Vector coordination + RRF fusion.

Implements §11 query classification and cross-project search (§13).
"""

from __future__ import annotations

import json
import re
import time
import logging
from dataclasses import dataclass, field, replace as _dc_replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.config import Config
from hybrid_search.index.embedder import Embedder
from hybrid_search.memory.router import (
    classify_confidence,
    fallback_hint,
    has_identifier_shape_token,
)
from hybrid_search.project import ProjectRegistry, ProjectInfo
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.fusion import FusedResult, reciprocal_rank_fusion
from hybrid_search.search.modules_search import search_modules
from hybrid_search.search.snippet import make_snippet
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import ModuleRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger(__name__)

# Cross-project search timeout per project (§13)
PROJECT_TIMEOUT_S = 2.0

# Step K: how many modules to pull member files from (wider than
# module-card slots). 8 empirically covers the valuein_homepage
# structure cases where the rep-path winner and the SQL-migration
# holder are two different admissions modules at search ranks 3 and 4.
_MEMBER_SOURCE_MODULES = 8
# Non-card module dedup by name prefers the file-count-richest
# variant so member emission picks the structural canonical members
# (``components/tuition-sessions/*.tsx``) over the shallow dashboard
# page variants of the same module name. Set in ``_module_results_for_query``.
# How many member files to emit per non-card source module. Card
# modules already contribute their rep file; their sibling members
# usually share a directory with the rep and don't add new recall
# signal. Non-card source modules (the admissions#2 module on
# valuein_homepage, search rank 4, that holds the S5 SQL migration)
# are the ones whose content otherwise wouldn't reach top-10, so
# those are what the member stream exists for.
#
# Emit 2 per non-card: the first is the module's top code member
# (covers the dominant directory), the second catches off-tree
# files like bucketed migrations that Step G attached. For the
# admissions#2 case: member 1 is the ``admission_results.sql``
# migration (Step G cross-tree attach); member 2 is the
# ``page.tsx`` under ``app/(dashboard)/students/admissions/``.
_MEMBER_EMIT_NONCARD = 2


class QueryType:
    EXACT_SYMBOL = "EXACT_SYMBOL"
    KOREAN_NL = "KOREAN_NL"
    ENGLISH_NL = "ENGLISH_NL"


# Default weights per query type (§11)
QUERY_WEIGHTS: dict[str, float] = {
    QueryType.EXACT_SYMBOL: 0.8,  # BM25 weight
    QueryType.KOREAN_NL: 0.15,
    QueryType.ENGLISH_NL: 0.4,
}

# Regex for code identifiers: camelCase, snake_case, SCREAMING_SNAKE, dot-qualified
_SYMBOL_RE = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*(?:[A-Z][a-z]+)+[a-zA-Z0-9_]*$"   # camelCase: signIn, createUser
    r"|^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$"                          # snake_case: tuition_fees
    r"|^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$"                          # SCREAMING_SNAKE: MAX_RETRIES
    r"|^[a-zA-Z_]\w*\.[a-zA-Z_]\w*$"                             # dot-qualified: AuthService.signIn
)

# Korean character range
_KOREAN_RE = re.compile(r"[\uac00-\ud7a3]")


def classify_query(query: str) -> str:
    """3-stage query classifier (§11)."""
    stripped = query.strip()

    # Stage 1: EXACT_SYMBOL — camelCase or snake_case pattern
    # Check each word; if any word is a symbol, it's EXACT_SYMBOL
    words = stripped.split()
    if any(_SYMBOL_RE.match(w) for w in words):
        # If also has Korean, it's a mixed query → use middle weights
        if _KOREAN_RE.search(stripped):
            return QueryType.KOREAN_NL  # Mixed: treat as Korean-leaning
        return QueryType.EXACT_SYMBOL

    # Stage 2: KOREAN_NL — >50% Korean characters
    total_alpha = sum(1 for c in stripped if c.isalpha())
    korean_count = len(_KOREAN_RE.findall(stripped))
    if total_alpha > 0 and korean_count / total_alpha > 0.5:
        return QueryType.KOREAN_NL

    # Stage 3: ENGLISH_NL
    return QueryType.ENGLISH_NL


def get_bm25_weight(query: str, explicit_weight: float | None = None) -> tuple[float, str]:
    """Determine BM25 weight from query classification or explicit override."""
    if explicit_weight is not None:
        qtype = classify_query(query)
        return explicit_weight, qtype

    qtype = classify_query(query)

    # Mixed query (Korean + symbol) → middle weight
    words = query.strip().split()
    has_symbol = any(_SYMBOL_RE.match(w) for w in words)
    has_korean = bool(_KOREAN_RE.search(query))

    if has_symbol and has_korean:
        return 0.4, qtype  # Middle of EXACT_SYMBOL(0.8) and KOREAN_NL(0.15)

    return QUERY_WEIGHTS[qtype], qtype


@dataclass
class HybridResult:
    chunk_id: str
    rrf_score: float
    bm25_rank: int | None
    vector_rank: int | None
    file_path: str
    project: str
    name: str | None
    qualified_name: str | None
    node_type: str | None
    start_line: int | None
    end_line: int | None
    content: str | None
    snippet: str
    module_id: str | None = None
    # File mtime in ISO 8601 — only populated for qa_log chunks where
    # the Memory-Layer boost needs age information. Everything else
    # leaves this None and pays no attention to it.
    file_mtime: str | None = None
    trust_meta: str | None = None


@dataclass
class HybridSearchResponse:
    results: list[HybridResult]
    query_type: str
    effective_bm25_weight: float
    query_time_ms: float
    total_chunks_searched: int
    top_score: float = 0.0
    score_gap: float | None = None
    confidence: str = "weak"
    fallback_hint: str | None = None
    skipped_projects: list[str] = field(default_factory=list)
    reranked: bool = False


# --- Memory Layer boost ------------------------------------------------
# Half-life in days for the recency decay applied to qa_log chunks.
# At 30d a past Q&A keeps half its boost; at 90d it's at 12.5%. Picked
# to roughly match the tempo of a feature branch — answers from within
# the current sprint stay relevant, older ones fade.
_MEMORY_HALF_LIFE_DAYS = 30.0

# Multiplier applied on top of rrf_score for qa_log chunks when the
# query is a plain topical search (no memory-intent signal). Small — a
# fresh Q&A should gently lift above a comparable chunk, not dominate
# actual code or docs.
_MEMORY_AMBIENT_BOOST = 0.20

# Memory cards are curated, compact semantic memory. They should outrank
# raw qa logs for memory-shaped queries without requiring as much recency
# pressure.
_MEMORY_CARD_AMBIENT_BOOST = 0.35
_MEMORY_CARD_INTENT_BOOST = 1.25
_DOMAIN_TERM_AMBIENT_BOOST = 0.55
_DOMAIN_TERM_INTENT_BOOST = 1.40

# Multiplier when the query explicitly recalls ("지난번에", "previously").
# Strong — the user is asking for the past exchange itself, so a close
# qa_log match should outrank most code chunks.
_MEMORY_INTENT_BOOST = 1.00

# Korean/English triggers for "I'm recalling something I asked before".
# Prefix match works because these phrases are typically sentence-
# initial or standalone. Korean particles are covered by substring
# match since we check with ``in`` rather than regex word boundary.
_MEMORY_INTENT_KO = (
    "지난번", "이전에", "아까", "방금", "전에", "저번에", "그때",
    "뭐였지", "했지", "결정했지", "정했지", "기억", "메모리",
)
_MEMORY_INTENT_EN_RE = re.compile(
    r"\b(previously|earlier|before|last\s+time|the\s+other\s+day|what\s+did\s+(?:i|we|you)\s+(?:ask|say))\b",
    re.IGNORECASE,
)


def _has_memory_intent(query: str) -> bool:
    """True when the query asks for a past exchange.

    Memory-intent queries skip the ambient 20% boost and pay the full
    100% boost so that a recent close-matching qa_log rises above code
    chunks. False positives are survivable (qa_log still needs to
    token-match for the boost to apply at all).
    """
    q = (query or "").strip()
    if not q:
        return False
    if any(tok in q for tok in _MEMORY_INTENT_KO):
        return True
    return bool(_MEMORY_INTENT_EN_RE.search(q))


def _parse_mtime_days_ago(mtime: str | None, now: datetime | None = None) -> float | None:
    """Return age in days from an ISO-formatted mtime; None when unparseable."""
    if not mtime:
        return None
    try:
        dt = datetime.fromisoformat(mtime)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    delta = (now - dt).total_seconds() / 86400.0
    return max(0.0, delta)


def _frontmatter_value(content: str | None, key: str) -> str | None:
    if not content:
        return None
    text = content.lstrip()
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    for line in text[4:end].splitlines():
        raw_key, sep, raw_value = line.partition(":")
        if sep and raw_key.strip() == key:
            return raw_value.strip().strip('"')
    return None


def _memory_status(result: HybridResult) -> str:
    return (_frontmatter_value(result.content, "status") or "active").lower()


def _trust_meta(
    *,
    node_type: str | None,
    trigger: str | None,
    confidence: str | None,
    status: str | None,
    mtime: str | None,
    content: str | None,
) -> str:
    kind = {
        "memory_card": "card",
        "domain_term": "domain_term",
        "episodic_example": "example",
        "qa_log": "qa",
    }.get(node_type or "", "code")
    parts = [kind]
    if confidence and kind in {"card", "domain_term", "example"}:
        parts.append(f"confidence={confidence}")
    if status and status != "active":
        parts.append(status)
    if trigger and kind == "qa":
        parts.append(trigger)
    age = _parse_mtime_days_ago(mtime)
    if age is not None:
        if age < 1:
            parts.append("today")
        else:
            parts.append(f"{int(age)}d ago")
    elif kind == "qa" and "## Answer excerpt" not in (content or ""):
        parts.append("metadata-only")
    elif kind == "code":
        parts.append("indexed")
    return "[" + " - ".join(parts) + "]"


_MEMORY_NODE_TYPES = {"qa_log", "memory_card", "domain_term", "episodic_example"}


def _apply_memory_boost(
    results: list[HybridResult],
    memory_intent: bool,
    now: datetime | None = None,
) -> list[HybridResult]:
    """Re-rank ``results`` with a half-life decay on memory chunks.

    For qa_log / memory_card / domain_term chunks:
        new_score = rrf_score * (1 + boost * 2^(-age_days / half_life))
    Other node types pass through unchanged. Results are re-sorted by
    the adjusted score. No-op when no qa_log chunks are present.
    """
    if not results:
        return results
    has_memory = any(r.node_type in _MEMORY_NODE_TYPES for r in results)
    if not has_memory:
        return results
    now = now or datetime.now(timezone.utc)
    adjusted: list[HybridResult] = []
    for r in results:
        if r.node_type not in _MEMORY_NODE_TYPES:
            adjusted.append(r)
            continue
        if _memory_status(r) in {"superseded", "archived"}:
            adjusted.append(_dc_replace(r, rrf_score=round(r.rrf_score * 0.35, 6)))
            continue
        if r.node_type == "domain_term":
            boost = _DOMAIN_TERM_INTENT_BOOST if memory_intent else _DOMAIN_TERM_AMBIENT_BOOST
        elif r.node_type == "memory_card":
            boost = _MEMORY_CARD_INTENT_BOOST if memory_intent else _MEMORY_CARD_AMBIENT_BOOST
        else:
            boost = _MEMORY_INTENT_BOOST if memory_intent else _MEMORY_AMBIENT_BOOST
        age = _parse_mtime_days_ago(r.file_mtime, now)
        if age is None:
            # No mtime → treat as fresh (age=0) so newly-written Q&A
            # still benefits from the boost before its file_mtime is
            # indexed. Conservative side: we don't penalise.
            age = 0.0
        decay = 0.5 ** (age / _MEMORY_HALF_LIFE_DAYS)
        new_score = r.rrf_score * (1.0 + boost * decay)
        adjusted.append(_dc_replace(r, rrf_score=round(new_score, 6)))
    adjusted.sort(key=lambda r: -r.rrf_score)
    return adjusted


def _merge_memory_results(
    chunk_results: list[HybridResult],
    memory_results: list[HybridResult],
    limit: int,
) -> list[HybridResult]:
    """Promote explicit memory-lane hits without duplicating chunk IDs.

    The normal RRF lane can bury memory cards because broad docs/modules
    match the same topical words. For explicit recall queries, curated
    memory cards are the primary answer unit, so we splice a small memory
    head before the regular chunk stream and preserve all other ordering.
    """
    if not memory_results:
        return chunk_results
    memory_head = [r for r in memory_results if r.node_type in _MEMORY_NODE_TYPES]
    if not memory_head:
        return chunk_results
    memory_head.sort(key=lambda r: (
        {"domain_term": 0, "memory_card": 1, "episodic_example": 2, "qa_log": 3}.get(r.node_type or "", 9),
        -r.rrf_score,
    ))
    head_limit = min(3, max(1, limit))
    merged: list[HybridResult] = []
    seen: set[str] = set()
    for r in memory_head[:head_limit]:
        if r.chunk_id not in seen:
            merged.append(r)
            seen.add(r.chunk_id)
    for r in chunk_results:
        if r.chunk_id not in seen:
            merged.append(r)
            seen.add(r.chunk_id)
    return merged


def _has_quality_anchor(
    query: str,
    results: list[HybridResult],
    top_score: float,
    thresholds: dict[str, float],
) -> bool:
    """Detect strong returned anchors that raw RRF gap alone underrates.

    Module and memory cards are injected after chunk fusion, so they often
    create small raw score gaps even when the returned answer unit is exactly
    the desired subsystem/context. Exact identifier prompts have the inverse
    issue: their top hit can be right despite a near-tie among sibling chunks.
    This keeps the public ranking unchanged and only avoids over-eager weak
    confidence metadata.
    """
    if not results:
        return False
    weak_score = thresholds.get("weak_score", 0.0)
    anchor_types = {"module_card", "memory_card", "domain_term", "episodic_example"}
    if top_score >= weak_score * 0.90:
        if any((r.node_type or "") in anchor_types for r in results[:3]):
            return True
    if top_score >= weak_score * 0.94 and has_identifier_shape_token(query):
        return True
    return False


class SearchOrchestrator:
    """Coordinates BM25 + Vector search with RRF fusion."""

    def __init__(self, config: Config, registry: ProjectRegistry, embedder: Embedder) -> None:
        self._config = config
        self._registry = registry
        self._embedder = embedder

    def hybrid_search(
        self,
        query: str,
        project: str | None = None,
        limit: int = 10,
        file_pattern: str | None = None,
        node_types: list[str] | None = None,
        bm25_weight: float | None = None,
        cwd: str | None = None,
        exclude_pattern: str | None = None,
    ) -> HybridSearchResponse:
        """Execute hybrid BM25 + Vector search with RRF fusion."""
        start = time.monotonic()

        # Determine weights
        effective_weight, qtype = get_bm25_weight(query, bm25_weight)

        # Resolve projects
        if project:
            info = self._registry.get_by_name(project)
            if info is None:
                return self._make_response(
                    query=query,
                    results=[], query_type=qtype, effective_bm25_weight=effective_weight,
                    query_time_ms=0, total_chunks_searched=0,
                )
            project_infos = [info]
        else:
            project_infos = self._registry.list_all()

        # Auto-detect project from cwd — scope to that project only
        primary_project_id: str | None = None
        if cwd and not project:
            detected_id = self._detect_primary_project(cwd, project_infos)
            if detected_id:
                project_infos = [p for p in project_infos if p.id == detected_id]
                primary_project_id = detected_id

        if not project_infos:
            return self._make_response(
                query=query,
                results=[], query_type=qtype, effective_bm25_weight=effective_weight,
                query_time_ms=0, total_chunks_searched=0,
            )

        # Embed query once
        query_vector = self._embedder.embed_query(query)
        retrieval_depth = limit * 3

        memory_intent = _has_memory_intent(query)

        # Search each project
        if len(project_infos) == 1:
            bm25_ids, vector_ids, total, skipped, authority_scores = self._search_single(
                project_infos[0], query, query_vector, retrieval_depth,
                file_pattern, node_types, exclude_pattern,
            )
        else:
            bm25_ids, vector_ids, total, skipped, authority_scores = self._search_cross_project(
                project_infos, query, query_vector, retrieval_depth,
                file_pattern, node_types,
                primary_project_id=primary_project_id,
                exclude_pattern=exclude_pattern,
            )

        # RRF fusion — numeric confidence scores nudge chunks with strong
        # incoming call edges ahead (M1). Absent chunks stay neutral.
        # M1.2: EXACT_SYMBOL queries bypass authority — exact-match lookup
        # (e.g. FusedResult, compute_file_hash) is hurt by boost that promotes
        # well-connected call-sites ahead of the definition itself.
        effective_authority = (
            None if qtype == QueryType.EXACT_SYMBOL else (authority_scores or None)
        )
        fused = reciprocal_rank_fusion(
            bm25_ids, vector_ids,
            k=self._config.search.rrf_k,
            bm25_weight=effective_weight,
            chunk_authority_scores=effective_authority,
            authority_alpha=self._config.search.authority_alpha,
        )

        # When reranking is enabled, return more candidates for Claude Code to rerank
        reranking_cfg = self._config.search.reranking
        effective_limit = reranking_cfg.max_candidates if reranking_cfg.enabled else limit

        # Enrich chunk results with metadata
        chunk_results = self._enrich_results(fused[:effective_limit], project_infos, query)

        memory_results: list[HybridResult] = []
        if memory_intent and node_types is None:
            memory_depth = max(retrieval_depth, 50)
            memory_node_types = ["domain_term", "memory_card", "episodic_example", "qa_log"]
            if len(project_infos) == 1:
                mem_bm25_ids, mem_vector_ids, _, _, _ = self._search_single(
                    project_infos[0], query, query_vector, memory_depth,
                    file_pattern, memory_node_types, exclude_pattern,
                )
            else:
                mem_bm25_ids, mem_vector_ids, _, _, _ = self._search_cross_project(
                    project_infos, query, query_vector, memory_depth,
                    file_pattern, memory_node_types,
                    primary_project_id=primary_project_id,
                    exclude_pattern=exclude_pattern,
                )
            mem_fused = reciprocal_rank_fusion(
                mem_bm25_ids, mem_vector_ids,
                k=self._config.search.rrf_k,
                bm25_weight=effective_weight,
            )
            memory_results = self._enrich_results(mem_fused[:max(100, limit)], project_infos, query)

        # Memory Layer — time-decay & intent boost for qa_log chunks.
        # A past Q&A should surface in future searches (that's the whole
        # compounding-quality point), but its weight must fade with age
        # (stale answers drift from current code) and amplify when the
        # user is explicitly recalling ("지난번에 뭐라고 했지"). The boost
        # runs on the already-enriched list so file_mtime is available
        # to compute age; the list is re-sorted by the adjusted score.
        chunk_results = _apply_memory_boost(chunk_results, memory_intent)
        if memory_results:
            memory_results = _apply_memory_boost(memory_results, memory_intent)
            chunk_results = _merge_memory_results(chunk_results, memory_results, limit)

        # Phase 5: inject module cards when the query is likely structural.
        # Module cards give agents a subsystem-level answer unit so they don't
        # have to Read 5 files to piece together "how is X organized". Step K
        # additionally surfaces ``module_member`` entries — sibling files under
        # the same module that the query-aware rep pick didn't land on — so
        # structure queries whose answer spans several files in one subsystem
        # (F2's admissions module, S5's entrance-tests) get multiple recall
        # hits from a single module retrieval.
        if memory_intent:
            module_cards, module_members = [], []
            module_slots = 0
        else:
            module_cards, module_members = self._module_results_for_query(
                qtype, query, project_infos, query_vector
            )
            module_slots = _module_slots_for(qtype, query)
        results = _interleave_modules(
            chunk_results, module_cards, module_slots, limit,
            members=module_members,
        )

        elapsed_ms = (time.monotonic() - start) * 1000
        return self._make_response(
            query=query,
            results=results,
            query_type=qtype,
            effective_bm25_weight=effective_weight,
            query_time_ms=round(elapsed_ms, 1),
            total_chunks_searched=total,
            skipped_projects=skipped,
            reranked=reranking_cfg.enabled,
        )

    def _make_response(
        self,
        *,
        query: str,
        results: list[HybridResult],
        query_type: str,
        effective_bm25_weight: float,
        query_time_ms: float,
        total_chunks_searched: int,
        skipped_projects: list[str] | None = None,
        reranked: bool = False,
    ) -> HybridSearchResponse:
        ranked_scores = sorted(
            (r.rrf_score for r in results if r.rrf_score > 0),
            reverse=True,
        )
        top_score = ranked_scores[0] if ranked_scores else 0.0
        score_gap = (
            round(ranked_scores[0] - ranked_scores[1], 6)
            if len(ranked_scores) >= 2
            else None
        )
        thresholds = self._config.router.confidence.as_dict()
        confidence = classify_confidence(top_score, score_gap, thresholds)
        if confidence == "weak" and _has_quality_anchor(query, results, top_score, thresholds):
            confidence = "mixed"
        hint = fallback_hint(query) if confidence == "weak" else None
        return HybridSearchResponse(
            results=results,
            query_type=query_type,
            effective_bm25_weight=effective_bm25_weight,
            query_time_ms=query_time_ms,
            total_chunks_searched=total_chunks_searched,
            top_score=top_score,
            score_gap=score_gap,
            confidence=confidence,
            fallback_hint=hint,
            skipped_projects=skipped_projects or [],
            reranked=reranked,
        )

    def _module_results_for_query(
        self,
        qtype: str,
        query: str,
        project_infos: list[ProjectInfo],
        query_vector=None,
    ) -> tuple[list[HybridResult], list[HybridResult]]:
        """Score modules; return ``(module_cards, module_members)``.

        ``query_vector`` is the already-embedded query from the chunk search
        path — passing it through lets Step C's semantic fusion fire without
        spending another API call.

        Step K: in addition to one card per surfaced module, emit up to
        ``_MEMBER_EMIT_PER_MODULE`` sibling files from the top
        ``_MEMBER_SOURCE_MODULES`` candidates as ``module_member`` entries.
        A structure query whose gold covers three files in one subsystem
        (S5 admissions — components dir + SQL migration + plan doc) can
        now collect all three from one module retrieval, instead of one
        rep path per module. The member-sourcing window is wider than the
        card slot count so a near-miss module (admissions #2 at search
        rank 4 on valuein_homepage, the one that actually holds the SQL)
        still contributes even when its card doesn't make the top-3 slot.
        """
        if _module_slots_for(qtype, query) == 0:
            return [], []

        per_project_limit = _MEMBER_SOURCE_MODULES
        hits: list[tuple[ProjectInfo, ModuleRecord, float]] = []
        for pinfo in project_infos:
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            if not idx_paths.store_db.exists():
                continue
            db = StoreDB(idx_paths.store_db)
            try:
                scored = search_modules(
                    db, pinfo.id, query,
                    limit=per_project_limit,
                    query_vector=query_vector,
                )
                for m, s in scored:
                    hits.append((pinfo, m, s))
            finally:
                db.close()

        hits.sort(key=lambda x: -x[2])
        # Step J: derive query tokens per-project so each module's rep
        # path is picked to align with the query — SQL migration wins
        # over API route when the query says "monthly stats", not vice
        # versa. The specificity gate (Step H) runs over each project's
        # own catalog so generic-noun aliases don't pull member files
        # with tangential matches (learned from F1: query 학생이 was
        # dragging student-analysis.md to rep when the real module is
        # homework-analysis).
        from hybrid_search.search.modules_search import compute_alias_specificity
        query_tokens_by_project: dict[str, set[str]] = {}
        for pinfo in project_infos:
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            if not idx_paths.store_db.exists():
                continue
            db = StoreDB(idx_paths.store_db)
            try:
                spec = compute_alias_specificity(db.get_modules(pinfo.id))
            finally:
                db.close()
            query_tokens_by_project[pinfo.id] = _derive_query_tokens(
                query, alias_specificity=spec,
            )
        # Two-pass emit: first fill ``card_slot_budget`` cards with
        # modules that offer *distinct* rep paths (otherwise the second
        # card duplicates the first and L5's "half chunks, half modules"
        # guarantee burns a slot on a dup file). Modules whose rep
        # collides with an earlier card — or that run past the budget —
        # fall through to the member stream instead. This is how S4
        # keeps its ``components/remote-room/*`` evidence: the
        # ``remote-room`` module's query-aware rep collapses to the
        # same ``docs/features/learning-remote-room.md`` already used
        # by ``remote-rooms``; dedup pushes it to member emission,
        # where its top code member surfaces the components dir.
        card_slot_budget = _module_slots_for(qtype, query)

        cards: list[HybridResult] = []
        members: list[HybridResult] = []
        used_rep_paths: set[str] = set()
        # Hold non-card modules for a second pass that emits members.
        member_sources: list[tuple[ProjectInfo, ModuleRecord, str]] = []

        for idx, (pinfo, m, _score) in enumerate(hits):
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            db = StoreDB(idx_paths.store_db)
            try:
                qtoks = query_tokens_by_project.get(pinfo.id, set())
                rep_path = _module_representative_path(db, m, qtoks)
                slot_available = len(cards) < card_slot_budget
                if slot_available and rep_path not in used_rep_paths:
                    used_rep_paths.add(rep_path)
                    cards.append(HybridResult(
                        chunk_id=f"module:{m.id}",
                        rrf_score=0.0,
                        bm25_rank=None,
                        vector_rank=None,
                        file_path=rep_path,
                        project=pinfo.name,
                        name=m.name,
                        qualified_name=f"module:{m.name}",
                        node_type="module",
                        start_line=None,
                        end_line=None,
                        content=_module_content_for_result(m),
                        snippet=_module_snippet_for_result(m),
                        module_id=m.id,
                    ))
                else:
                    member_sources.append((pinfo, m, rep_path))
            finally:
                db.close()

        # Non-card name-dedup: many projects carry several modules with
        # the same name but different file sets (valuein_homepage has
        # two ``tuition-sessions`` modules — a 4-file one keyed on the
        # dashboard page, and a 13-file one keyed on the
        # ``components/tuition-sessions/*`` directory). Keep the
        # file-count-richest variant per name so member emission picks
        # the structurally canonical files rather than dashboard
        # page.tsx files that agents rarely want as subsystem
        # evidence. Score ordering is preserved across names; dedup
        # only fires within a name.
        dedup_by_name: dict[str, tuple[ProjectInfo, ModuleRecord, str, int]] = {}
        for pinfo, m, rep in member_sources:
            name_key = (m.name or "").lower()
            # Need file count to compare variants; query it once per
            # candidate and close the db immediately.
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            db = StoreDB(idx_paths.store_db)
            try:
                count = len(db.get_files_by_module(m.id))
            finally:
                db.close()
            prev = dedup_by_name.get(name_key)
            if prev is None or count > prev[3]:
                dedup_by_name[name_key] = (pinfo, m, rep, count)
        # Preserve original score order: walk ``member_sources`` and
        # emit each retained candidate once.
        deduped_sources = []
        kept_ids: set[str] = set()
        for pinfo, m, rep in member_sources:
            name_key = (m.name or "").lower()
            kept = dedup_by_name.get(name_key)
            if kept is None:
                continue
            if kept[1].id != m.id:
                continue
            if m.id in kept_ids:
                continue
            kept_ids.add(m.id)
            deduped_sources.append((pinfo, m, rep))

        # Second pass: emit members only for non-card source modules.
        # Card modules skip member emission because their sibling files
        # typically share a directory with the rep and don't add recall
        # for ``expected_files`` style dir-prefix gold entries.
        for pinfo, m, _skip_rep in deduped_sources:
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            db = StoreDB(idx_paths.store_db)
            try:
                qtoks = query_tokens_by_project.get(pinfo.id, set())
                member_paths = _module_member_paths(
                    db, m, qtoks,
                    max_members=_MEMBER_EMIT_NONCARD,
                )
                for member_path in member_paths:
                    members.append(HybridResult(
                        chunk_id=f"member:{m.id}:{member_path}",
                        rrf_score=0.0,
                        bm25_rank=None,
                        vector_rank=None,
                        file_path=member_path,
                        project=pinfo.name,
                        name=m.name,
                        qualified_name=f"module:{m.name}",
                        node_type="module_member",
                        start_line=None,
                        end_line=None,
                        content=_module_content_for_result(m),
                        snippet=_module_snippet_for_result(m),
                        module_id=m.id,
                    ))
            finally:
                db.close()

        return cards, members

    def _search_single(
        self,
        pinfo: ProjectInfo,
        query: str,
        query_vector,
        depth: int,
        file_pattern: str | None,
        node_types: list[str] | None,
        exclude_pattern: str | None = None,
    ) -> tuple[list[str], list[str], int, list[str], dict[str, float]]:
        """Search a single project, return (bm25_ids, vector_ids, total, skipped, authority)."""
        project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
        idx_paths = IndexPaths(project_dir)

        if not idx_paths.store_db.exists():
            return [], [], 0, [], {}

        db = StoreDB(idx_paths.store_db)
        bm25_eng = BM25Engine(idx_paths.tantivy_dir, read_only=True)
        vec_eng = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)

        try:
            chunk_filter = _build_filter(
                db, pinfo.id, file_pattern, node_types, exclude_pattern,
            )

            # BM25 applies metadata filters after the Tantivy query. When the
            # filtered corpus is tiny (for example memory_card), a normal
            # top-K can contain zero eligible chunks even though matches exist.
            bm25_limit = max(depth, 1000) if chunk_filter else depth
            bm25_results = bm25_eng.search(query, limit=bm25_limit)
            bm25_ids = [r.chunk_id for r in bm25_results]
            if chunk_filter:
                bm25_ids = [cid for cid in bm25_ids if cid in chunk_filter]

            # Vector search
            vec_results = vec_eng.search(query_vector, limit=depth, chunk_ids_filter=chunk_filter)
            vector_ids = [r.chunk_id for r in vec_results]

            total = vec_eng.count
            authority = db.get_chunk_authority_scores(pinfo.id)
        finally:
            db.close()

        return bm25_ids, vector_ids, total, [], authority

    @staticmethod
    def _detect_primary_project(
        cwd: str, project_infos: list[ProjectInfo]
    ) -> str | None:
        """Find the registered project whose path contains the cwd (or vice versa)."""
        cwd_path = Path(cwd).resolve()
        for pinfo in project_infos:
            project_path = Path(pinfo.path).resolve()
            try:
                cwd_path.relative_to(project_path)
                return pinfo.id
            except ValueError:
                pass
            try:
                project_path.relative_to(cwd_path)
                return pinfo.id
            except ValueError:
                pass
        return None

    def _search_cross_project(
        self,
        project_infos: list[ProjectInfo],
        query: str,
        query_vector,
        depth: int,
        file_pattern: str | None,
        node_types: list[str] | None,
        primary_project_id: str | None = None,
        exclude_pattern: str | None = None,
    ) -> tuple[list[str], list[str], int, list[str], dict[str, float]]:
        """Cross-project search: interleave BM25 ranks, merge vector by cosine (§13).

        When primary_project_id is set (from cwd detection), primary project
        gets priority in BM25 interleave and a cosine boost in vector results.
        """
        per_project_bm25: list[list[str]] = []
        primary_bm25: list[str] | None = None
        all_vector: list[tuple[str, float]] = []  # (chunk_id, similarity)
        merged_authority: dict[str, float] = {}
        total = 0
        skipped: list[str] = []

        def search_one(pinfo: ProjectInfo):
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            if not idx_paths.store_db.exists():
                return None, None, 0, {}

            db = StoreDB(idx_paths.store_db)
            bm25_eng = BM25Engine(idx_paths.tantivy_dir, read_only=True)
            vec_eng = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)

            try:
                chunk_filter = _build_filter(
                    db, pinfo.id, file_pattern, node_types, exclude_pattern,
                )
                bm25_limit = max(depth, 1000) if chunk_filter else depth
                bm25_res = bm25_eng.search(query, limit=bm25_limit)
                bm25_ids = [r.chunk_id for r in bm25_res]
                if chunk_filter:
                    bm25_ids = [cid for cid in bm25_ids if cid in chunk_filter]

                vec_res = vec_eng.search(query_vector, limit=depth, chunk_ids_filter=chunk_filter)
                vec_pairs = [(r.chunk_id, r.score) for r in vec_res]
                count = vec_eng.count
                authority = db.get_chunk_authority_scores(pinfo.id)
            finally:
                db.close()

            return bm25_ids, vec_pairs, count, authority

        # Execute with timeout
        primary_chunk_ids: set[str] = set()
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(search_one, p): p for p in project_infos}
            for future in futures:
                pinfo = futures[future]
                try:
                    bm25_ids, vec_pairs, count, authority = future.result(timeout=PROJECT_TIMEOUT_S)
                    if bm25_ids is not None:
                        if primary_project_id and pinfo.id == primary_project_id:
                            primary_bm25 = bm25_ids
                            primary_chunk_ids.update(bm25_ids)
                            primary_chunk_ids.update(cid for cid, _ in vec_pairs)
                        else:
                            per_project_bm25.append(bm25_ids)
                        all_vector.extend(vec_pairs)
                        total += count
                        # chunk IDs are UUIDs (globally unique across projects)
                        merged_authority.update(authority)
                except (FutureTimeoutError, Exception) as e:
                    logger.warning("Project %s timed out or failed: %s", pinfo.name, e)
                    skipped.append(pinfo.name)

        # BM25: primary-first interleave
        if primary_bm25 is not None:
            # Put primary project first, then round-robin the rest
            secondary_bm25 = _interleave_round_robin(per_project_bm25)
            merged_bm25 = _weighted_interleave(primary_bm25, secondary_bm25, primary_ratio=2)
            logger.info("CWD boost: primary project gets 2:1 BM25 interleave priority")
        else:
            merged_bm25 = _interleave_round_robin(per_project_bm25)

        # Vector: sort by cosine similarity, with boost for primary project
        if primary_project_id and primary_chunk_ids:
            # Boost primary project results by 5% cosine similarity
            CWD_BOOST = 0.05
            all_vector = [
                (cid, sim + CWD_BOOST) if cid in primary_chunk_ids else (cid, sim)
                for cid, sim in all_vector
            ]
        all_vector.sort(key=lambda x: x[1], reverse=True)
        merged_vector = [cid for cid, _ in all_vector]

        return merged_bm25, merged_vector, total, skipped, merged_authority

    def _enrich_results(
        self,
        fused: list[FusedResult],
        project_infos: list[ProjectInfo],
        query: str,
    ) -> list[HybridResult]:
        """Look up chunk metadata for fused results."""
        results: list[HybridResult] = []
        # Cache DB connections by project_id
        db_cache: dict[str, tuple[StoreDB, str]] = {}

        for pinfo in project_infos:
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            if idx_paths.store_db.exists():
                db_cache[pinfo.id] = (StoreDB(idx_paths.store_db), pinfo.name)

        try:
            for fr in fused:
                for pid, (db, pname) in db_cache.items():
                    chunk = db.get_chunk(fr.chunk_id)
                    if chunk is None:
                        continue

                    file_rec = db.get_file(chunk.file_id)
                    file_path = file_rec.relative_path if file_rec else chunk.file_id

                    # Memory Layer: qa_log and memory_card chunks carry file_mtime
                    # through to the boost layer. Paying the attribute
                    # access for every chunk would bloat the hot path
                    # for the 99% of chunks that don't need it.
                    mtime = (
                        file_rec.file_mtime
                        if (file_rec and chunk.node_type in _MEMORY_NODE_TYPES)
                        else None
                    )
                    trust_meta = _trust_meta(
                        node_type=chunk.node_type,
                        trigger=_frontmatter_value(chunk.content, "trigger"),
                        confidence=_frontmatter_value(chunk.content, "confidence"),
                        status=_frontmatter_value(chunk.content, "status"),
                        mtime=mtime,
                        content=chunk.content,
                    )
                    snippet = make_snippet(
                        chunk.docstring,
                        chunk.content,
                        query,
                        node_type=chunk.node_type,
                    )
                    if trust_meta and snippet:
                        snippet = f"{trust_meta}\n{snippet}"
                    results.append(HybridResult(
                        chunk_id=fr.chunk_id,
                        rrf_score=round(fr.rrf_score, 6),
                        bm25_rank=fr.bm25_rank,
                        vector_rank=fr.vector_rank,
                        file_path=file_path,
                        project=pname,
                        name=chunk.name,
                        qualified_name=chunk.qualified_name,
                        node_type=chunk.node_type,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        content=chunk.content,
                        snippet=snippet,
                        file_mtime=mtime,
                        trust_meta=trust_meta,
                    ))
                    break  # Found the chunk, no need to check other projects
        finally:
            for db, _ in db_cache.values():
                db.close()

        return results


# Rationale signal tokens (Step A). When present the query is asking "why" —
# the real answer lives in a single plan/design doc, so module cards only
# inflate read_count. Keep this list conservative: a false positive costs us
# the structure-query improvement the module injection was designed for.
_RATIONALE_TOKENS_KO = ("이유", "배경", "목적", "의도", "동기", "취지")
_RATIONALE_TOKENS_EN = (
    "rationale", "why", "reason", "reasons", "motivation",
    "purpose", "intent", "background",
)
# Korean interrogative "왜" is a one-char particle; handle as whole-token.
_RATIONALE_INTERROGATIVE_KO = "왜"


def _has_rationale_signal(query: str) -> bool:
    """True when the query is asking for a design/motivation answer."""
    q = query.strip()
    if not q:
        return False
    if any(tok in q for tok in _RATIONALE_TOKENS_KO):
        return True
    # Whole-token match for "왜" to avoid accidental substring hits.
    tokens_ko = q.split()
    if any(tok == _RATIONALE_INTERROGATIVE_KO or tok.startswith(_RATIONALE_INTERROGATIVE_KO) for tok in tokens_ko):
        return True
    lower = q.lower()
    for tok in _RATIONALE_TOKENS_EN:
        # Word-boundary match so "purpose" doesn't fire on "multipurpose".
        if re.search(rf"\b{tok}\b", lower):
            return True
    return False


def _has_symbol_signal(query: str) -> bool:
    """True when the query contains a code identifier (camelCase, snake_case,
    SCREAMING_SNAKE, or dot-qualified). Mirror of the EXACT_SYMBOL stage of
    classify_query — kept as a separate helper because ``classify_query`` maps
    mixed (symbol + Korean) queries to KOREAN_NL, but for module-injection
    purposes we still want symbol-bearing queries to behave like EXACT_SYMBOL
    (chunk-only, no subsystem cards)."""
    for w in query.strip().split():
        if _SYMBOL_RE.match(w):
            return True
    return False


def _module_slots_for(qtype: str, query: str = "") -> int:
    """How many module cards to reserve at the top of results per query type.

    Two intent-based bypasses override the qtype default:

    - **Rationale** signal ("왜", "이유", "배경", "why", "purpose"…): the
      real answer is a single plan/design doc; subsystem cards only inflate
      read_count.
    - **Symbol** signal (camelCase / snake_case token present anywhere): the
      agent wants the file that *defines* that symbol, not a sibling module
      card. A mixed query like "TuitionChargeSection 컴포넌트" classifies as
      KOREAN_NL but its intent is precision lookup.
    """
    if query and _has_rationale_signal(query):
        return 0
    if query and _has_symbol_signal(query):
        return 0
    if qtype == QueryType.KOREAN_NL:
        return 3
    if qtype == QueryType.ENGLISH_NL:
        return 2
    return 0  # EXACT_SYMBOL: chunks only — precision queries want exact matches


def _interleave_modules(
    chunks: list[HybridResult],
    modules: list[HybridResult],
    slots: int,
    limit: int,
    *,
    members: list[HybridResult] | None = None,
) -> list[HybridResult]:
    """Interleave up to ``slots`` modules with chunks and module members.

    Placement: module at position 1, then chunk, module, chunk, module, then
    chunks fill the rest. This preserves the top-2 chunk slots at positions
    2 and 4, so a query whose real answer is a single doc (rationale category)
    is not buried by module cards, while structure/exploration queries still
    get a subsystem pointer at the very top.

    Step K — ``members`` is an optional list of ``module_member`` hits
    sibling to the surfaced modules. They enter the chunk stream at the
    head (before regular chunks), clustered by ``module_id`` so that
    A.1/A.2 stay adjacent, and ordered by which card they belong to
    (members of the top-ranked card first). Deduplication is by
    ``file_path`` — if a member shares a path with a module card or a
    chunk result, the earlier entry wins. This is how the admissions
    module's SQL migration lands in top-10 for S5 without fighting the
    ``tuition`` module's own rep path.

    Two-tier cap (Phase 6 L5): effective slots are capped at ``limit // 2``
    so a call with small ``limit`` still guarantees at least half the
    results are chunks. At the default ``limit=10`` with ``slots=3`` this
    is a no-op; at ``limit=5`` it drops to 2 modules, ensuring 3 chunk
    slots survive. Members inherit the cap indirectly — members whose
    parent card was dropped from ``head_modules`` are filtered out so
    the chunk-majority floor is preserved even when member emission is
    aggressive.
    """
    if not modules or slots <= 0 or limit <= 0:
        return chunks[:limit]

    # L5 two-tier: never let modules occupy more than half the result slots.
    slots = min(slots, max(1, limit // 2))
    head_modules = modules[:slots]
    module_files = {m.file_path for m in head_modules}

    # Members are emitted by the orchestrator only for non-card source
    # modules (see ``_module_results_for_query``), so members here never
    # share a ``module_id`` with a card. Preserve emit order (by source
    # module's search rank) and dedup by file_path.
    seen_paths: set[str] = set(module_files)
    deduped_members: list[HybridResult] = []
    for r in members or []:
        if r.file_path in seen_paths:
            continue
        seen_paths.add(r.file_path)
        deduped_members.append(r)

    # Member budget: cap at ``limit // 3`` so at the default
    # ``limit=10`` up to 3 members surface. Each member slot trades
    # a chunk slot — but with card-name-dedup reducing card clones,
    # F3/F4 get more chunk slots to spare, and the S5 admissions SQL
    # + F2 monthly-snapshot-cron both need a member slot to reach
    # top-10.
    members_budget = max(0, limit // 3)
    deduped_members = deduped_members[:members_budget]

    # Dedup chunks against cards + members.
    deduped_chunks = [c for c in chunks if c.file_path not in seen_paths]

    # Layout:
    #   - Module cards at even positions 0, 2, 4, …
    #   - Members at the last ``len(deduped_members)`` positions.
    #   - Chunks fill all remaining positions in order.
    # This keeps the top 2-3 chunks (primary-target documents for
    # rationale and structure queries like S2/S3) at ranks 2 and 4,
    # while still surfacing non-card-module evidence at ranks 8-10.
    slots_arr: list[HybridResult | None] = [None] * limit

    # Place module cards.
    mi = 0
    for pos in range(0, limit, 2):
        if mi >= len(head_modules):
            break
        slots_arr[pos] = head_modules[mi]
        mi += 1

    # Place members at the tail (last empty positions).
    memi = 0
    for pos in range(limit - 1, -1, -1):
        if memi >= len(deduped_members):
            break
        if slots_arr[pos] is None:
            slots_arr[pos] = deduped_members[memi]
            memi += 1

    # Fill remaining slots with chunks in order.
    ci = 0
    for pos in range(limit):
        if slots_arr[pos] is not None:
            continue
        if ci >= len(deduped_chunks):
            break
        slots_arr[pos] = deduped_chunks[ci]
        ci += 1

    return [r for r in slots_arr if r is not None]


def _module_representative_path(
    db: StoreDB,
    m: ModuleRecord,
    query_tokens: set[str] | None = None,
) -> str:
    """Pick a single file_path that best 'locates' this module for clients.

    When ``query_tokens`` is provided (Step J), prefer the member file whose
    filename tokens overlap the query tokens most. This is how the ``stats``
    module, when surfaced for the F2 query "월별 학원 통계", points at
    ``create_academy_monthly_stats.sql`` (3-way token overlap) rather than
    the documentationally-richer but topically-unrelated
    ``app/api/brand-settings/stats/route.ts``.

    Fallback priority (no query tokens or zero overlap):
      first entry_point's file → first related_doc → first member file.
    """
    if query_tokens:
        best = _query_aware_rep_member(db, m, query_tokens)
        if best is not None:
            return best

    if m.entry_points:
        try:
            ep = json.loads(m.entry_points)
            if ep:
                chunk = db.get_chunk(ep[0])
                if chunk:
                    fr = db.get_file(chunk.file_id)
                    if fr:
                        return fr.relative_path
        except (ValueError, TypeError):
            pass
    if m.related_docs:
        try:
            docs = json.loads(m.related_docs)
            if docs:
                return docs[0]
        except (ValueError, TypeError):
            pass
    files = db.get_files_by_module(m.id)
    if files:
        fr = db.get_file(files[0])
        if fr:
            return fr.relative_path
    return m.name or "module"


_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt"})


def _query_aware_rep_member(
    db: StoreDB,
    m: ModuleRecord,
    query_tokens: set[str],
) -> str | None:
    """Among this module's member files, pick the one whose filename
    tokens overlap the query tokens best.

    Tie-break order:
      1. Higher overlap wins (primary).
      2. Code member wins over doc member. Agents that surface a
         subsystem card want the implementation location; the doc is
         already discoverable via the chunk results (recall@10) and
         the related_docs field, so a tied doc would only steal the
         representative slot from a code file that belongs there.
         This recovers F1/F3 recall — the attendance card points at
         ``attendance/makeup-checkin-dialog.tsx``, not the feature
         markdown — while still letting F2 point at
         ``create_academy_monthly_stats.sql`` (the SQL is code).
      3. Shorter path wins (more specific member, fewer ancestor dirs).

    Returns None when no member scores ≥ 1 — caller falls back to the
    fixed-priority rule.
    """
    file_ids = db.get_files_by_module(m.id)
    if not file_ids:
        return None
    candidates: list[tuple[int, int, int, str]] = []  # (score, is_code, -len, path)
    for fid in file_ids:
        fr = db.get_file(fid)
        if fr is None:
            continue
        ftoks = _filename_token_set(fr.relative_path)
        if not ftoks:
            continue
        score = len(ftoks & query_tokens)
        if score == 0:
            continue
        is_code = 0 if Path(fr.relative_path).suffix.lower() in _DOC_SUFFIXES else 1
        candidates.append((score, is_code, -len(fr.relative_path), fr.relative_path))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][3]


def _module_member_paths(
    db: StoreDB,
    m: ModuleRecord,
    query_tokens: set[str],
    max_members: int,
    skip_path: str | None = None,
) -> list[str]:
    """Pick up to ``max_members`` member paths from this module.

    Priority buckets (descending): code members with filename-token
    overlap, code members without overlap (fallback — weight order),
    doc members with overlap, doc members without. Within a bucket we
    preserve ``get_files_by_module`` order, which is weight-descending
    so ``entry_points`` come first.

    The code-over-doc preference is load-bearing: for
    ``tuition-wizard`` on the S1 query, the only overlap > 0 members
    are docs (``tuition-final-adjustment.md``, ``tuition-hub.md``);
    their gold match is the ``components/tuition-wizard/`` directory,
    which only the code members cover. Without this bucketing, S1
    recall never improves from its non-card modules.

    Pass ``skip_path`` to exclude a path already emitted elsewhere —
    typically the module card's rep path for card-surfaced modules.
    For non-card modules we include the rep naturally so its content
    still reaches top-10 (S5 admissions#2 whose rep is the SQL
    migration that holds the gold primary_target).
    """
    if max_members <= 0:
        return []
    file_ids = db.get_files_by_module(m.id)
    if not file_ids:
        return []

    code_overlap: list[str] = []
    code_fallback: list[str] = []
    doc_overlap: list[str] = []
    doc_fallback: list[str] = []
    for fid in file_ids:
        fr = db.get_file(fid)
        if fr is None:
            continue
        path = fr.relative_path
        if skip_path is not None and path == skip_path:
            continue
        is_doc = Path(path).suffix.lower() in _DOC_SUFFIXES
        overlap = (
            len(_filename_token_set(path) & query_tokens)
            if query_tokens else 0
        )
        if is_doc:
            (doc_overlap if overlap > 0 else doc_fallback).append(path)
        else:
            (code_overlap if overlap > 0 else code_fallback).append(path)

    combined = code_overlap + code_fallback + doc_overlap + doc_fallback
    return combined[:max_members]


_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _filename_token_set(rel_path: str) -> set[str]:
    """Lowercase alnum tokens from filename stem, split on hyphen /
    underscore / camelCase boundaries, length ≥ 3. Used to score
    member-vs-query overlap for the query-aware representative-path
    pick (Step J).

    camelCase split matters: ``HomeworkTab.tsx`` → {homework, tab}. Without
    this, a .tsx code member never produced matching tokens and the
    query-aware rep path defaulted to markdown siblings whose hyphenated
    names happened to tokenize — F1's homework-analysis rep drifted to
    a feature .md because ``HomeworkTab`` collapsed to one blob.
    """
    stem = Path(rel_path).stem
    # Drop leading date (e.g., 20260327_).
    stem = re.sub(r"^\d{6,14}_?", "", stem)
    # Split on hyphen / underscore first.
    hyphen_parts = re.split(r"[-_]+", stem)
    out: set[str] = set()
    for hp in hyphen_parts:
        # Then split each piece on camelCase boundaries.
        for cp in _CAMEL_SPLIT_RE.split(hp):
            cp = cp.lower()
            if len(cp) >= 3 and cp.isalnum():
                out.add(cp)
    return out


def _derive_query_tokens(
    query: str,
    alias_specificity: dict[str, int] | None = None,
) -> set[str]:
    """Expand the query into a set of lowercase tokens used for
    query-aware rep-path selection.

    Pass ``alias_specificity`` to gate cross-language aliases: a stem
    whose alias substring-matches many module names (학생 → student on
    a 10+-module catalog) shouldn't pull its English form into the
    filename-match set. Without that gate, F1's homework-analysis rep
    path drifted to ``student-analysis.md`` because "student" scored
    the member on a generic stem mention.
    """
    from hybrid_search.search.modules_search import (
        expand_with_aliases,
        tokenize as ms_tokenize,
    )
    raw = ms_tokenize(query)
    return {
        t.lower() for t in expand_with_aliases(
            raw, alias_specificity=alias_specificity,
        )
    }


def _module_snippet_for_result(m: ModuleRecord) -> str:
    summ = m.summary or ""
    if summ.startswith("[hash:"):
        end = summ.find("]")
        if end != -1:
            summ = summ[end + 1:].strip()
    # Cap snippet to keep response lean for agents.
    return summ[:500]


def _module_content_for_result(m: ModuleRecord) -> str:
    parts = [_module_snippet_for_result(m)]
    if m.rationale:
        parts.append("Rationale:\n" + m.rationale)
    return "\n\n".join(p for p in parts if p)


def _weighted_interleave(
    primary: list[str], secondary: list[str], primary_ratio: int = 2
) -> list[str]:
    """Interleave primary and secondary lists with a ratio (e.g., 2:1 = 2 primary per 1 secondary)."""
    seen: set[str] = set()
    result: list[str] = []
    pi, si = 0, 0

    while pi < len(primary) or si < len(secondary):
        # Take `primary_ratio` items from primary
        for _ in range(primary_ratio):
            while pi < len(primary) and primary[pi] in seen:
                pi += 1
            if pi < len(primary):
                result.append(primary[pi])
                seen.add(primary[pi])
                pi += 1

        # Take 1 item from secondary
        while si < len(secondary) and secondary[si] in seen:
            si += 1
        if si < len(secondary):
            result.append(secondary[si])
            seen.add(secondary[si])
            si += 1

    return result


def _interleave_round_robin(lists: list[list[str]]) -> list[str]:
    """Round-robin interleave multiple ranked lists (§13 BM25 cross-project merge)."""
    seen: set[str] = set()
    result: list[str] = []
    max_len = max((len(lst) for lst in lists), default=0)

    for i in range(max_len):
        for lst in lists:
            if i < len(lst) and lst[i] not in seen:
                result.append(lst[i])
                seen.add(lst[i])

    return result


def _build_filter(
    db: StoreDB,
    project_id: str,
    file_pattern: str | None,
    node_types: list[str] | None,
    exclude_pattern: str | None = None,
) -> set[str] | None:
    """Build a set of matching chunk IDs for filtering.

    ``exclude_pattern`` drops chunks whose file matches the glob (e.g.
    ``docs/*`` to suppress documentation noise from results).
    """
    if not file_pattern and not node_types and not exclude_pattern:
        return None

    import fnmatch

    chunks = db.get_chunks_by_project(project_id)
    filtered_ids: set[str] = set()

    # Pre-load file paths to avoid N+1 queries
    file_path_cache: dict[str, str] = {}
    if file_pattern or exclude_pattern:
        for file_rec in db.get_all_files(project_id):
            file_path_cache[file_rec.id] = file_rec.relative_path

    for chunk in chunks:
        rel_path = file_path_cache.get(chunk.file_id, "")
        if file_pattern and not fnmatch.fnmatch(rel_path, file_pattern):
            continue
        if exclude_pattern and fnmatch.fnmatch(rel_path, exclude_pattern):
            continue
        if node_types and chunk.node_type not in node_types:
            continue
        filtered_ids.add(chunk.id)

    return filtered_ids
