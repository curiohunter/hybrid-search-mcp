"""Search orchestrator — query classification + BM25/Vector coordination + RRF fusion.

Implements §11 query classification and cross-project search (§13).
"""

from __future__ import annotations

import json
import re
import time
import logging
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from hybrid_search.config import Config
from hybrid_search.index.embedder import Embedder
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


@dataclass
class HybridSearchResponse:
    results: list[HybridResult]
    query_type: str
    effective_bm25_weight: float
    query_time_ms: float
    total_chunks_searched: int
    skipped_projects: list[str] = field(default_factory=list)
    reranked: bool = False


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
                return HybridSearchResponse(
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
            return HybridSearchResponse(
                results=[], query_type=qtype, effective_bm25_weight=effective_weight,
                query_time_ms=0, total_chunks_searched=0,
            )

        # Embed query once
        query_vector = self._embedder.embed_query(query)
        retrieval_depth = limit * 3

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

        # Phase 5: inject module cards when the query is likely structural.
        # Module cards give agents a subsystem-level answer unit so they don't
        # have to Read 5 files to piece together "how is X organized".
        module_results = self._module_results_for_query(
            qtype, query, project_infos, query_vector
        )
        module_slots = _module_slots_for(qtype, query)
        results = _interleave_modules(chunk_results, module_results, module_slots, limit)

        elapsed_ms = (time.monotonic() - start) * 1000
        return HybridSearchResponse(
            results=results,
            query_type=qtype,
            effective_bm25_weight=effective_weight,
            query_time_ms=round(elapsed_ms, 1),
            total_chunks_searched=total,
            skipped_projects=skipped,
            reranked=reranking_cfg.enabled,
        )

    def _module_results_for_query(
        self,
        qtype: str,
        query: str,
        project_infos: list[ProjectInfo],
        query_vector=None,
    ) -> list[HybridResult]:
        """Score modules across the project scope; return as HybridResult list.

        ``query_vector`` is the already-embedded query from the chunk search
        path — passing it through lets Step C's semantic fusion fire without
        spending another API call.
        """
        if _module_slots_for(qtype, query) == 0:
            return []

        per_project_limit = 5
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
        results: list[HybridResult] = []
        for pinfo, m, _score in hits:
            project_dir = get_project_dir(self._config.projects_dir, pinfo.id)
            idx_paths = IndexPaths(project_dir)
            db = StoreDB(idx_paths.store_db)
            try:
                qtoks = query_tokens_by_project.get(pinfo.id, set())
                file_path = _module_representative_path(db, m, qtoks)
                results.append(HybridResult(
                    chunk_id=f"module:{m.id}",
                    rrf_score=0.0,
                    bm25_rank=None,
                    vector_rank=None,
                    file_path=file_path,
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
            finally:
                db.close()
        return results

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

            # BM25 search
            bm25_results = bm25_eng.search(query, limit=depth)
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
                bm25_res = bm25_eng.search(query, limit=depth)
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
                        snippet=make_snippet(chunk.docstring, chunk.content, query),
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
) -> list[HybridResult]:
    """Interleave up to ``slots`` modules with chunks.

    Placement: module at position 1, then chunk, module, chunk, module, then
    chunks fill the rest. This preserves the top-2 chunk slots at positions
    2 and 4, so a query whose real answer is a single doc (rationale category)
    is not buried by module cards, while structure/exploration queries still
    get a subsystem pointer at the very top.

    Two-tier cap (Phase 6 L5): effective slots are capped at ``limit // 2``
    so a call with small ``limit`` still guarantees at least half the
    results are chunks. At the default ``limit=10`` with ``slots=3`` this
    is a no-op; at ``limit=5`` it drops to 2 modules, ensuring 3 chunk
    slots survive.
    """
    if not modules or slots <= 0 or limit <= 0:
        return chunks[:limit]

    # L5 two-tier: never let modules occupy more than half the result slots.
    slots = min(slots, max(1, limit // 2))
    head_modules = modules[:slots]
    module_files = {m.file_path for m in head_modules}
    deduped_chunks = [c for c in chunks if c.file_path not in module_files]

    result: list[HybridResult] = []
    mi = ci = 0
    # Positions 1,3,5,... get a module (zero-indexed: 0,2,4). After module slot
    # budget is exhausted we just stream chunks.
    for pos in range(limit):
        want_module = (pos % 2 == 0) and (mi < len(head_modules))
        if want_module:
            result.append(head_modules[mi])
            mi += 1
        elif ci < len(deduped_chunks):
            result.append(deduped_chunks[ci])
            ci += 1
        elif mi < len(head_modules):
            result.append(head_modules[mi])
            mi += 1
        else:
            break
    return result


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


