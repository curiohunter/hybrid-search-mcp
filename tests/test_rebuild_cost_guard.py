"""A forced rebuild must say what it will spend before it spends it.

One day's work re-ran a 15,800-chunk project three times while chasing
three separate fixes, plus twice more that died on a lock conflict. That
burned roughly 70M embedding tokens — about two thirds of it avoidable by
batching the changes into a single rebuild. Nothing in the tool said a
number until the credits ran out.

The guard is not a spending limit. It is the number, shown early enough
that "batch this with the next change" is still an option.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

from hybrid_search import cli, providers
from hybrid_search.cost import CostEstimate, estimate_rebuild


def _estimate(usd):
    return CostEstimate(files=100, chars=1_000_000, tokens=400_000,
                        model="gemini-embedding-2", usd=usd)


def _run(usd, *, yes=False, tty=True, answer="n"):
    args = argparse.Namespace(yes=yes)
    with patch("hybrid_search.cost.estimate_rebuild", return_value=_estimate(usd)), \
         patch("sys.stdin.isatty", return_value=tty), \
         patch("builtins.input", return_value=answer):
        return cli._confirm_rebuild_cost(args, MagicMock(), "/tmp/p")


class TestConfirmation:
    def test_declining_stops_the_rebuild(self):
        assert _run(3.35, answer="n") is False

    def test_accepting_proceeds(self):
        assert _run(3.35, answer="y") is True

    def test_yes_flag_skips_the_prompt(self):
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            assert _run(3.35, yes=True) is True

    def test_hooks_and_ci_are_never_blocked(self):
        """A post-commit hook has no one to answer the prompt; blocking it
        would wedge the commit. The estimate still reaches its log."""
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            assert _run(3.35, tty=False) is True

    def test_a_cheap_rebuild_is_not_worth_interrupting(self):
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            assert _run(0.03) is True

    def test_an_unpriced_model_does_not_block(self):
        """A missing price means we cannot inform the decision — guessing
        a number would be worse than staying quiet."""
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            assert _run(None) is True

    def test_the_estimate_is_printed_either_way(self, capsys):
        _run(3.35, answer="y")
        assert "$3.35" in capsys.readouterr().out


class TestEstimate:
    def test_ratio_is_measured_not_assumed(self, tmp_path):
        """Assuming a code-like chars/token ratio under-counted a
        Korean-heavy corpus by 47% — 3.5 assumed against 2.38 measured."""
        (tmp_path / "ko.md").write_text("이 프로젝트는 하이브리드 검색을 합니다.\n" * 200)
        cfg = MagicMock()
        cfg.indexing.supported_extensions = [".md"]
        cfg.indexing.exclude_patterns = []
        cfg.indexing.max_file_size_kb = 512
        cfg.indexing.index_qa_logs = False
        cfg.embedding.backend = "gemini"
        cfg.embedding.model = ""
        cfg.embedding.openai_model = "text-embedding-3-small"
        cfg.embedding.dimensions = 0
        est = estimate_rebuild(tmp_path, cfg)
        assert est is not None
        assert est.chars / est.tokens < 3.0  # Korean packs far denser

    def test_empty_project_has_no_estimate(self, tmp_path):
        cfg = MagicMock()
        cfg.indexing.supported_extensions = [".py"]
        cfg.indexing.exclude_patterns = []
        cfg.indexing.max_file_size_kb = 512
        cfg.indexing.index_qa_logs = False
        assert estimate_rebuild(tmp_path, cfg) is None


class TestPricing:
    def test_known_models_are_priced(self):
        assert providers.input_price("gemini-embedding-2") == 0.20
        assert providers.input_price("text-embedding-3-small") == 0.02

    def test_unknown_model_returns_none_rather_than_a_guess(self):
        assert providers.input_price("gemini-embedding-99") is None
