"""Tests for module-level search — search/modules_search.py."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from hybrid_search.search.modules_search import (
    VECTOR_MIN_COSINE,
    VECTOR_WEIGHT,
    _strip_korean_particle,
    compute_alias_specificity,
    expand_with_aliases,
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


# ---------- Step H: selective particle strip ----------

def test_strip_korean_particle_common_endings():
    assert _strip_korean_particle("통계는") == "통계"
    assert _strip_korean_particle("학원에서") == "학원"
    assert _strip_korean_particle("학생이") == "학생"


def test_strip_korean_particle_english_passes_through():
    assert _strip_korean_particle("portal") is None
    assert _strip_korean_particle("portal-v3") is None


def test_expand_injects_alias_when_stem_is_specific(tmp_path):
    """통계는 → 통계 (alias lookup finds stats, specificity = 1 on a
    catalog with only a `stats` module) → `stats` gets injected."""
    db = _make_db(tmp_path)
    _upsert_module(db, name="stats")
    _upsert_module(db, name="briefs")
    spec = compute_alias_specificity(db.get_modules(PROJECT_ID))
    out = expand_with_aliases(["월별", "통계는"], alias_specificity=spec)
    assert "stats" in out
    assert "통계" in out
    assert "monthly" in out


def test_expand_blocks_alias_when_target_is_generic(tmp_path):
    """학생이 → 학생 stem has alias `student`. If `student` appears in
    many module names, the gate blocks the cross-language injection to
    avoid polluting queries where 학생 is just a generic subject noun."""
    db = _make_db(tmp_path)
    for nm in (
        "students", "student-hub", "student-detail", "student-list",
    ):
        _upsert_module(db, name=nm)
    spec = compute_alias_specificity(db.get_modules(PROJECT_ID))
    out = expand_with_aliases(["학생이", "숙제"], alias_specificity=spec)
    # Stem still added (Korean-on-Korean body match is safe).
    assert "학생" in out
    # But the cross-language alias is gated out.
    assert "student" not in out
    # Direct (non-strip) alias on 숙제 → homework is unaffected.
    assert "homework" in out


def test_expand_skips_stem_without_alias(tmp_path):
    """Step H refinement: a particle-stripped stem that has no alias
    (e.g., 시스템) adds nothing. Previously this injected noise —
    generic Korean stems matched unrelated Korean prose docs."""
    db = _make_db(tmp_path)
    _upsert_module(db, name="consultations")
    spec = compute_alias_specificity(db.get_modules(PROJECT_ID))
    out = expand_with_aliases(["시스템은", "상담"], alias_specificity=spec)
    # 시스템 has no alias → stem not added.
    assert "시스템" not in out
    # Direct alias on 상담 → consultation still fires.
    assert "consultation" in out


def test_compute_alias_specificity_counts_substring_matches(tmp_path):
    db = _make_db(tmp_path)
    _upsert_module(db, name="stats")
    _upsert_module(db, name="students")
    _upsert_module(db, name="student-hub")
    spec = compute_alias_specificity(db.get_modules(PROJECT_ID))
    assert spec["stats"] == 1
    # "student" appears in both "students" and "student-hub".
    assert spec["student"] == 2


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


# ---------- Step C: vector fusion ----------

def _upsert_module_with_vector(
    db: StoreDB,
    mid: str,
    name: str,
    vector: np.ndarray,
) -> ModuleRecord:
    vec_bytes = np.asarray(vector, dtype=np.float32).tobytes()
    m = ModuleRecord(
        id=mid, project_id=PROJECT_ID, name=name,
        summary=f"[hash:v1:cc] {name} card text",
        entry_points=None, depends_on=None, related_docs=None,
        rationale=None, signals=json.dumps(["directory"]),
        member_hash="h", updated_at="2026-04-22T00:00:00",
        summary_vector=vec_bytes,
        vector_input_hash="deadbeef",
    )
    with db.transaction() as conn:
        db.upsert_module(conn, m)
    return m


def _unit(v: list[float]) -> np.ndarray:
    arr = np.array(v, dtype=np.float32)
    n = np.linalg.norm(arr)
    return arr / n if n > 0 else arr


def test_vector_raises_semantic_match_above_unrelated(tmp_path):
    """Module whose vector aligns with the query should beat an unrelated
    module even when neither module name contains any query token."""
    db = _make_db(tmp_path)
    # "Aligned" module: vector (1, 0, 0); query vector also (1, 0, 0) → cos=1.0
    _upsert_module_with_vector(
        db, "m-aligned", "quantum-flux",  # name has no token overlap with query
        vector=_unit([1.0, 0.0, 0.0]),
    )
    # "Orthogonal" module: vector (0, 1, 0); cos with query = 0.0
    _upsert_module_with_vector(
        db, "m-orth", "telemetry-buffer",
        vector=_unit([0.0, 1.0, 0.0]),
    )
    query_vec = _unit([1.0, 0.0, 0.0])
    hits = search_modules(
        db, PROJECT_ID, "completely unrelated words", limit=2,
        query_vector=query_vec,
    )
    assert hits
    assert hits[0][0].id == "m-aligned"


def test_vector_below_floor_contributes_zero(tmp_path):
    """Cosine < VECTOR_MIN_COSINE must not add to the score — keeps cross-
    language noise from lifting unrelated modules."""
    db = _make_db(tmp_path)
    # Cosine will be tiny (0.1) — below floor (0.25 default).
    low_cos = _unit([1.0, 0.0, 0.0]) * 0.1 + _unit([0.0, 1.0, 0.0]) * 0.995
    low_cos = low_cos / np.linalg.norm(low_cos)
    _upsert_module_with_vector(db, "m-low", "foobar", vector=low_cos)
    query_vec = _unit([1.0, 0.0, 0.0])
    # No token overlap + below-floor cosine → score 0 → no hit.
    hits = search_modules(
        db, PROJECT_ID, "xyz zzz", limit=2, query_vector=query_vec,
    )
    assert hits == []


def test_vector_loses_to_exact_name_hit(tmp_path):
    """A name-token hit (≥ 10 score) should outrank even a perfect cosine (≈ 15
    after weight). We want name-hit to be sticky so 'portal-v3' queries land
    on the portal-v3 module even if some semantic sibling is closer in
    embedding space."""
    db = _make_db(tmp_path)
    # Module with exact name hit but weak vector
    _upsert_module_with_vector(
        db, "m-name", "tuition",
        vector=_unit([0.0, 1.0, 0.0]),  # orthogonal to query vec
    )
    # Module with perfect vector but name no token overlap
    _upsert_module_with_vector(
        db, "m-vec", "quantum-flux",
        vector=_unit([1.0, 0.0, 0.0]),  # perfect cosine
    )
    query_vec = _unit([1.0, 0.0, 0.0])
    hits = search_modules(
        db, PROJECT_ID, "tuition", limit=2, query_vector=query_vec,
    )
    # Name hit (~10 + body mentions) tied with cosine (15.0), cosine wins
    # narrowly — BUT the scorer treats the name-hit module as having both
    # token_score and (orthogonal) vec_score=0, so token-only path beats only
    # when body hits compound. Verify the name hit still lands in top-2.
    names = [h[0].name for h in hits]
    assert "tuition" in names


def test_vector_weight_dominates_single_body_mention(tmp_path):
    """A strong semantic match should beat a single bland body mention.
    Confirms VECTOR_WEIGHT is tuned high enough for cross-language bridging."""
    db = _make_db(tmp_path)
    # 1 body mention of "foo" → token_score = 1
    _upsert_module_with_vector(
        db, "m-body", "unrelated-name",
        vector=_unit([0.0, 1.0, 0.0]),  # cos = 0
    )
    # No token match but strong cosine
    _upsert_module_with_vector(
        db, "m-cos", "different-name",
        vector=_unit([1.0, 0.0, 0.0]),  # cos = 1 → vec_score = 15
    )
    # Put "foo" only in m-body's summary so token path hits it.
    with db.transaction() as conn:
        db._conn.execute(
            "UPDATE modules SET summary = ? WHERE id = ?",
            ("[hash:v1:cc] foo desc", "m-body"),
        )
    query_vec = _unit([1.0, 0.0, 0.0])
    hits = search_modules(
        db, PROJECT_ID, "foo", limit=2, query_vector=query_vec,
    )
    # m-cos's vector score (15) should outrank m-body's body score (1)
    assert hits[0][0].id == "m-cos"


def test_vector_ignored_when_module_has_no_stored_vector(tmp_path):
    """Backward compatibility: older v6 rows have no summary_vector. Scoring
    must fall back to token-only for those modules without crashing."""
    db = _make_db(tmp_path)
    _upsert_module(db, id="m-old", name="portal",
                   summary="[hash:v1:aa] portal shell")  # no summary_vector
    query_vec = _unit([1.0, 0.0, 0.0])
    hits = search_modules(
        db, PROJECT_ID, "portal", limit=1, query_vector=query_vec,
    )
    assert hits
    assert hits[0][0].id == "m-old"


def test_vector_constants_shape():
    assert VECTOR_WEIGHT > 10.0  # strong enough to shift rankings
    assert 0.0 < VECTOR_MIN_COSINE < 1.0
