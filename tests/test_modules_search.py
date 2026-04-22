"""Tests for module-level search — search/modules_search.py."""

from __future__ import annotations

import json
from pathlib import Path

from hybrid_search.search.modules_search import (
    module_text,
    search_modules,
    tokenize,
)
from hybrid_search.storage.db import FileRecord, ModuleRecord, StoreDB


PROJECT_ID = "test-project"


def _make_db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


def _upsert_module(db: StoreDB, **overrides) -> ModuleRecord:
    m = ModuleRecord(
        id=overrides.pop("id", f"mod_{len(db.get_modules(PROJECT_ID)):04x}"),
        project_id=PROJECT_ID,
        name=overrides.pop("name", "sample"),
        summary=overrides.pop("summary", None),
        entry_points=overrides.pop("entry_points", None),
        depends_on=overrides.pop("depends_on", None),
        related_docs=overrides.pop("related_docs", None),
        rationale=overrides.pop("rationale", None),
        signals=overrides.pop("signals", json.dumps(["directory"])),
        member_hash=overrides.pop("member_hash", "h"),
        updated_at=overrides.pop("updated_at", "2026-04-22T00:00:00"),
    )
    assert not overrides, f"unused overrides: {overrides}"
    with db.transaction() as conn:
        db.upsert_module(conn, m)
    return m


# ---------- tokenize ----------

def test_tokenize_splits_korean_and_english():
    toks = tokenize("portal 인증 흐름")
    assert "portal" in toks
    assert "인증" in toks
    assert "흐름" in toks


def test_tokenize_drops_stopwords():
    toks = tokenize("포털은 어떻게 동작하나")
    assert "어떻게" not in toks
    assert "하나" not in toks


def test_tokenize_drops_short_tokens():
    toks = tokenize("a b hello world")
    assert "a" not in toks and "b" not in toks
    assert "hello" in toks


# ---------- module_text ----------

def test_module_text_strips_hash_prefix():
    m = ModuleRecord(
        id="m1", project_id=PROJECT_ID, name="portal-v3",
        summary="[hash:v1:abcd] Module about auth flow",
        entry_points=None, depends_on=None, related_docs=None,
        rationale=None, signals=None, member_hash="h",
        updated_at="x",
    )
    text = module_text(m)
    assert "[hash:" not in text
    assert "auth flow" in text


def test_module_text_includes_related_docs():
    m = ModuleRecord(
        id="m1", project_id=PROJECT_ID, name="tuition",
        summary="desc",
        entry_points=None, depends_on=None,
        related_docs=json.dumps(["docs/features/tuition.md"]),
        rationale=None, signals=None, member_hash="h",
        updated_at="x",
    )
    assert "docs/features/tuition.md" in module_text(m)


# ---------- search_modules ----------

def test_search_returns_name_hits_first(tmp_path):
    db = _make_db(tmp_path)
    _upsert_module(db, id="m-portal", name="portal-v3",
                   summary="[hash:v1:aa] UI portal shell")
    _upsert_module(db, id="m-tuition", name="tuition",
                   summary="[hash:v1:bb] tuition fees section")
    hits = search_modules(db, PROJECT_ID, "portal-v3 흐름", limit=3)
    assert hits
    assert hits[0][0].name == "portal-v3"


def test_search_matches_via_related_docs(tmp_path):
    db = _make_db(tmp_path)
    _upsert_module(db, id="m1", name="abstract",
                   summary="[hash:v1:cc] Some unrelated desc",
                   related_docs=json.dumps(["docs/plans/ledger-writepath.md"]))
    hits = search_modules(db, PROJECT_ID, "ledger writepath", limit=2)
    assert hits
    assert hits[0][0].id == "m1"


def test_search_returns_empty_for_no_token_match(tmp_path):
    db = _make_db(tmp_path)
    _upsert_module(db, id="m1", name="alpha", summary="[hash:v1:dd] a b c")
    hits = search_modules(db, PROJECT_ID, "zzz nothing matches", limit=3)
    assert hits == []


def test_search_respects_limit(tmp_path):
    db = _make_db(tmp_path)
    for i in range(5):
        _upsert_module(db, id=f"m{i}", name=f"portal-{i}",
                       summary=f"[hash:v1:ee] portal portal portal variant {i}")
    hits = search_modules(db, PROJECT_ID, "portal", limit=3)
    assert len(hits) == 3


def test_search_handles_empty_query(tmp_path):
    db = _make_db(tmp_path)
    _upsert_module(db, id="m1", name="alpha", summary="x")
    assert search_modules(db, PROJECT_ID, "", limit=3) == []
    assert search_modules(db, PROJECT_ID, "  ", limit=3) == []


def test_search_name_boost_beats_summary_mentions(tmp_path):
    db = _make_db(tmp_path)
    # m-name: name contains "portal", summary 1 mention
    _upsert_module(db, id="m-name", name="portal",
                   summary="[hash:v1:ff] generic content")
    # m-summ: name unrelated, summary contains "portal" 5 times
    _upsert_module(db, id="m-summ", name="unrelated",
                   summary="[hash:v1:aa] portal portal portal portal portal")
    hits = search_modules(db, PROJECT_ID, "portal", limit=2)
    # Name hit (3x boost) should win over plain summary mentions
    assert hits[0][0].id == "m-name"
