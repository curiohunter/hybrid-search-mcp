"""Module scoring must not reward size.

`token_score` used a raw `text.count()`, so a module's score scaled with how
much text it contained. One module in a live corpus had a 100,476-char
summary against a 334-char median — an accretion of unrelated docs — and it
took top-1 on 3 of 25 gold queries that had nothing to do with it: a portal
auth question, a ledger design question, and a memory-layer question all
landed on the same blob.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hybrid_search.search import modules_search
from hybrid_search.search.modules_search import OCCURRENCE_CAP, search_modules


def _module(name: str, text: str):
    m = MagicMock()
    m.name = name
    m.summary_vector = None
    m.__str__ = lambda s: name
    modules_search.module_text = modules_search.module_text  # keep real fn
    return m


class TestOccurrenceSaturation:
    def test_repetition_stops_counting_past_the_cap(self, monkeypatch):
        """The 40th mention of a term must not outrank the 4th."""
        focused = _module("tuition", "tuition billing")
        blob = _module("misc", "tuition " * 200)
        monkeypatch.setattr(
            modules_search, "module_text",
            lambda m: "tuition billing" if m.name == "tuition" else "tuition " * 200,
        )
        db = MagicMock()
        db.get_modules.return_value = [focused, blob]
        scored = dict(
            (m.name, s) for m, s in search_modules(db, "p", "tuition", limit=5)
        )
        # 'tuition' is in the focused module's NAME, which is the strongest
        # signal; the blob may only accumulate up to the cap.
        assert scored["misc"] <= OCCURRENCE_CAP
        assert scored["tuition"] > scored["misc"]

    def test_a_term_present_a_few_times_still_scores(self, monkeypatch):
        """Saturation must not become a binary present/absent signal."""
        monkeypatch.setattr(modules_search, "module_text", lambda m: "alpha alpha beta")
        db = MagicMock()
        m = _module("mod", "")
        db.get_modules.return_value = [m]
        scored = search_modules(db, "p", "alpha", limit=5)
        assert scored and scored[0][1] == 2  # two occurrences, under the cap

    def test_cap_is_documented_as_a_constant(self):
        assert OCCURRENCE_CAP == 3
