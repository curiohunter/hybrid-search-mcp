"""F11 complete fix — the recency fast path.

Meta-recall questions ("방금 뭐 얘기했지?") carry no topical tokens, so
similarity retrieval structurally cannot answer them — it matches past
instances of the question itself (round-3 verdict: core-claim failure,
must close in this PR). Time order can answer them: the newest CONTENT
records lead the response, deterministically, with empty/interrupted
turns and meta-recall records excluded.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hybrid_search.project import ProjectInfo
from hybrid_search.search.orchestrator import (
    HybridResult,
    SearchOrchestrator,
    _filter_conv_noise,
    _is_empty_turn,
)
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


# --- empty-turn detection -------------------------------------------------------

class TestEmptyTurn:
    @pytest.mark.parametrize("content,empty", [
        ("[Request interrupted by user for tool use]", True),
        ("[claude turn] [Request interrupted by user]", True),
        ("ok", True),
        ("", True),
        (None, True),
        ("[claude turn] 소개 관계 그래프의 student_referrals 테이블 설계를 논의했고 "
         "마이그레이션 파일 경로를 확정했습니다.", False),
        ("[in-flight conversation - claude] PaysSam 정산 연동은 lib/payssam-client.ts에서 "
         "처리되고 webhook 동기화 흐름이 있습니다.", False),
    ])
    def test_detection(self, content, empty) -> None:
        assert _is_empty_turn(content) is empty

    def test_filter_drops_noise_keeps_content(self) -> None:
        def conv(cid, text):
            return HybridResult(
                chunk_id=cid, rrf_score=0.02, bm25_rank=1, vector_rank=1,
                file_path=f".conversations/claude/{cid}.jsonl", project="p",
                name=cid, qualified_name=cid, node_type="conv_turn",
                start_line=None, end_line=None, content=text, snippet="s",
            )
        noise = conv("noise", "[claude turn] [Request interrupted by user for tool use]")
        content = conv("real", "[claude turn] referral 그래프 로스터 마이그레이션을 커밋했고 "
                               "다음은 스냅샷 컬럼 작업입니다.")
        out = _filter_conv_noise([noise, content])
        assert [r.chunk_id for r in out] == ["real"]


# --- data source + head assembly ---------------------------------------------------

QA_META = (
    '---\nquery: "방금 Claude랑 뭐 얘기했지?"\ntimestamp: {ts}\n'
    "trigger: codex_stop_hook\nanswer_chars: 100\nanswer_excerpt_chars: 100\n---\n\n"
    "## Answer excerpt\n\n방금 PaysSam 얘기했어요 (화석화된 답)\n\n## Top results\n"
)
QA_CONTENT = (
    '---\nquery: "{q}"\ntimestamp: {ts}\n'
    "trigger: stop_hook\nanswer_chars: 200\nanswer_excerpt_chars: 200\n---\n\n"
    "## Answer excerpt\n\n{answer}\n\n## Top results\n"
)
QA_QUESTION_ONLY = (
    '---\nquery: "{q}"\ntimestamp: {ts}\ntrigger: user_prompt_submit\n---\n\n'
    "## Top results\n\n### 1. `src/x.ts:1-2` — n\n"
)


@pytest.fixture()
def env(tmp_path: Path):
    """A project with qa files + an indexed conv store."""
    root = tmp_path / "proj"
    qa_dir = root / ".hybrid-search" / "qa" / "2026" / "07"
    qa_dir.mkdir(parents=True)

    def qa(name: str, text: str) -> None:
        (qa_dir / name).write_text(text, encoding="utf-8")

    # newest → oldest by timestamp
    qa("27-100000-newest.md", QA_CONTENT.format(
        q="소개 관계 그래프 로스터 어떻게 저장해?", ts="2026-07-27T10:00:00+00:00",
        answer="referral_network_roster 마이그레이션과 services/referral/network.ts로 처리합니다.",
    ))
    qa("27-090000-meta.md", QA_META.format(ts="2026-07-27T09:00:00+00:00"))
    qa("27-080000-qonly.md", QA_QUESTION_ONLY.format(
        q="스냅샷 컬럼 설계", ts="2026-07-27T08:00:00+00:00",
    ))
    qa("26-140000-old.md", QA_CONTENT.format(
        q="payssam 정산 어디서 처리해?", ts="2026-07-26T14:00:00+00:00",
        answer="lib/payssam-client.ts와 webhook route에서 처리합니다.",
    ))

    config = MagicMock()
    config.projects_dir = tmp_path / "data" / "projects"
    pinfo = ProjectInfo(
        id="p1", name="proj", path=str(root),
        last_indexed_at=None, file_count=1, chunk_count=1,
    )
    idx = IndexPaths(get_project_dir(config.projects_dir, pinfo.id))
    db = StoreDB(idx.store_db)
    with db.transaction() as conn:
        db.upsert_file(conn, FileRecord(
            id="cf", project_id="p1",
            relative_path=".conversations/claude/s1.jsonl", file_hash="h",
        ))
        db.insert_chunks(conn, [
            ChunkRecord(id="conv-new", file_id="cf", project_id="p1",
                        node_type="conv_turn", qualified_name="claude:s1#2",
                        content="[claude turn] 소개 관계 그래프 시각화 UI에서 "
                                "노드 색상은 소개 단계별로 구분하기로 결정했습니다."),
            ChunkRecord(id="conv-empty", file_id="cf", project_id="p1",
                        node_type="conv_turn", qualified_name="claude:s1#3",
                        content="[claude turn] [Request interrupted by user for tool use]"),
            ChunkRecord(id="conv-meta", file_id="cf", project_id="p1",
                        node_type="conv_turn", qualified_name="codex:s2#0",
                        content="[codex turn] 방금 Claude랑 뭐 얘기했지? ..."),
            ChunkRecord(id="conv-old", file_id="cf", project_id="p1",
                        node_type="conv_turn", qualified_name="claude:s1#0",
                        content="[claude turn] j-credit 정산 로직은 서비스 레이어에서 "
                                "멱등 처리하는 구조로 정리했습니다."),
        ])
        conn.executemany(
            "INSERT INTO conversation_meta (chunk_id, project_id, source, "
            "session_id, turn_index, ts) VALUES (?, 'p1', ?, 's', 0, ?)",
            [
                ("conv-new", "claude", "2026-07-27T11:00:00+00:00"),
                ("conv-empty", "claude", "2026-07-27T12:00:00+00:00"),
                ("conv-meta", "codex", "2026-07-27T11:30:00+00:00"),
                ("conv-old", "claude", "2026-07-26T13:00:00+00:00"),
            ],
        )
    db.close()

    orch = SearchOrchestrator(config, MagicMock(), MagicMock())
    return orch, pinfo


class TestRecentActivityHead:
    def test_newest_content_first_noise_excluded(self, env) -> None:
        orch, pinfo = env
        head = orch._recent_activity_results(pinfo, "방금 뭐 얘기했지?")
        ids = [r.chunk_id for r in head]
        # Empty turn (12:00, newest!) and meta-recall records must be
        # excluded; newest CONTENT leads.
        assert "conv-empty" not in ids
        assert "conv-meta" not in ids
        assert not any("meta" in i for i in ids)
        assert ids[0] == "conv-new"                      # 11:00 conv content
        assert ids[1] == "recent:qa:27-100000-newest"    # 10:00 qa answer
        # question-only qa excluded
        assert not any("qonly" in i for i in ids)
        # older content still present, after newer
        assert ids.index("conv-new") < ids.index("recent:qa:26-140000-old")

    def test_head_capped_and_time_ordered(self, env) -> None:
        orch, pinfo = env
        head = orch._recent_activity_results(pinfo, "최근 작업 알려줘")
        assert len(head) <= orch._RECENT_ACTIVITY_HEAD
        times = [r.file_mtime or "" for r in head]
        assert times == sorted(times, reverse=True)

    def test_deterministic_lane_has_no_similarity_footprint(self, env) -> None:
        """rrf 0.0 + no lane ranks — the head must not leak into the
        similarity-based confidence inputs (ranked filters rrf > 0)."""
        orch, pinfo = env
        head = orch._recent_activity_results(pinfo, "방금 뭐 얘기했지?")
        assert head and all(
            r.rrf_score == 0.0 and r.bm25_rank is None and r.vector_rank is None
            for r in head
        )
        assert all("recent-activity" in (r.trust_meta or "") for r in head)

    def test_fail_open_on_missing_everything(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.projects_dir = tmp_path / "nope"
        orch = SearchOrchestrator(config, MagicMock(), MagicMock())
        pinfo = ProjectInfo(
            id="px", name="px", path=str(tmp_path / "empty"),
            last_indexed_at=None, file_count=0, chunk_count=0,
        )
        assert orch._recent_activity_results(pinfo, "방금 뭐 했지") == []
