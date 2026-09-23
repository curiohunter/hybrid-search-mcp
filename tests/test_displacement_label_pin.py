"""A displacement label judges a pair, so it must not outlive its successor.

2026-09-23: a hand label's rationale described a successor the supersession
map no longer named — the verdict was being applied to a pair nobody judged.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "displacement_audit.py"
_spec = importlib.util.spec_from_file_location("displacement_audit_pin", _PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def test_unpinned_label_is_trusted_as_before() -> None:
    labels = {"a": {"verdict": "damage"}}
    assert audit.label_for(labels, "a", "s2") == {"verdict": "damage"}


def test_pinned_label_holds_for_its_successor() -> None:
    labels = {"a": {"verdict": "legitimate", "successor": "s1"}}
    assert audit.label_for(labels, "a", "s1")["verdict"] == "legitimate"


def test_pinned_label_goes_stale_when_successor_changes() -> None:
    labels = {"a": {"verdict": "legitimate", "successor": "s1"}}
    assert audit.label_for(labels, "a", "s2") is None


def test_unknown_successor_keeps_the_label() -> None:
    labels = {"a": {"verdict": "legitimate", "successor": "s1"}}
    assert audit.label_for(labels, "a", None)["verdict"] == "legitimate"


def test_missing_label() -> None:
    assert audit.label_for({}, "a", "s1") is None
