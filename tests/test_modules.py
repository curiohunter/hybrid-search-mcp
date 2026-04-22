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


def test_discover_promotes_singleton_when_dirname_in_doc_body(tmp_path):
    """Step F3: a size-1 code dir normally gets dropped, but if any doc
    body mentions the dir name as a distinct token we treat that as
    enough signal to keep it as a module — this closes the F2 gold-miss
    where components/analytics/ had just one file and no explicit path
    mention, so it never surfaced as a subsystem answer."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/analytics/monthly.tsx")
    _seed_file(db, "docs/features/stats.md")
    _write_file(
        tmp_path, "docs/features/stats.md",
        "The analytics subsystem computes monthly stats for each academy.",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    assert "analytics" in mods
    # Signal flag advertises why a singleton was kept.
    assert "doc_promoted" in json.loads(mods["analytics"].signals)


def test_discover_promotes_singleton_when_file_path_mentioned(tmp_path):
    """Singleton dir gets kept when the doc mentions its file by path
    (the classic mention case, but now surviving the ≥ 2 files rule)."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/analytics/snapshot.tsx")
    _seed_file(db, "docs/features/unrelated.md")
    _write_file(
        tmp_path, "docs/features/unrelated.md",
        "See components/analytics/snapshot.tsx for the cron wiring.",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    names = {m.name for m in db.get_modules(PROJECT_ID)}
    assert "analytics" in names


def test_discover_singleton_without_any_doc_still_dropped(tmp_path):
    """Negative case: no doc mentions the dir or its file → still dropped
    so we don't flood the module list with every one-off directory."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/unnoticed/lonely.tsx")
    discover_modules(db, PROJECT_ID, tmp_path)
    names = {m.name for m in db.get_modules(PROJECT_ID)}
    assert "unnoticed" not in names


def test_discover_name_prose_crossref_attaches_doc(tmp_path):
    """Step F5: a doc that mentions a module's leaf name multiple times
    in prose (without a proper file-path mention) should still cross-ref
    onto that module. Closes S2 failure mode where portal-parent-student.md
    referenced portal-v3 in prose alongside parenthesized paths the path
    regex can't capture."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/shell.tsx")
    _seed_file(db, "components/portal-v3/layout.tsx")
    _seed_file(db, "docs/features/parent-portal.md")
    # Prose mentions only (no file path). Repeated so it clears the
    # ≥ _MIN_NAME_MENTIONS threshold and counts as topical.
    _write_file(
        tmp_path, "docs/features/parent-portal.md",
        "The portal-v3 shell serves 학부모 and 학생 accounts. "
        "portal-v3 renders the header and bottom bar.",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    assert "portal-v3" in mods
    assert "docs/features/parent-portal.md" in json.loads(mods["portal-v3"].related_docs)
    assert "crossref_doc" in json.loads(mods["portal-v3"].signals)


def test_discover_name_prose_single_mention_ignored(tmp_path):
    """Single-mention name-drops shouldn't attach (keeps generic design
    manifests out of every module's cross-refs)."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/admissions/a.tsx")
    _seed_file(db, "components/admissions/b.tsx")
    _seed_file(db, "docs/design-system.md")
    _write_file(
        tmp_path, "docs/design-system.md",
        "Design system for all routes including admissions page.",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    assert "design-system.md" not in json.dumps(
        json.loads(mods["admissions"].related_docs)
    )


def test_discover_name_prose_generic_meta_doc_skipped(tmp_path):
    """DESIGN.md / CLAUDE.md / README.md are project-level manifests that
    list every subsystem by name; F5 explicitly skips them regardless of
    occurrence count."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/admissions/a.tsx")
    _seed_file(db, "components/admissions/b.tsx")
    _seed_file(db, "DESIGN.md")
    _write_file(
        tmp_path, "DESIGN.md",
        "admissions admissions admissions — the flow touches admissions.",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    # Even with 4 occurrences, DESIGN.md never attaches.
    assert "DESIGN.md" not in json.loads(mods["admissions"].related_docs)


def test_discover_name_prose_crossref_skips_self_doc(tmp_path):
    """A doc located inside the module itself shouldn't cross-ref onto
    that module (it's already a primary member)."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/a.tsx")
    _seed_file(db, "components/portal-v3/b.tsx")
    _seed_file(db, "components/portal-v3/README.md")
    _write_file(
        tmp_path, "components/portal-v3/README.md",
        "portal-v3 overview",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    # README is already a primary member, not a crossref.
    cur = db._conn.execute(
        "SELECT weight FROM file_modules WHERE module_id = ?",
        (mods["portal-v3"].id,),
    )
    weights = sorted(row[0] for row in cur.fetchall())
    # Two code files (1.0) + one doc (0.5). No 0.2 crossref weight.
    assert 0.2 not in weights


def test_discover_crossref_doc_attaches_to_multiple_modules(tmp_path):
    """Step F1: a doc that mentions files across >1 module keys should stay
    with its own docs module (strict-merge rule) but also become a low-weight
    member of each mentioned target module."""
    db = _make_db(tmp_path)
    fid_a = _seed_file(db, "components/portal-v3/shell.tsx")
    _seed_file(db, "components/portal-v3/layout.tsx")
    fid_h = _seed_file(db, "components/student-hub/page.tsx")
    _seed_file(db, "components/student-hub/card.tsx")
    _seed_file(db, "docs/features/portal-parent-student.md")
    _write_file(
        tmp_path, "docs/features/portal-parent-student.md",
        "Parents and students share components/portal-v3/shell.tsx "
        "and components/student-hub/page.tsx.",
    )

    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    assert "portal-v3" in mods
    assert "student-hub" in mods

    # Cross-ref: doc advertises in related_docs of each target module.
    portal_docs = json.loads(mods["portal-v3"].related_docs)
    hub_docs = json.loads(mods["student-hub"].related_docs)
    assert "docs/features/portal-parent-student.md" in portal_docs
    assert "docs/features/portal-parent-student.md" in hub_docs

    # And the crossref signal is set on both.
    assert "crossref_doc" in json.loads(mods["portal-v3"].signals)
    assert "crossref_doc" in json.loads(mods["student-hub"].signals)

    # The doc file is a member of both modules (low weight) in addition to
    # its own docs/features module.
    portal_files = set(db.get_files_by_module(mods["portal-v3"].id))
    hub_files = set(db.get_files_by_module(mods["student-hub"].id))
    doc_file = db.get_file_by_path(PROJECT_ID, "docs/features/portal-parent-student.md")
    assert doc_file.id in portal_files
    assert doc_file.id in hub_files
    # Primary code files still there.
    assert fid_a in portal_files
    assert fid_h in hub_files


def test_discover_crossref_weight_is_lower_than_primary(tmp_path):
    """Cross-ref doc members land with _WEIGHT_DOC_CROSSREF (0.2), strictly
    below the primary doc weight (0.5) so file_modules ranking still favors
    direct ownership."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/portal-v3/shell.tsx")
    _seed_file(db, "components/portal-v3/layout.tsx")
    _seed_file(db, "components/student-hub/a.tsx")
    _seed_file(db, "components/student-hub/b.tsx")
    _seed_file(db, "docs/features/overlap.md")
    _write_file(
        tmp_path, "docs/features/overlap.md",
        "Touches components/portal-v3/shell.tsx and components/student-hub/a.tsx",
    )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    doc_file = db.get_file_by_path(PROJECT_ID, "docs/features/overlap.md")
    cur = db._conn.execute(
        "SELECT weight FROM file_modules WHERE file_id = ? AND module_id = ?",
        (doc_file.id, mods["portal-v3"].id),
    )
    weight = cur.fetchone()[0]
    assert weight == 0.2


def test_discover_crossref_cap_per_module(tmp_path):
    """When many multi-target docs point at the same module, we cap how many
    become cross-ref members to prevent HANDOFF-style bloat."""
    db = _make_db(tmp_path)
    _seed_file(db, "components/target/a.tsx")
    _seed_file(db, "components/target/b.tsx")
    _seed_file(db, "components/other/a.tsx")
    _seed_file(db, "components/other/b.tsx")
    # Five multi-target docs, all pointing at target + other.
    for i in range(5):
        rel = f"docs/d{i}.md"
        _seed_file(db, rel)
        _write_file(
            tmp_path, rel,
            "See components/target/a.tsx and components/other/a.tsx",
        )
    discover_modules(db, PROJECT_ID, tmp_path)
    mods = {m.name: m for m in db.get_modules(PROJECT_ID)}
    target_docs = [
        d for d in json.loads(mods["target"].related_docs)
        if d.startswith("docs/")
    ]
    # _MAX_CROSSREFS_PER_MODULE = 3
    assert len(target_docs) <= 3


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
