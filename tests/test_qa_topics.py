"""Unit tests for language-general qa topic matching (qa_topics).

The behavioral contract comes from benchmarks/topic_gold_set.json (run
via benchmarks/topic_gold_eval.py); these tests pin the structural
properties that make the gold set pass, so a regression points at the
exact broken mechanism instead of a gold-set aggregate.
"""

from __future__ import annotations

import json
from pathlib import Path

from hybrid_search.search.qa_topics import (
    same_topic,
    topic_group_indices,
    topic_tokens,
    weighted_overlap,
)

GOLD = Path(__file__).parent.parent / "benchmarks" / "topic_gold_set.json"


def _pair(query: str, answer: str) -> tuple[dict, dict]:
    return (topic_tokens(query), topic_tokens(answer))


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


class TestGoldSetGate:
    """The full gold set is the regression contract for the matcher."""

    def test_gold_set_gate_passes(self) -> None:
        pairs = json.loads(GOLD.read_text())["pairs"]
        same_pass = same_total = 0
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
                same_total += 1
                same_pass += same_topic(_pair(**p["a"]), _pair(**p["b"]))
        # Recall floor; NOT 100% — the matcher is conservative by design.
        assert same_pass / same_total >= 0.90, f"same recall {same_pass}/{same_total}"
