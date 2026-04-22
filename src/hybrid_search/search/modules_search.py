"""Module-level search — Phase 5 Step 4.

Scores modules against a query using token overlap on a composed "module text"
(name + summary + rationale + related doc paths + member filenames). Cheap
enough to scan all project modules in-memory because there are typically only
a few hundred.

Returns top-N ``ModuleRecord`` with a score > 0. Callers blend these into the
hybrid response ahead of or alongside chunk results depending on query type.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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


def search_modules(
    db: StoreDB,
    project_id: str,
    query: str,
    limit: int = 3,
) -> list[tuple[ModuleRecord, float]]:
    tokens = tokenize(query)
    if not tokens:
        return []
    expanded = expand_with_aliases(tokens)

    modules = db.get_modules(project_id)
    if not modules:
        return []

    scored: list[tuple[ModuleRecord, float]] = []
    for m in modules:
        text = module_text(m).lower()
        name_low = (m.name or "").lower()
        score = 0.0
        for t in expanded:
            tl = t.lower()
            occ = text.count(tl)
            if occ == 0:
                continue
            # Name hit is the strongest signal that this module is the answer;
            # we want a single name-contained-token to beat several body mentions.
            if tl in name_low:
                score += 10.0 + occ
            else:
                score += occ
        if score > 0:
            scored.append((m, score))

    scored.sort(key=lambda x: -x[1])
    return scored[:limit]
