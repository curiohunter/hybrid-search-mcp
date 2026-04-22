"""Module-level search — Phase 5 Step 4 + Step C vector fusion.

Scores modules against a query by blending two signals:

  - **token overlap** on a composed "module text" (name + summary + rationale
    + related doc paths). The name column gets a strong boost so a module
    whose name contains a query token beats any number of body mentions.
  - **semantic cosine** between the query embedding and the module's
    ``summary_vector`` (text-embedding-3-small, unit-normalized). Step C
    rolled this in to bridge Korean NL ↔ English module names without
    relying solely on the hand-curated alias list.

Both signals are computed in-memory across all project modules — typically
a few hundred, so this stays sub-millisecond.

Returns top-N ``(ModuleRecord, score)`` with score > 0.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from hybrid_search.storage.db import ModuleRecord, StoreDB

_TOKEN_RE = re.compile(r"[0-9A-Za-z_\-]+|[가-힣]+")
_MIN_TOKEN_LEN = 2

_KOREAN_STOPWORDS = frozenset({
    "어떻게", "어떻", "어떤", "무엇", "무엇인가", "왜", "이유", "어디", "어디에",
    "하나", "되나", "있나", "나요", "무슨", "이것", "그것", "으로", "하는",
})
_ENGLISH_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "how", "what", "why",
    "where", "of", "to", "for", "in", "on", "does", "do", "and", "or",
})

# Hand-curated Korean↔English domain alias map. Queried modules almost always
# have English names (``portal-v3``, ``tuition``); Korean NL queries use native
# terms. Without a bridge the 10x name-boost never fires on cross-language
# queries. Keep this list tight — broad synonyms create false positives.
_ALIAS_PAIRS: tuple[tuple[str, str], ...] = (
    ("포털", "portal"),
    ("학생", "student"),
    ("학부모", "parent"),
    ("수강료", "tuition"),
    ("학원비", "tuition"),
    ("수업", "lesson"),
    ("인증", "auth"),
    ("로그인", "login"),
    ("상담", "consultation"),
    ("숙제", "homework"),
    ("출결", "attendance"),
    ("출석", "attendance"),
    ("원격", "remote"),
    ("입학", "admission"),
    ("시험", "exam"),
    ("계획", "plan"),
    ("설계", "design"),
    ("통계", "stats"),
    ("월별", "monthly"),
    ("퇴원", "withdrawal"),
    ("환불", "refund"),
    ("일정", "schedule"),
    ("변형", "variant"),
    ("문제", "problem"),
    ("교재", "textbook"),
    ("강의", "course"),
    ("분석", "analysis"),
    ("대시보드", "dashboard"),
    ("포털v3", "portal-v3"),
)

_ALIAS_MAP: dict[str, list[str]] = {}
for k, v in _ALIAS_PAIRS:
    _ALIAS_MAP.setdefault(k, []).append(v)
    _ALIAS_MAP.setdefault(v, []).append(k)


def tokenize(query: str) -> list[str]:
    out = []
    for raw in _TOKEN_RE.findall(query):
        if len(raw) < _MIN_TOKEN_LEN:
            continue
        low = raw.lower()
        if low in _KOREAN_STOPWORDS or low in _ENGLISH_STOPWORDS:
            continue
        out.append(raw)
    return out


def expand_with_aliases(tokens: list[str]) -> list[str]:
    """Add bilingual aliases so Korean NL queries can match English module names."""
    seen: list[str] = []
    for t in tokens:
        key = t.lower()
        if key not in seen:
            seen.append(key)
        for alias in _ALIAS_MAP.get(key, ()):
            if alias not in seen:
                seen.append(alias)
    return seen


def module_text(m: ModuleRecord) -> str:
    """Composite searchable text — what BM25 would have indexed.

    Includes member filenames extracted from related_docs so that a query like
    "portal v3" matches a module whose name is ``portal-v3`` even if the
    summary text happens not to contain the query term verbatim.
    """
    parts = [m.name or ""]
    if m.summary:
        # Strip the [hash:...] prefix that synthesis adds for skip-detection.
        summ = m.summary
        if summ.startswith("[hash:"):
            end = summ.find("]")
            if end != -1:
                summ = summ[end + 1:].strip()
        parts.append(summ)
    if m.rationale:
        parts.append(m.rationale)
    if m.related_docs:
        try:
            docs = json.loads(m.related_docs)
            parts.extend(docs)
        except (ValueError, TypeError):
            pass
    return "\n".join(p for p in parts if p)


# Cosine signal must outrank a bare body-overlap (1-3) but not a name-hit
# (≥ 10). A unit cosine of 0.6 × VECTOR_WEIGHT ≈ 9 keeps a strongly-semantic
# module ahead of mere token matches while still losing to the name match —
# which is desirable because a module whose *name* already contains a query
# token is an exceptionally reliable answer.
VECTOR_WEIGHT = 15.0
# Cosine floor — below this the semantic score contributes nothing. Avoids
# noise from cross-language chatter pushing unrelated modules up.
VECTOR_MIN_COSINE = 0.25


def search_modules(
    db: StoreDB,
    project_id: str,
    query: str,
    limit: int = 3,
    query_vector: np.ndarray | None = None,
) -> list[tuple[ModuleRecord, float]]:
    """Score modules by token overlap; if a query vector is provided, blend in
    semantic cosine so Korean NL ↔ English module-name gaps close without
    requiring the alias map to know every pair."""
    tokens = tokenize(query)
    if not tokens and query_vector is None:
        return []
    expanded = expand_with_aliases(tokens) if tokens else []

    modules = db.get_modules(project_id)
    if not modules:
        return []

    use_vector = query_vector is not None
    q = None
    if use_vector:
        q = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        # Guard: embeddings are unit-normalized by Embedder, but re-normalize
        # defensively so tests can pass any vector shape.
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        else:
            use_vector = False

    scored: list[tuple[ModuleRecord, float]] = []
    for m in modules:
        text = module_text(m).lower()
        name_low = (m.name or "").lower()
        token_score = 0.0
        for t in expanded:
            tl = t.lower()
            occ = text.count(tl)
            if occ == 0:
                continue
            # Name hit is the strongest signal that this module is the answer;
            # we want a single name-contained-token to beat several body mentions.
            if tl in name_low:
                token_score += 10.0 + occ
            else:
                token_score += occ

        vec_score = 0.0
        if use_vector and m.summary_vector:
            try:
                mv = np.frombuffer(m.summary_vector, dtype=np.float32)
                if mv.size == q.size:
                    cosine = float(np.dot(q, mv))
                    if cosine >= VECTOR_MIN_COSINE:
                        vec_score = cosine * VECTOR_WEIGHT
            except (ValueError, TypeError):
                pass

        score = token_score + vec_score
        if score > 0:
            scored.append((m, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:limit]
