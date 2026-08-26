"""Suite-wide fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_query_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ADV3 cross-language lane calls the OpenAI chat API for
    Hangul-dominant queries. Tests must never hit the network, so the
    lane is force-disabled suite-wide. Tests that exercise the lane
    inject a fake translator (``orch._translator``) or a ``request_fn``
    into ``QueryTranslator`` directly — neither path checks this toggle.
    """
    monkeypatch.setenv("HYBRID_SEARCH_TRANSLATION", "0")
