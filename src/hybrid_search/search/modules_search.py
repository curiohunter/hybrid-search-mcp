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

# Step H: Korean particles (조사). Stripped stems give the alias map a
# chance to fire on NL queries like "통계는" → "통계" → "stats".
_KOREAN_PARTICLES = (
    "는", "은", "이", "가", "을", "를", "의", "에", "도", "만",
    "로", "랑", "과", "와", "까지", "부터", "에서", "에게",
)

# When a particle-stripped token pulls in a cross-language alias, we
# inject the alias only if that alias is *specific* — i.e. it appears
# as a substring in at most this many module names. "stats" (1 match)
# passes; "student" (matches student-hub / students / …) is blocked.
# Found empirically on valuein_homepage: ≤ 3 preserves every specific
# domain noun the gold set cares about without re-introducing the
# generic-noun regression the naive strip triggered.
_MAX_ALIAS_MODULE_MATCHES = 3


def _is_korean(tok: str) -> bool:
    return bool(tok) and all("가" <= ch <= "힣" for ch in tok)


def _strip_korean_particle(tok: str) -> str | None:
    """Return a shorter form if the token ends in a common particle and
    the stripped stem is still ≥ 2 Hangul chars; else None. Only applies
    to pure-Hangul tokens — English/code tokens stay as-is."""
    if not _is_korean(tok):
        return None
    for p in _KOREAN_PARTICLES:
        if len(tok) > len(p) + 1 and tok.endswith(p):
            stem = tok[: -len(p)]
            if len(stem) >= 2:
                return stem
    return None


def compute_alias_specificity(modules) -> dict[str, int]:
    """Count, per alias form, how many module names contain it as a
    substring. Feeds ``expand_with_aliases`` so particle-stripped
    cross-language aliases only fire when the target alias is narrow
    enough to actually disambiguate (e.g. ``stats`` but not ``student``).
    Computed once per ``search_modules`` call — negligible cost at
    a few hundred modules."""
    names = [((m.name or "").lower()) for m in modules]
    counts: dict[str, int] = {}
    all_forms: set[str] = set()
    for k, v in _ALIAS_PAIRS:
        all_forms.add(k.lower())
        all_forms.add(v.lower())
    for form in all_forms:
        counts[form] = sum(1 for n in names if form and form in n)
    return counts


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


def expand_with_aliases(
    tokens: list[str],
    alias_specificity: dict[str, int] | None = None,
) -> list[str]:
    """Add bilingual aliases so Korean NL queries can match English module
    names.

    Step H: Korean tokens also contribute their particle-stripped stems
    (통계는 → 통계) so the alias lookup catches base nouns that would
    otherwise miss due to trailing particles on NL phrasing. The
    *cross-language alias* produced by stripping is only injected when
    ``alias_specificity`` tells us it's narrow (≤
    ``_MAX_ALIAS_MODULE_MATCHES`` module-name substring matches).
    Generic-noun aliases like "student" — which would promote
    student-adjacent modules for every query that happens to contain
    "학생이" — are thereby blocked. The stem itself is always added so
    Korean direct-match on module content still improves.
    """
    seen: list[str] = []

    def _add(val: str) -> None:
        if val and val not in seen:
            seen.append(val)

    for t in tokens:
        key = t.lower()
        _add(key)
        for alias in _ALIAS_MAP.get(key, ()):
            _add(alias)

        stripped = _strip_korean_particle(t)
        if not stripped:
            continue
        sk = stripped.lower()
        aliases = _ALIAS_MAP.get(sk, ())
        # Only act on the stem when it actually reaches the alias map —
        # otherwise adding it just injects a generic Korean body-match
        # ("시스템", "예약") that promotes unrelated Korean prose docs.
        # The stem itself and any cross-language alias only contribute
        # signal in tandem with the alias map.
        if not aliases:
            continue
        _add(sk)
        for alias in aliases:
            if alias_specificity is not None:
                matches = alias_specificity.get(alias.lower(), 0)
                if matches > _MAX_ALIAS_MODULE_MATCHES:
                    continue
            _add(alias)
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

    modules = db.get_modules(project_id)
    if not modules:
        return []

    # Specificity gate data for alias expansion — computed once per
    # search so particle-stripped aliases only inject when they're
    # actually narrow.
    specificity = compute_alias_specificity(modules) if tokens else None
    expanded = (
        expand_with_aliases(tokens, alias_specificity=specificity)
        if tokens else []
    )

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
