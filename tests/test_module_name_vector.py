"""A module's name and its contents are separate signals.

They used to be concatenated and embedded as one vector. That let each
erase the other: on a live corpus the module whose name answered the
query exactly — "문제은행" against `problem-bank`, cosine 0.657 on the
name alone — fell to 15th place once its own file listing was folded in
(0.476), losing to an unrelated module at 0.537.

The fix is not a bigger alias dictionary. A multilingual embedding model
already relates the Korean noun to the English module name; the hand-kept
56-entry Korean↔English map existed only because an earlier model could
not. What was missing was giving the name its own vector so that signal
survives to query time.

Measured after the change, on six Korean queries: the expected module
went to rank 1 in all six (previously 15, 25, 14, 9, 1, 1).
"""

from __future__ import annotations

import numpy as np

from hybrid_search.search.modules_search import VECTOR_MIN_COSINE, search_modules
from hybrid_search.storage.db import ModuleRecord


class _Db:
    def __init__(self, modules):
        self._modules = modules

    def get_modules(self, project_id):
        return self._modules


def _mod(name, summary, name_vec, summ_vec):
    def blob(v):
        return np.asarray(v, dtype=np.float32).tobytes() if v is not None else None
    return ModuleRecord(
        id=f"m_{name}", project_id="p", name=name, summary=summary,
        summary_vector=blob(summ_vec), name_vector=blob(name_vec),
    )


class TestNameVectorIsScoredOnItsOwn:
    def test_a_name_match_wins_despite_diluted_card_text(self):
        """The reported failure: the right module's card text drags its
        combined vector below an unrelated module's."""
        q = np.array([1.0, 0.0], dtype=np.float32)
        right = _mod("problem-bank", "…", name_vec=[0.99, 0.14], summ_vec=[0.45, 0.89])
        wrong = _mod("api-students", "…", name_vec=[0.30, 0.95], summ_vec=[0.60, 0.80])
        got = search_modules(_Db([right, wrong]), "p", "문제은행", limit=2, query_vector=q)
        assert got[0][0].name == "problem-bank"

    def test_content_still_wins_when_the_name_says_nothing(self):
        """Opaque names are common; the summary must remain a way in."""
        q = np.array([1.0, 0.0], dtype=np.float32)
        opaque = _mod("core", "…", name_vec=[0.1, 0.99], summ_vec=[0.97, 0.24])
        other = _mod("misc", "…", name_vec=[0.2, 0.98], summ_vec=[0.3, 0.95])
        got = search_modules(_Db([opaque, other]), "p", "질문", limit=2, query_vector=q)
        assert got[0][0].name == "core"

    def test_a_module_without_a_name_vector_still_scores(self):
        """Indexes written before the column existed must keep working."""
        q = np.array([1.0, 0.0], dtype=np.float32)
        legacy = _mod("legacy", "…", name_vec=None, summ_vec=[0.95, 0.31])
        got = search_modules(_Db([legacy]), "p", "질문", limit=2, query_vector=q)
        assert got and got[0][0].name == "legacy"

    def test_a_weak_match_on_both_is_still_filtered(self):
        """The minimum-cosine bar applies to the stronger of the two, not
        to their average — but a module weak on both stays out."""
        q = np.array([1.0, 0.0], dtype=np.float32)
        weak = _mod("weak", "", name_vec=[0.1, 0.99], summ_vec=[0.1, 0.99])
        got = search_modules(_Db([weak]), "p", "질문", limit=2, query_vector=q)
        assert not got or got[0][1] < VECTOR_MIN_COSINE * 15
