"""R1 fix tests — index-time qa supersession mapping + query-time splice.

The ripgrep holdout R1 failure: a probe phrased like the OBSOLETE answer
puts the stale qa at #1 while the correction never enters the candidate
set (corpus crowding). Query-time recency ordering can't help — it only
sees retrieved chunks. These tests cover the three layers of the fix:

- ``memory.supersession.compute_supersession`` — corpus-wide grouping,
  newest-by-frontmatter-timestamp wins, refuses to guess when undated.
- ``StoreDB`` qa_supersession table — replace/get roundtrip semantics.
- ``orchestrator._splice_superseding`` — the correction lands directly
  above the stale hit, the stale hit is marked, list length and
  non-qa entries stay untouched.
"""

from __future__ import annotations

import pytest

from hybrid_search.memory.supersession import compute_supersession
from hybrid_search.search.orchestrator import (
    HybridResult,
    _SUPERSEDED_MARK,
    _splice_superseding,
)
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB


def _qa_content(query: str, answer: str, timestamp: str | None) -> str:
    ts_line = f'timestamp: {timestamp}\n' if timestamp else ""
    return (
        "---\n"
        f'query: "{query}"\n'
        f"{ts_line}"
        "trigger: stop_hook\n"
        "---\n\n"
        f"# Q: {query}\n\n"
        "## Answer excerpt\n\n"
        f"{answer}\n\n"
        "## Top results\n"
    )


OLD_QA = _qa_content(
    "ripgrep max-columns 기본값이 뭐지?",
    "ripgrep max_columns 기본값은 team config 기준 150입니다.",
    "2026-07-01T10:00:00+00:00",
)
NEW_QA = _qa_content(
    "ripgrep max-columns 기본값 바뀌었나?",
    "ripgrep max_columns 기본값은 team config 기준 300으로 변경되었습니다.",
    "2026-07-10T10:00:00+00:00",
)
NEWEST_QA = _qa_content(
    "ripgrep max-columns 기본값 최종 확인",
    "ripgrep max_columns 기본값은 team config 기준 500이 최종입니다.",
    "2026-07-12T10:00:00+00:00",
)
ADJACENT_QA = _qa_content(
    "ripgrep hidden 파일 검색은 어떻게 설정해?",
    "hidden 파일은 --hidden 플래그와 glob ignore 규칙으로 제어합니다.",
    "2026-07-11T10:00:00+00:00",
)


# --- compute_supersession ---------------------------------------------------

class TestComputeSupersession:
    def test_same_topic_old_maps_to_new(self) -> None:
        mapping = compute_supersession([("old", OLD_QA), ("new", NEW_QA)])
        assert mapping == {"old": "new"}

    def test_adjacent_topic_never_grouped(self) -> None:
        mapping = compute_supersession([("old", OLD_QA), ("adj", ADJACENT_QA)])
        assert mapping == {}

    def test_three_member_group_maps_to_newest(self) -> None:
        mapping = compute_supersession(
            [("old", OLD_QA), ("newest", NEWEST_QA), ("new", NEW_QA)]
        )
        assert mapping == {"old": "newest", "new": "newest"}

    def test_input_order_does_not_matter(self) -> None:
        a = compute_supersession([("old", OLD_QA), ("new", NEW_QA)])
        b = compute_supersession([("new", NEW_QA), ("old", OLD_QA)])
        assert a == b == {"old": "new"}

    def test_all_undated_group_refuses_to_guess(self) -> None:
        old = _qa_content(
            "ripgrep max-columns 기본값이 뭐지?",
            "ripgrep max_columns 기본값은 team config 기준 150입니다.",
            None,
        )
        new = _qa_content(
            "ripgrep max-columns 기본값 바뀌었나?",
            "ripgrep max_columns 기본값은 team config 기준 300으로 변경되었습니다.",
            None,
        )
        assert compute_supersession([("old", old), ("new", new)]) == {}

    def test_undated_member_maps_to_newest_dated(self) -> None:
        undated = _qa_content(
            "ripgrep max-columns 기본값이 뭐지?",
            "ripgrep max_columns 기본값은 team config 기준 150입니다.",
            None,
        )
        mapping = compute_supersession([("undated", undated), ("new", NEW_QA)])
        assert mapping == {"undated": "new"}

    def test_single_or_empty_corpus(self) -> None:
        assert compute_supersession([]) == {}
        assert compute_supersession([("only", OLD_QA)]) == {}

    def test_answer_only_overlap_is_not_enough_corpus_wide(self) -> None:
        """2026-07-15 field check: long assistant answers about one project
        share vocabulary; the query-time answer-only path over-groups when
        applied corpus-wide. Index-time requires question agreement."""
        vague_a = _qa_content(
            "진행해줘",
            "memory layer confidence 게이트는 classify_confidence 함수와 "
            "corpus_absent 캡, fallback_hint 계약으로 처리합니다.",
            "2026-07-01T10:00:00+00:00",
        )
        vague_b = _qa_content(
            "다음 단계가 뭐지?",
            "memory layer confidence 게이트는 classify_confidence 함수와 "
            "corpus_absent 캡, fallback_hint 계약이 핵심입니다.",
            "2026-07-10T10:00:00+00:00",
        )
        assert compute_supersession([("a", vague_a), ("b", vague_b)]) == {}

    def test_short_imperative_turns_never_group(self) -> None:
        """Imperative turns share one command word and tiny token sets;
        the answers must not supply the grouping evidence."""
        cmd_a = _qa_content(
            "커밋 하고 푸시까지 진행해줘",
            "hybrid search supersession splice 구현을 커밋했고 orchestrator "
            "confidence 계약 테스트까지 통과했습니다.",
            "2026-07-01T10:00:00+00:00",
        )
        cmd_b = _qa_content(
            "진행해",
            "hybrid search supersession splice 구현과 orchestrator confidence "
            "계약 회귀까지 마무리했습니다.",
            "2026-07-10T10:00:00+00:00",
        )
        assert compute_supersession([("a", cmd_a), ("b", cmd_b)]) == {}

    def test_machine_payload_queries_are_skipped(self) -> None:
        notif_old = _qa_content(
            "<task-notification> task-id abc completed background command",
            "Background command ripgrep holdout completed with exit code 0.",
            "2026-07-01T10:00:00+00:00",
        )
        notif_new = _qa_content(
            "<task-notification> task-id xyz completed background command",
            "Background command ripgrep holdout completed with exit code 0.",
            "2026-07-10T10:00:00+00:00",
        )
        assert compute_supersession(
            [("n-old", notif_old), ("n-new", notif_new)]
        ) == {}


# --- StoreDB qa_supersession -------------------------------------------------

class TestQaSupersessionStore:
    def test_replace_and_get_roundtrip(self, tmp_path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", {"old": "new", "o2": "new"})
            assert db.get_qa_superseding(["old", "o2", "unrelated"]) == {
                "old": "new", "o2": "new",
            }
        finally:
            db.close()

    def test_replace_clears_previous_rows(self, tmp_path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", {"old": "new"})
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", {"o2": "n2"})
            assert db.get_qa_superseding(["old", "o2"]) == {"o2": "n2"}
        finally:
            db.close()

    def test_empty_lookup(self, tmp_path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            assert db.get_qa_superseding([]) == {}
        finally:
            db.close()

    def test_survives_reopen(self, tmp_path) -> None:
        db = StoreDB(tmp_path / "store.db")
        with db.transaction() as conn:
            db.replace_qa_supersession(conn, "p1", {"old": "new"})
        db.close()
        db = StoreDB(tmp_path / "store.db")
        try:
            assert db.get_qa_superseding(["old"]) == {"old": "new"}
        finally:
            db.close()


# --- _splice_superseding ------------------------------------------------------

def _mk(
    chunk_id: str,
    node_type: str = "qa_log",
    score: float = 0.02,
    trust_meta: str | None = "[qa - 10d ago]",
) -> HybridResult:
    return HybridResult(
        chunk_id=chunk_id,
        rrf_score=score,
        bm25_rank=1,
        vector_rank=1,
        file_path=f".hybrid-search/qa/{chunk_id}.md",
        project="p",
        name=chunk_id,
        qualified_name=chunk_id,
        node_type=node_type,
        start_line=1,
        end_line=10,
        content="body",
        snippet="snippet",
        trust_meta=trust_meta,
    )


class TestSpliceSuperseding:
    def test_full_list_replaces_stale_and_keeps_code_slots(self) -> None:
        """Round-1 slot invariant: at full top-N the correction takes the
        stale slot; code hits never lose theirs."""
        stale = _mk("old")
        results = [stale, _mk("code-1", node_type="function"), _mk("code-2", node_type="function")]
        out = _splice_superseding(
            results, {"old": "new"}, lambda nid, s: _mk(nid, score=s.rrf_score),
            limit=3,
        )
        assert [r.chunk_id for r in out] == ["new", "code-1", "code-2"]

    def test_spare_capacity_inserts_and_keeps_marked_stale(self) -> None:
        results = [_mk("old"), _mk("code", node_type="function")]
        out = _splice_superseding(
            results, {"old": "new"}, lambda nid, s: _mk(nid), limit=5,
        )
        assert [r.chunk_id for r in out] == ["new", "old", "code"]
        stale = next(r for r in out if r.chunk_id == "old")
        assert _SUPERSEDED_MARK in (stale.trust_meta or "")
        assert stale.snippet.startswith(_SUPERSEDED_MARK)

    def test_spliced_result_inherits_stale_score(self) -> None:
        results = [_mk("old", score=0.0173), _mk("code", node_type="function")]
        out = _splice_superseding(
            results, {"old": "new"}, lambda nid, s: _mk(nid, score=s.rrf_score),
            limit=2,
        )
        assert out[0].chunk_id == "new"
        assert out[0].rrf_score == pytest.approx(0.0173)

    def test_target_retrieved_above_marks_stale_without_splice(self) -> None:
        """The newest answer surfaced on its own above the stale one —
        no splice, but the stale hit still carries the superseded mark."""
        results = [_mk("new"), _mk("old")]
        out = _splice_superseding(results, {"old": "new"}, lambda nid, s: _mk(nid))
        assert [r.chunk_id for r in out] == ["new", "old"]
        assert _SUPERSEDED_MARK in (out[1].trust_meta or "")

    def test_target_retrieved_below_is_noop(self) -> None:
        """Target present but ranked BELOW the stale hit: query-time
        recency ordering owns that case; marking would contradict the
        visible order."""
        results = [_mk("old"), _mk("new")]
        out = _splice_superseding(results, {"old": "new"}, lambda nid, s: _mk(nid))
        assert out is results

    def test_fetch_miss_is_noop(self) -> None:
        results = [_mk("old")]
        out = _splice_superseding(results, {"old": "gone"}, lambda nid, s: None)
        assert out is results

    def test_cap_bounds_number_of_splices(self) -> None:
        results = [_mk("o1"), _mk("o2"), _mk("o3")]
        out = _splice_superseding(
            results,
            {"o1": "n1", "o2": "n2", "o3": "n3"},
            lambda nid, s: _mk(nid),
            cap=2,
            limit=3,
        )
        # Full list → replacements; cap stops after two.
        assert [r.chunk_id for r in out] == ["n1", "n2", "o3"]

    def test_non_qa_hit_never_spliced(self) -> None:
        results = [_mk("old", node_type="function")]
        out = _splice_superseding(results, {"old": "new"}, lambda nid, s: _mk(nid))
        assert out is results

    def test_empty_mapping_is_identity(self) -> None:
        results = [_mk("old")]
        assert _splice_superseding(results, {}, lambda nid, s: _mk(nid)) is results


# --- end-to-end through the store (R1 shape) ----------------------------------

class TestR1EndToEnd:
    """Replicates the holdout R1 shape at the storage layer: the stale qa
    is 'retrieved' (mapping lookup by its chunk_id) while the correction
    exists only in the store — the splice must materialise it."""

    def _seed(self, db: StoreDB) -> None:
        with db.transaction() as conn:
            db.upsert_file(conn, FileRecord(
                id="f-old", project_id="p1",
                relative_path=".hybrid-search/qa/2026/07/01-old.md",
                file_hash="h1", file_mtime="2026-07-01T10:00:00+00:00",
            ))
            db.upsert_file(conn, FileRecord(
                id="f-new", project_id="p1",
                relative_path=".hybrid-search/qa/2026/07/10-new.md",
                file_hash="h2", file_mtime="2026-07-10T10:00:00+00:00",
            ))
            db.insert_chunks(conn, [
                ChunkRecord(id="qa-old", file_id="f-old", project_id="p1",
                            name="01-old", node_type="qa_log", content=OLD_QA),
                ChunkRecord(id="qa-new", file_id="f-new", project_id="p1",
                            name="10-new", node_type="qa_log", content=NEW_QA),
            ])

    def test_mapping_pass_then_lookup(self, tmp_path) -> None:
        db = StoreDB(tmp_path / "store.db")
        try:
            self._seed(db)
            chunks = db.get_chunks_by_node_type("p1", "qa_log")
            mapping = compute_supersession([(c.id, c.content or "") for c in chunks])
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", mapping)
            # Query time: only the stale chunk was retrieved.
            superseding = db.get_qa_superseding(["qa-old"])
            assert superseding == {"qa-old": "qa-new"}
            newer = db.get_chunk(superseding["qa-old"])
            assert newer is not None and "300" in (newer.content or "")
        finally:
            db.close()

    def test_stale_only_rate_zero_after_splice(self, tmp_path) -> None:
        """R1-T2: whenever a superseded qa is shown, its correction shows
        above it — the worst-case 'stale only' presentation is gone."""
        db = StoreDB(tmp_path / "store.db")
        try:
            self._seed(db)
            chunks = db.get_chunks_by_node_type("p1", "qa_log")
            mapping = compute_supersession([(c.id, c.content or "") for c in chunks])
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, "p1", mapping)

            results = [_mk("qa-old"), _mk("code-1", node_type="function")]

            def fetch(nid: str, stale: HybridResult) -> HybridResult | None:
                chunk = db.get_chunk(nid)
                if chunk is None:
                    return None
                return _mk(nid, score=stale.rrf_score)

            mapping = db.get_qa_superseding(["qa-old"])
            # Spare capacity: correction above the marked stale hit.
            out = _splice_superseding(results, mapping, fetch, limit=5)
            ids = [r.chunk_id for r in out]
            assert ids.index("qa-new") < ids.index("qa-old")
            # Full list: correction REPLACES the stale hit — stale-only
            # is impossible either way.
            out_full = _splice_superseding(results, mapping, fetch, limit=2)
            ids_full = [r.chunk_id for r in out_full]
            assert "qa-new" in ids_full and "qa-old" not in ids_full
        finally:
            db.close()
