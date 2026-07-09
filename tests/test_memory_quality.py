"""Memory quality gate (P3) — junk filtering, near-dup skip, hook relevance."""

from __future__ import annotations

from pathlib import Path

import pytest

from hybrid_search.memory import reader
from hybrid_search.memory.qa_log import record_turn
from hybrid_search.memory.quality import is_junk_query, jaccard, query_tokens


class TestIsJunkQuery:
    @pytest.mark.parametrize("query", [
        "",
        None,
        "───────────────────────────────",
        "• 반영했고 실측까지 끝냈습니다.",
        '"• 반영했고 실측까지 끝냈습니다.',
        "<task-notification>",
        "Another Claude session sent a message:",
        "[SYSTEM NOTIFICATION - NOT USER INPUT] something",
        "src/hybrid_search/memory/qa_log.py",
        "╭─────────────────────────────╮",
        "# Q: something",
        "> quoted output line",
    ])
    def test_junk_detected(self, query) -> None:
        assert is_junk_query(query) is True

    @pytest.mark.parametrize("query", [
        "밸류인 프로젝트에서 confidence가 왜 weak으로 나와?",
        "how does the conversation indexer work",
        "벤브 아냐 ?",
        "wiki 생성 인덱스 구조 설명해줘",
        "index.md 어디서 갱신돼?",  # path plus actual question — not path-only
    ])
    def test_real_questions_kept(self, query: str) -> None:
        assert is_junk_query(query) is False


class TestNearDupTokens:
    def test_jaccard_identical(self) -> None:
        a = query_tokens("대화 인덱싱 흐름 설명해줘")
        assert jaccard(a, a) == 1.0

    def test_jaccard_disjoint(self) -> None:
        assert jaccard(query_tokens("wiki 구조"), query_tokens("billing flow")) == 0.0

    def test_empty_is_zero(self) -> None:
        assert jaccard(set(), query_tokens("anything")) == 0.0


def _seed_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".hybrid-search").mkdir(parents=True)
    return root


class TestRecordTurnGate:
    def test_junk_turn_not_saved(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "1")
        root = _seed_project(tmp_path)
        path = record_turn(
            query="─────────────────────",
            cwd=str(root),
            project_infos=[],
        )
        assert path is None

    def test_real_turn_saved(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "1")
        root = _seed_project(tmp_path)
        path = record_turn(
            query="confidence가 왜 weak으로 나오는지 알려줘",
            cwd=str(root),
            project_infos=[],
        )
        assert path is not None and path.exists()

    def test_near_dup_turn_skipped(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "1")
        root = _seed_project(tmp_path)
        first = record_turn(
            query="confidence가 왜 weak으로 나오는지 알려줘 자세히",
            cwd=str(root),
            project_infos=[],
            dedup=False,  # bypass the 5s exact-hash window; test the token path
        )
        assert first is not None
        second = record_turn(
            # Same tokens, different punctuation/spacing — exact hash differs.
            query="confidence가  왜 weak으로 나오는지 알려줘 자세히!",
            cwd=str(root),
            project_infos=[],
        )
        assert second is None

    def test_different_question_not_skipped(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HYBRID_SEARCH_QA_LOG", "1")
        root = _seed_project(tmp_path)
        record_turn(
            query="confidence가 왜 weak으로 나오는지 알려줘",
            cwd=str(root), project_infos=[], dedup=False,
        )
        other = record_turn(
            query="대화 인덱서 Stop 훅은 어디서 스폰돼?",
            cwd=str(root), project_infos=[],
        )
        assert other is not None


class TestGrepQaQueries:
    def _write_qa(self, root: Path, stem: str, query: str, body: str) -> None:
        qa = root / ".hybrid-search" / "qa" / "2026" / "07"
        qa.mkdir(parents=True, exist_ok=True)
        (qa / f"{stem}.md").write_text(
            f'---\nquery: "{query}"\nquery_type: "TURN"\ntimestamp: "2026-07-09T00:00:00+00:00"\n---\n\n{body}\n'
        )

    def test_matches_question_field_only(self, tmp_path: Path) -> None:
        root = _seed_project(tmp_path)
        # Question mentions the file — relevant.
        self._write_qa(root, "09-000001-aaaa0001", "scanner.py 스캔 순서 설명", "body")
        # Only the body mentions the file (tool log) — irrelevant.
        self._write_qa(
            root, "09-000002-aaaa0002", "완전히 다른 질문",
            "tool log: Read src/hybrid_search/index/scanner.py",
        )
        hits = list(reader.grep_qa_queries(root, "scanner.py"))
        assert len(hits) == 1
        assert "스캔 순서" in hits[0].index.query
