"""Language-general topic matching for qa_log supersession.

Decides whether two Q&As are the *same fact at different times* (newer
supersedes) or merely *adjacent topics sharing vocabulary* (relevance
order must survive). The 2026-07-13 httpx EN holdout showed the previous
raw-overlap matcher was calibrated on Korean token statistics: the
Hangul 2-char prefix acted as a stemmer, English got none, so English
same-topic pairs under-grouped (update newer_first 6/6 KO → 1/6 EN)
while generic English tokens ("unit", "test") over-grouped an
adversarial pair. This module fixes both with:

  1. Language-aware normalization — Korean by morphological analysis
     when the optional ``[korean]`` extra is installed, else the
     josa-tolerant 2-char prefix; English is stemmed (Snowball); code
     identifiers (``max_connections``, ``SSLContext``, ``http2``) keep
     their exact lowercased form AND contribute their split parts.
  2. Weighted overlap — identifiers count 3x, generic low-information
     words (unit/test/file/학생/파일) count 0.3x, so shared generic
     vocabulary can no longer carry a grouping decision.
  3. A distinctive-shared-token requirement — no grouping at all unless
     the two sides share at least one non-generic token.
  4. Complete-link grouping — every member must match every other
     member, so A≈B≈C chains can never pull A and C into one group.

The Korean side ran without any stopword floor at all until 2026-09-09 —
English got 80 stopwords and a stemmer in 2026-07-13, Korean got 24
question words — so copulas, demonstratives and 하다/되다/있다/없다
inflections all counted as DISTINCTIVE shared tokens. The damage grows
with answer length, which is why one-sentence gold pairs never showed it
and 1.5k-char consolidated notes collapsed seven separate facts into one
supersession clique.

Thresholds are per-backend and calibrated against TWO signals, because
either alone is blind: benchmarks/topic_gold_set.json is the recall floor
(hard zero false groups on adjacent/bridge — benchmarks/topic_gold_eval.py)
and pairwise acceptance over a real qa corpus is the precision signal
(benchmarks/topic_threshold_sweep.py). The gold set is 88 synthetic pairs
and has twice stayed green while corpus behavior moved underneath it.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = [
    "strip_path_directories",
    "topic_tokens",
    "weighted_overlap",
    "same_topic",
    "topic_group_indices",
    "topic_backend",
]

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

# The morphological backend's own low-information tier. Separate from the
# prefix set above because the two tokenizations were calibrated
# separately: a 2-char prefix pools words the analyzer keeps apart, so an
# entry that is safely low-information as a lemma can be wrong as a
# prefix. Adding this list's extra entries to the prefix path was measured
# and cost a rank (see §5.4 of the audit plan) — under the analyzer it
# costs nothing, because 브랜치 can no longer be mistaken for 브랜드.
_KO_GENERIC_LEMMAS = _KO_GENERIC_PREFIXES | frozenset({
    # Process verbs and quantity words at DF >= 15% over 2,231 real qa
    # notes. The criterion is stated so this tier stays reproducible
    # instead of becoming a per-pair fix: language-general process
    # vocabulary only, never a domain noun however frequent (문항 48%,
    # 해설 24%, 교재 23% are the dogfood corpus's own subject matter and
    # keep full weight, or the list overfits one project).
    "고치", "나오", "필요", "다음", "완료", "전체", "반영", "돌리",
    "실제", "통과", "들어가", "바꾸", "남기", "알리",
})

# Korean function words as 2-char prefixes — DROPPED, the counterpart of
# _EN_STOPWORDS. FALLBACK PATH ONLY: with the morphological backend these
# are all reached by part-of-speech instead, and this list is not consulted.
# Until 2026-09-09 the Korean side had no such floor at all: this
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


# --- Korean: morphological backend (optional) --------------------------
#
# The prefix rules above are a stand-in for a morphological analyzer, and
# that is the whole reason the hand list exists. Every published Korean
# stopword list (stopwordsiso ko: 595 entries) holds BASE forms and bare
# particles, because it presupposes an analyzer upstream; only 40% of the
# prefixes above have any counterpart there, and the rest are inflected
# surfaces (있습/했다/됩니/것이) no such list carries. Meanwhile that same
# published list cannot be used as-is, because it drops words that ARE the
# topic in this corpus (근거, 결론, 기준, 비교).
#
# With an analyzer the whole problem dissolves into part-of-speech tags:
# 조사 (J*), 어미 (E*), 접사 (X*), 의존명사 (NNB), 대명사 (NP), 수사 (NR),
# 관형사 (MM) and 부사 (MAG/MAJ) carry no topic, and 이다/아니다 are their own
# tags (VCP/VCN). The 2-char truncation goes away with them, and so does
# its collision problem — 이미(MAG) and 이미지(NNG) become different tokens,
# 그래서(MAJ) stops eating 그래프(NNG).
#
# So kiwipiepy is an OPTIONAL extra (`pip install memory-layer-mcp[korean]`,
# ~104MB of model) and this module degrades to the prefix path without it,
# the same shape as the snowballstemmer fallback below. What survives the
# switch is small and defensible: light verbs that POS cannot rule out
# because they are genuinely VV/VA, and a handful of 형식명사 that kiwi tags
# NNG.
#
# The two backends do NOT produce interchangeable tokens, so the stored
# supersession map records which one built it (see storage/db.py) and the
# query path refuses a map built by the other — a wrong correction is
# worse than none.

_KIWI_CONTENT_TAGS = frozenset({
    "NNG",  # 일반명사
    "NNP",  # 고유명사
    "VV",   # 동사
    "VA",   # 형용사
    "XR",   # 어근 (정확-, 명확- … carries the content of X하다)
})

# Light verbs and 형식명사 that POS alone cannot rule out: kiwi tags them
# NNG/VV/VA like any content word, but they appear in Q&As about every
# topic. Lemma forms (kiwi returns 하 for 했습니다, 위하 for 위해서).
_KO_LIGHT_LEMMAS = frozenset({
    # 하다 / 되다 / 있다 / 없다 / 같다 and the -하다 light verbs
    "하", "되", "있", "없", "같", "말",
    "위하", "대하", "통하", "의하", "관하", "인하", "시키", "드리",
    "보이", "지나", "가지",
    # 형식명사 — kiwi calls these NNG, but they are scaffolding
    "경우", "정도", "자체", "나머지", "마지막", "부분", "때", "번",
    # NOT here: 이유. It was, for one draft, and instrumenting the corpus
    # showed its document frequency fall to 0% — this project's whole
    # thesis is answering *why*, so 이유 is the last word to throw away.
})


@lru_cache(maxsize=1)
def _kiwi():
    """Kiwi morphological analyzer, or None when the optional ``[korean]``
    extra is not installed. Import and model load happen once."""
    try:
        from kiwipiepy import Kiwi

        return Kiwi()
    except Exception:
        return None


def topic_backend() -> str:
    """Identifier for the tokenization in force — stamped on the stored
    supersession map so a backend change cannot be read as data."""
    return "ko-kiwi-1" if _kiwi() is not None else "ko-prefix-1"


def _korean_morphemes(text: str) -> list[str]:
    """Content morphemes of the Hangul in ``text``, or [] without kiwi.

    Non-Hangul forms are left to the identifier/English path below, which
    understands snake_case and camelCase in a way kiwi's SL tag does not.
    """
    kiwi = _kiwi()
    if kiwi is None:
        return []
    out: list[str] = []
    for token in kiwi.tokenize(text):
        if token.tag not in _KIWI_CONTENT_TAGS:
            continue
        form = token.form
        if not _is_hangul(form) or form in _KO_LIGHT_LEMMAS:
            continue
        out.append(form)
    return out


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


# Filesystem paths, reduced to their basename. Same problem as the banner
# above, from a different direction: every turn in a project quotes the
# same directory chain, and its segments are snake_case, so `_is_identifier`
# hands them 3x weight — the highest in the module. Two turns that share
# nothing but `/Users/<name>/project/<repo>/...` then look like one topic.
#
# On 2026-09-09 that was not theoretical: a gold question's answer was
# mapped as superseded by an unrelated shell-command paste, and since the
# splice REPLACES a stale hit at full capacity, the answer left the results
# entirely. Restoring it is what surfaced this rule.
#
# The basename survives on purpose. A file's NAME is a topic — "qa_topics.py
# 어디 고쳤지" is a question about that file — while the directory chain to it
# is scaffolding shared by everything in the repo.
_PATH_RE = re.compile(r"(?:~|\.{0,2}/)[\w.\-]+(?:/[\w.\-]+)+/?")


def strip_path_directories(text: str) -> str:
    """Reduce filesystem paths to their basename."""
    def _basename(match: re.Match[str]) -> str:
        segments = match.group(0).rstrip("/").split("/")
        return f" {segments[-1]} " if segments else " "

    return _PATH_RE.sub(_basename, text or "")


def topic_tokens(text: str | None) -> dict[str, float]:
    """Normalized token → weight map for topic comparison.

    Korean: content morphemes by part-of-speech when the ``[korean]``
    extra is installed, else the josa-tolerant 2-char prefix (the
    original matcher). English: lowercase + Snowball stem, stopwords
    dropped.
    Identifiers: exact lowercased form at 3x weight, plus their split
    parts as ordinary English tokens. Pure digits dropped (timestamps
    and line numbers must never count as topical overlap).
    """
    if not text:
        return {}
    text = strip_path_directories(strip_attachment_boilerplate(text))
    if not text.strip():
        return {}
    stem = _en_stemmer()
    out: dict[str, float] = {}

    def _put(token: str, weight: float, *, min_len: int = 2) -> None:
        # min_len 2 is a PREFIX rule: a 1-char 2-char-prefix is a
        # truncation artifact, never a word. A lemma is not — 얇다 lemmatizes
        # to 얇, and 값·물·키·돌 are ordinary nouns. Applying the prefix rule
        # to lemmas silently deleted them, and cost a gold question whose
        # subject was 얇은 입력 (2026-09-09).
        if len(token) < min_len:
            return
        prev = out.get(token, 0.0)
        if weight > prev:
            out[token] = weight

    # With the [korean] extra the whole text is analyzed once, by
    # part-of-speech; without it each Hangul run falls back to the prefix
    # rules in the loop below. Either way the non-Hangul path is the same.
    morphological = _kiwi() is not None
    for form in _korean_morphemes(text):
        _put(form, _W_GENERIC if form in _KO_GENERIC_LEMMAS else _W_NORMAL, min_len=1)

    for mixed in _TOKEN_RE.findall(text):
        for raw in _split_mixed_script(mixed):
            if raw.isdigit():
                continue
            if _is_hangul(raw):
                if morphological:
                    continue
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
_QUERY_ONLY_OVERLAP = 0.6

# Per-backend, because the ratio means different things in each. The
# analyzer throws away the particles and inflections the prefix path
# keeps, so both sides of every ratio shrink and every overlap rises —
# a genuinely-same gold pair went 0.57 -> 0.85, and so did pairs that
# share nothing but vocabulary. Reusing the prefix point under the
# analyzer passed all 88 gold pairs while corpus-wide acceptance rose
# 10.6 -> 14.5 per 10k on junk pairs; the gold set is far too small to
# see that, which is exactly why it is the recall FLOOR and the corpus
# is the precision signal (benchmarks/topic_threshold_sweep.py).
#
# Both points are the lowest corpus acceptance that still clears the
# gate, at matched gold recall (35/37):
#
#   backend        gold same   corpus accept/10k
#   ko-prefix-1      35/37           10.6
#   ko-kiwi-1        35/37            6.3
#
# 36/37 was available under the analyzer at 11.0 per 10k and declined:
# one gold pair is not worth 75% more false grouping, and this matcher
# sacrifices recall before precision — a false group deletes an answer.
_THRESHOLDS = {
    #                query, answer, answer-only
    "ko-prefix-1": (0.30, 0.18, 0.28),
    "ko-kiwi-1": (0.48, 0.26, 0.37),
}

# Module-level names kept for the sweep tools and for tests that pin a
# single point; `_active_thresholds()` is what the matcher reads.
_QUERY_OVERLAP = 0.30
_ANSWER_OVERLAP = 0.18
# Cross-language pairs (KO question ↔ EN question) share no question
# tokens at all, but their *answers* share the identifiers that carry
# the fact (max_connections, vitest.config.ts). Adjacent-topic answer
# overlap tops out well below this on the gold set.
_ANSWER_ONLY_OVERLAP = 0.28


def _active_thresholds() -> tuple[float, float, float]:
    """(query, answer, answer-only) for the tokenization in force.

    A sweep tool that assigns the module-level names wins, so the
    calibration harness can hold one point across backends."""
    if (_QUERY_OVERLAP, _ANSWER_OVERLAP, _ANSWER_ONLY_OVERLAP) != (0.30, 0.18, 0.28):
        return _QUERY_OVERLAP, _ANSWER_OVERLAP, _ANSWER_ONLY_OVERLAP
    return _THRESHOLDS.get(topic_backend(), (_QUERY_OVERLAP, _ANSWER_OVERLAP,
                                             _ANSWER_ONLY_OVERLAP))


def same_topic(
    a: tuple[dict[str, float], dict[str, float]],
    b: tuple[dict[str, float], dict[str, float]],
) -> bool:
    """True when two (question-tokens, answer-tokens) pairs describe the
    same fact. Questions share vocabulary cheaply; *answers carry the
    facts* — so the answer signal is always required when both answers
    exist, and at least two distinctive (non-generic) shared tokens are
    required on every path."""
    query_thr, answer_thr, answer_only_thr = _active_thresholds()
    qa_union_a = {**a[1], **a[0]}
    qa_union_b = {**b[1], **b[0]}
    if _distinctive_shared_count(qa_union_a, qa_union_b) < _MIN_DISTINCTIVE_SHARED:
        return False
    q_ov = weighted_overlap(a[0], b[0])
    if a[1] and b[1]:
        a_ov = weighted_overlap(a[1], b[1])
        if q_ov >= query_thr and a_ov >= answer_thr:
            return True
        return a_ov >= answer_only_thr
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
