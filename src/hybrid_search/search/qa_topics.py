"""Language-general topic matching for qa_log supersession.

Decides whether two Q&As are the *same fact at different times* (newer
supersedes) or merely *adjacent topics sharing vocabulary* (relevance
order must survive). The 2026-07-13 httpx EN holdout showed the previous
raw-overlap matcher was calibrated on Korean token statistics: the
Hangul 2-char prefix acted as a stemmer, English got none, so English
same-topic pairs under-grouped (update newer_first 6/6 KO → 1/6 EN)
while generic English tokens ("unit", "test") over-grouped an
adversarial pair. This module fixes both with:

  1. Language-aware normalization — Korean keeps the josa-tolerant
     2-char prefix; English is stemmed (Snowball); code identifiers
     (``max_connections``, ``SSLContext``, ``http2``) keep their exact
     lowercased form AND contribute their split parts.
  2. Weighted overlap — identifiers count 3x, generic low-information
     words (unit/test/file/학생/파일) count 0.3x, so shared generic
     vocabulary can no longer carry a grouping decision.
  3. A distinctive-shared-token requirement — no grouping at all unless
     the two sides share at least one non-generic token.
  4. Complete-link grouping — every member must match every other
     member, so A≈B≈C chains can never pull A and C into one group.

Thresholds are calibrated on benchmarks/topic_gold_set.json (ko/en/mixed
× same/adjacent/bridge) with a hard zero-false-group constraint on the
adjacent and bridge slices; see benchmarks/topic_gold_eval.py.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["topic_tokens", "weighted_overlap", "same_topic", "topic_group_indices"]

# Tokenized on the ORIGINAL text (no casefold) so camelCase survives long
# enough to be detected as an identifier. \w keeps underscores intact.
_TOKEN_RE = re.compile(r"[\w가-힣]+")
# camelCase and ACRONYMCase (SSLContext, HTTPTransport) — but not a
# plain Capitalized word (Timeout, Report).
_CAMEL_RE = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]")
_IDENT_SPLIT_RE = re.compile(
    r"_+|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
    r"|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])"
)

_W_IDENTIFIER = 3.0
_W_NORMAL = 1.0
_W_GENERIC = 0.3

# Dropped outright: question scaffolding carries no topic signal.
_EN_STOPWORDS = frozenset({
    "how", "what", "where", "when", "which", "who", "why", "whats",
    "the", "a", "an", "of", "for", "in", "on", "at", "to", "with",
    "and", "or", "not", "no", "is", "are", "was", "were", "be", "been",
    "do", "does", "did", "done", "we", "our", "your", "you", "it", "its",
    "this", "that", "these", "those", "they", "them", "there", "then",
    "i", "my", "me", "us", "he", "she", "his", "her",
    "from", "by", "as", "but", "if", "so", "all", "any", "per", "via",
    "into", "onto", "over", "under", "after", "before", "between",
    "only", "also", "just", "more", "most", "than", "up", "down", "out",
    "off", "own", "same", "other", "each", "both", "such", "some",
    "can", "could", "should", "would", "will", "has", "have", "had",
    "explain", "show", "tell", "please",
})

_KO_INSTRUCTION = frozenset({
    "어떻게", "어디", "언제", "누가", "무엇", "뭐지", "뭐야", "왜",
    "있나", "있어", "있지", "되지", "되나", "됐지", "인가", "인지",
    "우리", "설명", "정리", "알려", "보여", "확인",
})

# Low-information words: they appear in Q&As about *any* topic, so a
# shared "test"/"파일" must not carry a grouping decision. Weighted 0.3,
# not dropped — many shared generics still nudge a genuinely-same pair.
_EN_GENERIC_WORDS = (
    "use", "using", "used", "run", "running", "work", "working", "make",
    "add", "added", "remove", "check", "look", "get", "set", "want",
    "need", "change", "changed", "update", "updated", "fix", "fixed",
    "issue", "bug", "code", "function", "project", "problem", "question",
    "answer", "file", "files", "error", "errors", "unit", "test", "tests",
    "fixture", "request", "requests", "response", "responses", "client",
    "server", "api", "app", "data", "log", "logs", "value", "values",
    "call", "calls", "new", "old", "still", "now", "time", "way", "thing",
    # Format and unit tokens ride along with any topic ("csv encoding"
    # appears in attendance uploads AND report exports); they must not
    # count as distinctive shared evidence.
    "csv", "json", "xml", "yaml", "html", "pdf", "excel", "bom",
    "utf", "euc", "ascii", "unicode",
    "mb", "gb", "kb", "tb", "ms", "sec", "kst", "utc",
)

# Hangul generics as 2-char prefixes (post-normalization form).
_KO_GENERIC_PREFIXES = frozenset({
    "학생", "파일", "오류", "에러", "문제", "확인", "변경", "추가",
    "방식", "관련",
    # Domain-neutral verbs and nouns — the Korean side of the
    # "make"/"add"/"change"/"value" entries above. Kept at 0.3 rather
    # than dropped: many shared generics still nudge a genuinely-same
    # pair, and one shared generic must not decide alone.
    "만들", "생성", "삭제", "수정", "적용", "실행", "발생", "처리",
    "진행", "사용", "설정", "작업", "상태", "결과", "내용", "목록",
})

# Korean function words as 2-char prefixes — DROPPED, the counterpart of
# _EN_STOPWORDS. Until 2026-09-09 the Korean side had no such floor: this
# module fixed English's missing stemmer in 2026-07-13 but left English's
# 80-word stopword list with no Korean twin, so copulas, demonstratives,
# quantifiers and 하다/되다/있다/없다 inflections all rode at _W_NORMAL and
# counted as DISTINCTIVE shared tokens.
#
# The damage is length-dependent, which is why the gold set never caught
# it: its one-sentence answers are too short for the noise to accumulate.
# On real consolidated notes (~1.5k chars) it grouped seven distinct
# git-workflow facts into one clique — 21 pairs, 6 of them passing even
# the strict index-time predicate — carried by 것이/다른/아니/실제/있다.
#
# Derived from document frequency over 2,236 real qa notes, then filtered
# to closed-class items only: a high-DF DOMAIN noun must not be listed,
# that would overfit one corpus. Four candidates are deliberately absent
# because the 2-char prefix collides with a real topic word —
# 그래(그래프), 그리(그리드), 이미(이미지), 자기(자기오염/자기평가).
#
# The list is also deliberately INCOMPLETE as a paradigm: 합니/입니/있습 are
# here, 했습/없습/됐습/하겠 are not, and the demonstratives (이게/그건/이제 …)
# are not. Completing either half was implemented and measured — pairwise
# precision moved only 10.6 -> 10.1 accepted per 10k while Set A lost a
# rank (MRR 0.571 -> 0.546), each half costing it independently. A change
# that costs a measured rank and buys nothing measurable does not ship;
# see docs/plans/2026-09-09-topic-matcher-audit.md §5.4. Add to this list
# only with a benchmark run behind it.
_KO_STOPWORD_PREFIXES = frozenset({
    # 있다 / 없다 / 되다 / 하다 / 이다 inflections
    "있습", "있는", "있다", "있어", "있으", "있었", "있고",
    "없다", "없는", "없이", "없어", "없었",
    "된다", "되는", "되지", "됐다", "되고", "됩니",
    "한다", "하는", "하지", "했다", "하고", "하면", "해서", "해야",
    "했고", "했는", "합니", "입니", "습니",
    # 아니다 / 같다
    "아니", "같다", "같은", "같이",
    # 의존명사 · 지시 · 대명사
    "것이", "것은", "것을", "것도", "것만", "것과", "것으",
    "이것", "그것", "저것", "여기", "거기", "저기",
    "내가", "네가", "제가", "우리", "당신", "남의",
    "경우", "정도", "자체", "때문", "위해", "대해", "통해",
    # 접속 · 지시 부사
    "그렇", "이렇", "저렇", "그런", "이런", "저런",
    "그러", "따라", "또는", "만약",
    # 시간 · 정도 부사
    "지금", "다시", "아직", "그냥", "바로", "매우", "아주", "정말",
    "훨씬", "먼저", "나중", "이전", "이후", "처음", "마지",
    # 수량 · 범위
    "하나", "전부", "모두", "모든", "각각", "여러", "몇몇", "대부",
    # 그 밖의 고빈도 관형·부사
    "실제", "다른", "다르",
})


@lru_cache(maxsize=1)
def _en_stemmer():
    """Snowball English stemmer; identity fallback keeps the matcher
    functional (Korean-era behavior) if the dependency is missing."""
    try:
        import snowballstemmer

        return snowballstemmer.stemmer("english").stemWord
    except Exception:
        return lambda w: w


@lru_cache(maxsize=1)
def _en_generic_stems() -> frozenset[str]:
    stem = _en_stemmer()
    return frozenset(stem(w) for w in _EN_GENERIC_WORDS)


def _is_hangul(token: str) -> bool:
    return any("가" <= c <= "힣" for c in token)


_HANGUL_RUN_RE = re.compile(r"[가-힣]+|[^가-힣]+")


def _split_mixed_script(token: str) -> list[str]:
    """Korean dev text glues josa onto identifiers ("cron이", "Vitest로",
    "False는"). Treating the whole token as Hangul destroyed the
    identifier (prefix "cr"). Split into script runs and keep only the
    non-Hangul parts: the attached Hangul run is virtually always a
    josa/suffix, never the topic."""
    if not _is_hangul(token) or all("가" <= c <= "힣" for c in token):
        return [token]
    return [run for run in _HANGUL_RUN_RE.findall(token) if not _is_hangul(run)]


def _is_identifier(token: str) -> bool:
    """snake_case, camelCase, or letter/digit mixes (http2, o200k,
    SSLContext). These keep their exact form: the identifier itself is a
    strong topic signal that stemming would destroy."""
    if "_" in token:
        return True
    if _CAMEL_RE.search(token):
        return True
    has_alpha = any(c.isalpha() for c in token)
    has_digit = any(c.isdigit() for c in token)
    return has_alpha and has_digit


def _english_weight(stemmed: str) -> float:
    return _W_GENERIC if stemmed in _en_generic_stems() else _W_NORMAL


# Harness-inserted attachment banners. Every pasted screenshot carries the
# same sentence, so its tokens ("image", "displayed", "multiply", "coordinates")
# are shared by every screenshot turn in the corpus and nothing else. Left in,
# they dominate topic overlap: the 2026-09-04 Reflector run put four of
# valuein's ten largest clusters together on this boilerplate alone, grouping
# unrelated turns whose only common ground was that a picture was attached.
_ATTACHMENT_BOILERPLATE_RE = re.compile(
    r"\[Image(?:\s*#\d+)?(?::[^\]]{0,200})?\]|\[Pasted text[^\]]{0,120}\]",
    re.IGNORECASE,
)


def strip_attachment_boilerplate(text: str) -> str:
    """Remove attachment banners so topic overlap reflects what was asked."""
    return _ATTACHMENT_BOILERPLATE_RE.sub(" ", text or "")


def topic_tokens(text: str | None) -> dict[str, float]:
    """Normalized token → weight map for topic comparison.

    Korean: josa-tolerant 2-char prefix (unchanged from the original
    matcher). English: lowercase + Snowball stem, stopwords dropped.
    Identifiers: exact lowercased form at 3x weight, plus their split
    parts as ordinary English tokens. Pure digits dropped (timestamps
    and line numbers must never count as topical overlap).
    """
    if not text:
        return {}
    text = strip_attachment_boilerplate(text)
    if not text.strip():
        return {}
    stem = _en_stemmer()
    out: dict[str, float] = {}

    def _put(token: str, weight: float) -> None:
        if len(token) < 2:
            return
        prev = out.get(token, 0.0)
        if weight > prev:
            out[token] = weight

    for mixed in _TOKEN_RE.findall(text):
        for raw in _split_mixed_script(mixed):
            if raw.isdigit():
                continue
            if _is_hangul(raw):
                if raw in _KO_INSTRUCTION or raw.endswith("해줘"):
                    continue
                prefix = raw[:2]
                if prefix in _KO_STOPWORD_PREFIXES:
                    continue
                _put(prefix, _W_GENERIC if prefix in _KO_GENERIC_PREFIXES else _W_NORMAL)
                continue
            lowered = raw.lower()
            if _is_identifier(raw):
                _put(lowered, _W_IDENTIFIER)
                for part in _IDENT_SPLIT_RE.split(raw):
                    part = part.lower()
                    if len(part) < 2 or part.isdigit() or part in _EN_STOPWORDS:
                        continue
                    stemmed = stem(part)
                    _put(stemmed, _english_weight(stemmed))
                continue
            if lowered in _EN_STOPWORDS:
                continue
            stemmed = stem(lowered)
            _put(stemmed, _english_weight(stemmed))
    return out


def weighted_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    """Shared weight over the lighter side's total weight (0..1).

    min(w_a, w_b) per shared token so an identifier on one side and its
    stem-only echo on the other can't overclaim."""
    if not a or not b:
        return 0.0
    shared = sum(min(a[t], b[t]) for t in a.keys() & b.keys())
    denom = min(sum(a.values()), sum(b.values()))
    return shared / denom if denom else 0.0


def _distinctive_shared_count(a: dict[str, float], b: dict[str, float]) -> int:
    return sum(
        1 for t in a.keys() & b.keys() if min(a[t], b[t]) >= _W_NORMAL
    )


# Calibrated on benchmarks/topic_gold_set.json (2026-07-13) under a hard
# zero-false-group constraint on every adjacent + bridge slice (ko/en/
# mixed). Grid sweep in benchmarks/topic_gold_eval.py; chosen point kept
# conservative — the worse failure is still a wrong group (fresh
# adjacent answer stealing an old exact answer's slot), so recall is
# sacrificed before precision. One shared mid-weight word ("base",
# "connect") must never group on its own: two distinctive shared tokens
# are required on every path.
_MIN_DISTINCTIVE_SHARED = 2
_QUERY_OVERLAP = 0.30
_ANSWER_OVERLAP = 0.18
# Cross-language pairs (KO question ↔ EN question) share no question
# tokens at all, but their *answers* share the identifiers that carry
# the fact (max_connections, vitest.config.ts). Adjacent-topic answer
# overlap tops out well below this on the gold set.
_ANSWER_ONLY_OVERLAP = 0.28
_QUERY_ONLY_OVERLAP = 0.6


def same_topic(
    a: tuple[dict[str, float], dict[str, float]],
    b: tuple[dict[str, float], dict[str, float]],
) -> bool:
    """True when two (question-tokens, answer-tokens) pairs describe the
    same fact. Questions share vocabulary cheaply; *answers carry the
    facts* — so the answer signal is always required when both answers
    exist, and at least two distinctive (non-generic) shared tokens are
    required on every path."""
    qa_union_a = {**a[1], **a[0]}
    qa_union_b = {**b[1], **b[0]}
    if _distinctive_shared_count(qa_union_a, qa_union_b) < _MIN_DISTINCTIVE_SHARED:
        return False
    q_ov = weighted_overlap(a[0], b[0])
    if a[1] and b[1]:
        a_ov = weighted_overlap(a[1], b[1])
        if q_ov >= _QUERY_OVERLAP and a_ov >= _ANSWER_OVERLAP:
            return True
        return a_ov >= _ANSWER_ONLY_OVERLAP
    return q_ov >= _QUERY_ONLY_OVERLAP


def topic_group_indices(
    items: list[tuple[dict[str, float], dict[str, float]]],
) -> list[list[int]]:
    """Complete-link grouping: a candidate joins a group only when it
    matches EVERY member, so A≈B and B≈C can never chain A and C into
    one group (the union-find failure mode). Greedy in input order —
    callers pass results in relevance order, making ties deterministic.
    Candidate counts are head-of-results small; O(n²) is fine."""
    groups: list[list[int]] = []
    for i, item in enumerate(items):
        for group in groups:
            if all(same_topic(item, items[j]) for j in group):
                group.append(i)
                break
        else:
            groups.append([i])
    return groups
