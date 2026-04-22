"""Tests for module discovery — index/modules.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybrid_search.index.modules import (
    UnionFind,
    _extract_path_mentions,
    _module_key_for,
    discover_modules,
)
from hybrid_search.storage.db import FileRecord, StoreDB


PROJECT_ID = "test-project"


def _make_db(tmp_path: Path) -> StoreDB:
    return StoreDB(tmp_path / "store.db")


def _seed_file(db: StoreDB, rel_path: str, project_id: str = PROJECT_ID) -> str:
    fid = f"f_{project_id}_{hash(rel_path) & 0xffffffff:x}"
    with db.transaction() as conn:
        db.upsert_file(
            conn,
            FileRecord(
                id=fid, project_id=project_id,
                relative_path=rel_path, file_hash="h",
            ),
        )
    return fid


def _write_file(root: Path, rel_path: str, body: str = "") -> None:
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body)


# ---------- _module_key_for ----------

def test_module_key_leaf_directory():
    assert _module_key_for("components/portal-v3/login.tsx") == "portal-v3"


def test_module_key_deep_path():
    # Non-container top-level dir stays in the key
    assert _module_key_for("docs/features/auth.md") == "docs/features"


def test_module_key_root_file():
    assert _module_key_for("README.md") == "root:md"


def test_module_key_strips_container_prefix():
    # src/, app/, components/, lib/ are container dirs — skipped so immediate
    # sub-dir becomes the module key.
    assert _module_key_for("src/auth/login.py") == "auth"
    assert _module_key_for("app/dashboard/page.tsx") == "dashboard"


# ---------- _extract_path_mentions ----------

def test_extract_path_mentions_in_prose():
    text = (
        "We updated `src/auth/login.py` and also components/portal-v3/form.tsx. "
        "See docs/features/auth.md for context."
    )
    hits = _extract_path_mentions(text)
    assert "src/auth/login.py" in hits
    assert "components/portal-v3/form.tsx" in hits
    assert "docs/features/auth.md" in hits


def test_extract_path_mentions_ignores_plain_words():
    text = "The login flow works as expected. Hello world."
    assert _extract_path_mentions(text) == set()


# ---------- UnionFind ----------

def test_union_find_merges_transitively():
    uf = UnionFind(["a", "b", "c"])
    uf.union("a", "b")
    uf.union("b", "c")
    assert uf.find("a") == uf.find("c")


def test_union_find_handles_unknown_members():
    uf = UnionFind(["a"])
    uf.union("a", "b")  # b was not in original set
    assert uf.find("a") == uf.find("b")


# ---------- discover_modules ----------

def test_discover_single_directory_forms_module(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/a.tsx")
    _seed_file(db, "components/portal-v3/b.tsx")

    stats = discover_modules(db, PROJECT_ID, tmp_path)
    assert stats["modules"] == 1
    assert stats["files_assigned"] == 2
    mods = db.get_modules(PROJECT_ID)
    assert len(mods) == 1
    assert mods[0].name == "portal-v3"
    signals = json.loads(mods[0].signals)
    assert "directory" in signals


def test_discover_singleton_code_file_dropped(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/alone/only.tsx")

    stats = discover_modules(db, PROJECT_ID, tmp_path)
    # Singleton code file is below threshold and has no docs → dropped
    assert stats["modules"] == 0


def test_discover_singleton_doc_kept(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "docs/features/auth.md")
    _write_file(tmp_path, "docs/features/auth.md", "Some content")

    stats = discover_modules(db, PROJECT_ID, tmp_path)
    # Doc-only singleton stays as a module (docs often stand alone).
    assert stats["modules"] == 1


def test_discover_doc_mention_merges_modules(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/a.tsx")
    _seed_file(db, "components/portal-v3/b.tsx")
    _seed_file(db, "docs/features/portal.md")
    _write_file(
        tmp_path, "docs/features/portal.md",
        "Portal uses components/portal-v3/a.tsx and components/portal-v3/b.tsx",
    )

    stats = discover_modules(db, PROJECT_ID, tmp_path)
    # Doc-mention should pull all three into one module.
    assert stats["modules"] == 1
    assert stats["files_assigned"] == 3
    mods = db.get_modules(PROJECT_ID)
    signals = json.loads(mods[0].signals)
    assert "doc_mention" in signals
    assert "has_doc" in signals


def test_discover_unmentioned_doc_stays_separate(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/a.tsx")
    _seed_file(db, "components/portal-v3/b.tsx")
    _seed_file(db, "docs/features/portal.md")
    _write_file(tmp_path, "docs/features/portal.md", "Just prose, no paths.")

    stats = discover_modules(db, PROJECT_ID, tmp_path)
    assert stats["modules"] == 2  # portal-v3 + docs/features
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    assert "portal-v3" in mods
    assert "features" in mods


def test_discover_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "a/x.py")
    _seed_file(db, "a/y.py")

    s1 = discover_modules(db, PROJECT_ID, tmp_path)
    s2 = discover_modules(db, PROJECT_ID, tmp_path)
    assert s1 == s2
    mods = db.get_modules(PROJECT_ID)
    assert len(mods) == 1
    # Hashes stable across runs (determinism proxy)
    assert mods[0].member_hash


def test_discover_related_docs_populated(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/tuition/row.tsx")
    _seed_file(db, "components/tuition/table.tsx")
    _seed_file(db, "docs/features/tuition.md")
    _write_file(
        tmp_path, "docs/features/tuition.md",
        "Tuition row is `components/tuition/row.tsx`",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = db.get_modules(PROJECT_ID)
    assert len(mods) == 1
    docs = json.loads(mods[0].related_docs)
    assert "docs/features/tuition.md" in docs


def test_discover_file_module_weights(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/tuition/row.tsx")
    _seed_file(db, "components/tuition/table.tsx")
    _seed_file(db, "docs/features/tuition.md")
    _write_file(
        tmp_path, "docs/features/tuition.md",
        "See components/tuition/row.tsx",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = db.get_modules(PROJECT_ID)
    assert len(mods) == 1
    mid = mods[0].id
    # Verify via SQL that doc got weight 0.5, code 1.0
    cur = db._conn.execute(
        "SELECT weight FROM file_modules WHERE module_id = ? ORDER BY weight",
        (mid,),
    )
    weights = [row[0] for row in cur.fetchall()]
    assert 0.5 in weights
    assert 1.0 in weights


def test_discover_project_scope_isolation(tmp_path):
    db1 = _make_db(tmp_path / "p1")
    db2 = _make_db(tmp_path / "p2")
    _seed_file(db1, "a/x.py", project_id=PROJECT_ID)
    _seed_file(db1, "a/y.py", project_id=PROJECT_ID)
    _seed_file(db2, "b/x.py", project_id="other")
    _seed_file(db2, "b/y.py", project_id="other")
    discover_modules(db1, PROJECT_ID, tmp_path / "p1")
    discover_modules(db2, "other", tmp_path / "p2")
    assert db1.get_module_count(PROJECT_ID) == 1
    assert db1.get_module_count("other") == 0
    assert db2.get_module_count("other") == 1
    assert db2.get_module_count(PROJECT_ID) == 0


def test_discover_get_files_by_module(tmp_path):
    db = _make_db(tmp_path)
    fid_a = _seed_file(db, "x/a.py")
    fid_b = _seed_file(db, "x/b.py")
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = db.get_modules(PROJECT_ID)
    files_in_mod = db.get_files_by_module(mods[0].id)
    assert set(files_in_mod) == {fid_a, fid_b}


def test_search_modules_by_name(tmp_path):
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/a.tsx")
    _seed_file(db, "components/portal-v3/b.tsx")
    _seed_file(db, "components/tuition/a.tsx")
    _seed_file(db, "components/tuition/b.tsx")
    discover_modules(db, PROJECT_ID, tmp_path)
    hits = db.search_modules_by_name("portal", PROJECT_ID)
    assert len(hits) == 1
    assert hits[0].name == "portal-v3"
