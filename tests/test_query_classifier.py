"""Tests for query classification — search/orchestrator.py classify_query & get_bm25_weight."""

from hybrid_search.search.orchestrator import (
    QueryType,
    QUERY_WEIGHTS,
    classify_query,
    get_bm25_weight,
)


class TestClassifyQuery:
    """classify_query() 3-stage classifier tests."""

    # Stage 1: EXACT_SYMBOL
    def test_camel_case(self) -> None:
        assert classify_query("signIn") == QueryType.EXACT_SYMBOL

    def test_camel_case_multi_word(self) -> None:
        assert classify_query("createUserAccount") == QueryType.EXACT_SYMBOL

    def test_snake_case(self) -> None:
        assert classify_query("tuition_fees") == QueryType.EXACT_SYMBOL

    def test_screaming_snake(self) -> None:
        assert classify_query("MAX_RETRIES") == QueryType.EXACT_SYMBOL

    def test_dot_qualified(self) -> None:
        assert classify_query("AuthService.signIn") == QueryType.EXACT_SYMBOL

    def test_symbol_in_phrase(self) -> None:
        # "find createUser function" → has camelCase word
        assert classify_query("find createUser function") == QueryType.EXACT_SYMBOL

    # Stage 2: KOREAN_NL
    def test_korean_query(self) -> None:
        assert classify_query("할일 관리") == QueryType.KOREAN_NL

    def test_korean_majority(self) -> None:
        assert classify_query("사용자 인증 로직") == QueryType.KOREAN_NL

    def test_korean_with_english_minority(self) -> None:
        # Korean > 50% of alpha chars
        assert classify_query("API 인증 로직 구현") == QueryType.KOREAN_NL

    def test_mixed_korean_and_symbol(self) -> None:
        # Has both Korean and symbol → treated as KOREAN_NL
        assert classify_query("createUser 함수 찾기") == QueryType.KOREAN_NL

    # Stage 3: ENGLISH_NL
    def test_english_phrase(self) -> None:
        assert classify_query("find authentication logic") == QueryType.ENGLISH_NL

    def test_single_english_word(self) -> None:
        assert classify_query("authentication") == QueryType.ENGLISH_NL

    def test_english_question(self) -> None:
        assert classify_query("how does the login flow work") == QueryType.ENGLISH_NL

    # Edge cases
    def test_empty_string(self) -> None:
        assert classify_query("") == QueryType.ENGLISH_NL

    def test_whitespace_only(self) -> None:
        assert classify_query("   ") == QueryType.ENGLISH_NL

    def test_numbers_only(self) -> None:
        assert classify_query("12345") == QueryType.ENGLISH_NL

    def test_single_lowercase_word_not_symbol(self) -> None:
        # "hello" is not camelCase/snake_case/SCREAMING
        assert classify_query("hello") == QueryType.ENGLISH_NL


class TestGetBm25Weight:
    """get_bm25_weight() tests."""

    def test_explicit_weight_overrides(self) -> None:
        weight, qtype = get_bm25_weight("signIn", explicit_weight=0.3)
        assert weight == 0.3
        assert qtype == QueryType.EXACT_SYMBOL

    def test_symbol_weight(self) -> None:
        weight, qtype = get_bm25_weight("createUser")
        assert weight == QUERY_WEIGHTS[QueryType.EXACT_SYMBOL]
        assert qtype == QueryType.EXACT_SYMBOL

    def test_korean_weight(self) -> None:
        weight, qtype = get_bm25_weight("할일 관리")
        assert weight == QUERY_WEIGHTS[QueryType.KOREAN_NL]
        assert qtype == QueryType.KOREAN_NL

    def test_english_weight(self) -> None:
        weight, qtype = get_bm25_weight("find login function")
        assert weight == QUERY_WEIGHTS[QueryType.ENGLISH_NL]
        assert qtype == QueryType.ENGLISH_NL

    def test_mixed_korean_symbol_gets_middle_weight(self) -> None:
        weight, qtype = get_bm25_weight("createUser 함수")
        assert weight == 0.4  # Middle weight for mixed queries
        assert qtype == QueryType.KOREAN_NL

    def test_explicit_weight_zero(self) -> None:
        weight, _ = get_bm25_weight("anything", explicit_weight=0.0)
        assert weight == 0.0

    def test_explicit_weight_one(self) -> None:
        weight, _ = get_bm25_weight("anything", explicit_weight=1.0)
        assert weight == 1.0
