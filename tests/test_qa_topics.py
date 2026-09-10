"""Unit tests for language-general qa topic matching (qa_topics).

The behavioral contract comes from benchmarks/topic_gold_set.json (run
via benchmarks/topic_gold_eval.py); these tests pin the structural
properties that make the gold set pass, so a regression points at the
exact broken mechanism instead of a gold-set aggregate.
"""

from __future__ import annotations

import json
from pathlib import Path

import hybrid_search.search.qa_topics as qa_topics
from hybrid_search.search.qa_topics import (
    _KO_STOPWORD_PREFIXES,
    same_topic,
    topic_group_indices,
    topic_tokens,
    weighted_overlap,
)

GOLD = Path(__file__).parent.parent / "benchmarks" / "topic_gold_set.json"


def _pair(query: str, answer: str) -> tuple[dict, dict]:
    return (topic_tokens(query), topic_tokens(answer))


def _prefix_tokens(text: str) -> dict:
    """topic_tokens forced onto the prefix backend.

    The Korean path has two backends (morphological when the ``[korean]``
    extra is installed, prefix otherwise) and the properties below are
    backend-specific, so a test that means one of them must say which.
    """
    import hybrid_search.search.qa_topics as qt

    real = qt._kiwi
    qt._kiwi = lambda: None
    try:
        return topic_tokens(text)
    finally:
        qt._kiwi = real


class TestTopicTokens:
    def test_english_morphology_stems_to_shared_form(self) -> None:
        variants = ["retry", "retries", "retried"]
        stems = [set(topic_tokens(v)) for v in variants]
        assert stems[0] == stems[1] == stems[2]

    def test_connection_and_connections_share_a_stem(self) -> None:
        assert set(topic_tokens("connection")) == set(topic_tokens("connections"))

    def test_identifier_keeps_exact_form_at_high_weight(self) -> None:
        toks = topic_tokens("we pass max_connections=50 explicitly")
        assert toks["max_connections"] == 3.0

    def test_identifier_contributes_split_parts(self) -> None:
        toks = topic_tokens("SSLContext setup")
        assert "sslcontext" in toks
        assert "ssl" in toks and "context" in toks

    def test_letter_digit_mix_is_identifier(self) -> None:
        toks = topic_tokens("enable http2 support")
        assert toks["http2"] == 3.0
        assert "http" in toks  # split part links to plain-word mentions

    def test_hangul_keeps_josa_tolerant_prefix(self) -> None:
        assert set(topic_tokens("정산 배치는")) == set(topic_tokens("정산 배치가"))

    def test_mixed_script_token_preserves_identifier(self) -> None:
        # "cron이" / "Vitest로" — ASCII identifier with an attached josa
        # must yield the ASCII form, not a mangled Hangul prefix.
        assert "cron" in topic_tokens("배치 cron이 바뀜")
        assert "vitest" in topic_tokens("Vitest로 전환")

    def test_pure_digits_dropped(self) -> None:
        assert topic_tokens("045318 2026") == {}

    def test_english_stopwords_dropped(self) -> None:
        toks = topic_tokens("how does it go from the base image")
        assert "from" not in toks and "how" not in toks and "does" not in toks

    def test_generic_words_downweighted(self) -> None:
        toks = topic_tokens("unit test fixture timeout")
        assert toks[[k for k in toks if k.startswith("test")][0]] < 1.0
        assert toks["timeout"] == 1.0


class TestSameTopic:
    def test_english_update_pair_groups(self) -> None:
        a = _pair(
            "how many retries does the transport do",
            "The transport is created with retries=0.",
        )
        b = _pair(
            "transport retry count changed",
            "We retried connect errors in the transport: HTTPTransport(retries=3).",
        )
        assert same_topic(a, b)

    def test_generic_only_overlap_never_groups(self) -> None:
        # unit + test + fixture shared — all generic; no distinctive link.
        a = _pair(
            "why did the unit test hit its timeout",
            "The unit test exceeded the 30s pytest limit on CI.",
        )
        b = _pair(
            "unit test fixture cleanup order",
            "The database fixture tears down after the cache fixture.",
        )
        assert not same_topic(a, b)

    def test_single_shared_word_is_not_enough(self) -> None:
        # "base" alone (base url vs base image) must not group.
        a = _pair(
            "where is the base url set",
            "base_url is injected by the client factory from tenant config.",
        )
        b = _pair(
            "which base image does the dockerfile use",
            "The Dockerfile builds from python:3.12-slim as the base image.",
        )
        assert not same_topic(a, b)

    def test_cross_language_pair_groups_via_shared_identifiers(self) -> None:
        a = _pair(
            "커넥션 풀 제한 얼마야",
            "httpx.Limits(max_connections=50, max_keepalive_connections=10)을 넘깁니다.",
        )
        b = _pair(
            "connection pool limits tuned",
            "We pass httpx.Limits(max_connections=50, max_keepalive_connections=10) now.",
        )
        assert same_topic(a, b)

    def test_korean_pair_still_groups(self) -> None:
        a = _pair(
            "수강료 정산 배치는 언제 도나요",
            "정산 배치는 매일 새벽 2시(KST), cron 0 2 * * * 로 실행됩니다.",
        )
        b = _pair(
            "정산 배치 시각 변경 확인",
            "정산 배치가 새벽 2시에서 4시로 변경됐습니다. cron은 0 4 * * * 입니다.",
        )
        assert same_topic(a, b)

    def test_missing_answers_require_near_identical_question(self) -> None:
        a = (topic_tokens("학생 숙제 파일 저장 위치"), {})
        b = (topic_tokens("학생 출결 파일 업로드"), {})
        assert not same_topic(a, b)


class TestWeightedOverlap:
    def test_empty_sides_are_zero(self) -> None:
        assert weighted_overlap({}, {"a": 1.0}) == 0.0

    def test_shared_weight_uses_min_side(self) -> None:
        a = {"timeout": 1.0, "retri": 1.0}
        b = {"timeout": 1.0}
        assert weighted_overlap(a, b) == 1.0


class TestTopicGroupIndices:
    def test_bridge_chain_does_not_merge_endpoints(self) -> None:
        # A≈B (timeout), B≈C (retry) — complete-link must keep A and C
        # apart even though union-find would chain all three.
        a = _pair("what is the default timeout value", "The default timeout is 30 seconds.")
        b = _pair(
            "how do timeout and retry interact",
            "Each retry attempt gets its own timeout budget; retries never extend the deadline.",
        )
        c = _pair(
            "what is the retry backoff curve",
            "Retry backoff is exponential from 250ms with full jitter.",
        )
        groups = topic_group_indices([a, b, c])
        assert not any(0 in g and 2 in g for g in groups)

    def test_same_topic_trio_groups_fully(self) -> None:
        a = _pair("default request timeout", "Default timeout is 5 seconds for all phases.")
        b = _pair("default timeout raised", "Timeout default went from 5 to 30 seconds.")
        c = _pair(
            "request timeout value now",
            "The default timeout is 30 seconds after the change.",
        )
        groups = topic_group_indices([a, b, c])
        assert [0, 1, 2] in groups

    def test_order_is_deterministic(self) -> None:
        a = _pair("default timeout", "Default timeout is 5 seconds.")
        b = _pair("unrelated proxy setup", "Proxy is egress.internal with trust_env off.")
        assert topic_group_indices([a, b]) == [[0], [1]]


class TestKoreanStopwordFloor:
    """The Korean counterpart of _EN_STOPWORDS (added 2026-09-09).

    English got 80 dropped stopwords plus a stemmer in 2026-07-13; the
    Korean side kept only 24 question words, so copulas, demonstratives,
    quantifiers and 하다/되다/있다/없다 inflections rode at full weight and
    counted as DISTINCTIVE shared tokens. The damage is length-dependent
    — one-sentence answers never accumulate enough of them — which is why
    only the long-answer gold slice exposes it.
    """

    def test_function_words_are_dropped(self) -> None:
        toks = topic_tokens("그것은 실제로 다른 경우와 같은 것이 아니라고 합니다")
        assert toks == {}

    def test_topic_nouns_survive_beside_function_words(self) -> None:
        toks = topic_tokens("그것은 실제로 정산 배치가 아니라고 합니다")
        assert set(toks) == {"정산", "배치"}

    def test_list_is_not_extended_past_what_measurement_supports(self) -> None:
        """The list stops where the benchmark stopped paying for it.

        Completing the 하다/되다/있다 paradigm (했습, 없습, 됐습, 하겠 …) and
        adding the demonstratives (이게, 그건, 이제 …) was implemented and
        measured on 2026-09-09: pairwise precision moved 10.6 -> 10.1
        accepted per 10k, and Set A lost a rank (C6 1 -> 2, MRR 0.571 ->
        0.546) for it. Both halves cost that rank independently. So
        했습니다 stays in while 합니다 is dropped — an asymmetry that is a
        decision, not an oversight, and this test says so out loud.
        """
        assert "합니" in _KO_STOPWORD_PREFIXES
        assert "했습" not in _KO_STOPWORD_PREFIXES

    def test_prefix_collisions_are_not_dropped(self) -> None:
        # 그래(그래서) / 그리(그리고) / 이미(이미) / 자기(자기) are function
        # words, but their 2-char prefixes are also the head of real topic
        # words. The prefix scheme cannot tell them apart, so none of the
        # four may be listed. (The morphological backend has no such
        # problem — it keeps 그래프 whole — which is why this asserts on
        # the prefix path specifically.)
        for word in ("그래프", "그리드", "이미지", "자기오염"):
            assert word[:2] in _prefix_tokens(word), word

    def test_long_korean_answers_on_adjacent_topics_do_not_group(self) -> None:
        # Shape of the 2026-09-09 field finding: two distinct facts about
        # one workflow, stated at consolidated-note length. Before the floor
        # they grouped on 것이/다른/아니/실제/있다 alone.
        a = _pair(
            "머지한 다음에 원격까지 올렸나요",
            "이 질문에는 말이 아니라 실물로 답합니다. 지금 로컬 브랜치가 origin/main보다 "
            "앞서 있는지 보고, 실제로 올라간 커밋 목록을 다시 확인해서 그대로 보여드립니다. "
            "남의 커밋이 섞여 있으면 그대로 밀지 않고 먼저 알립니다. 다른 세션이 만든 작업을 "
            "내가 대신 올리는 것은 아니기 때문입니다.",
        )
        b = _pair(
            "안 쓰는 워크트리 정리해 주세요",
            "워크트리를 지우기 전에 그 안에 남은 미머지 작업이 있는지 먼저 셉니다. 다른 세션이 "
            "만들어 둔 브랜치가 아직 메인에 들어가지 않았을 수 있어서, 커밋이 남아 있으면 "
            "지우지 않고 알립니다. 실제로 남의 작업을 통째로 날린 적이 있어서 이 순서는 "
            "그대로 지킵니다.",
        )
        assert not same_topic(a, b)

    def test_removing_shared_noise_strengthens_a_genuine_pair(self) -> None:
        # The floor is not a blunt threshold raise: it shrinks the
        # denominator too, so a pair carried by real topic words scores
        # HIGHER after it. This is why same-slice recall did not move.
        a = _pair(
            "정산 배치 언제 도는지 알려줘",
            "수강료 정산 배치는 매일 새벽 2시(KST)에 돕니다. cron 표현식은 0 2 * * * 이고, "
            "실패하면 다시 돌리지 않고 알림만 남깁니다. 지금 설정은 그대로입니다.",
        )
        b = _pair(
            "정산 배치 시각 바뀐 거 확인해줘",
            "수강료 정산 배치 시각이 새벽 2시에서 4시로 바뀌었습니다. cron 표현식은 "
            "0 4 * * * 입니다. 실패 시 재시도하지 않는 것은 이전과 같습니다.",
        )
        assert same_topic(a, b)
        assert weighted_overlap(a[1], b[1]) > 0.5


class TestGoldSetGate:
    """The full gold set is the regression contract for the matcher.

    Same-topic recall is floored PER LANGUAGE (matching
    benchmarks/topic_gold_eval.py) — an aggregate floor would let an
    English regression hide behind Korean/mixed passes, which is exactly
    the language-generality failure this PR exists to prevent.
    """

    SAME_FLOOR = {"ko": 0.90, "en": 0.85, "mixed": 0.85}

    def test_gold_set_gate_passes(self) -> None:
        pairs = json.loads(GOLD.read_text())["pairs"]
        same: dict[str, list[int]] = {}
        for p in pairs:
            if p.get("known_limitation"):
                continue
            if p["relation"] == "bridge":
                items = [_pair(i["query"], i["answer"]) for i in p["items"]]
                groups = topic_group_indices(items)
                assert not any(0 in g and 2 in g for g in groups), (
                    f"{p['id']}: bridge endpoints chained into one group"
                )
            elif p["relation"] == "adjacent":
                got = same_topic(_pair(**p["a"]), _pair(**p["b"]))
                assert not got, f"{p['id']}: adjacent pair falsely grouped"
            else:
                bucket = same.setdefault(p["lang"], [0, 0])
                bucket[1] += 1
                bucket[0] += same_topic(_pair(**p["a"]), _pair(**p["b"]))
        # Per-language recall floors; NOT 100% — conservative by design.
        for lang, floor in self.SAME_FLOOR.items():
            passed, total = same[lang]
            assert passed / total >= floor, (
                f"{lang}/same recall {passed}/{total} below floor {floor:.0%}"
            )

class TestDevWorkflowVocabularyStaysDistinctive:
    """머지·푸시·배포·커밋·브랜치 must NOT be low-information.

    They look exactly like the English generic tier (test, file, code,
    request) and demoting them was implemented and measured on
    2026-09-09: corpus-wide pairwise acceptance improved 8.5 -> 5.9 per
    10k and every gold slice still passed — and Set A top3 fell 0.70 ->
    0.65, on the one question that asks about the push procedure itself
    ("메인에 올리기 전에 뭘 확인하기로 했었지"). Demote the vocabulary of a
    workflow and you lose the people asking about that workflow. The
    objective function makes Set A top3 >= 0.70 a hard floor, so this is
    a rejected branch, not an oversight.
    """

    def test_workflow_words_keep_full_weight(self) -> None:
        for word in ("머지", "푸시", "배포", "커밋", "브랜치"):
            assert word not in qa_topics._KO_GENERIC_LEMMAS, word
            assert word not in qa_topics._KO_GENERIC_PREFIXES, word

