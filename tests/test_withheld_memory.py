"""Records that own nothing stay out of the index (2026-09-23).

A qa log with no answer of its own — the record written when the question
arrived — and a memory card whose summary is a metrics block keep their
file on disk but get no chunk. An index built before the gate is purged
once, by deletion only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.index import doc_chunker, pipeline as pipeline_mod
from hybrid_search.index.doc_chunker import chunk_doc_file, is_withheld_memory
from hybrid_search.memory.qa_log import QARecord, _format_record
from hybrid_search.project import project_hash
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine

from tests.test_pipeline import _make_pipeline


def _qa(answer: str | None, *, results: list[dict] | None = None) -> str:
    return _format_record(QARecord(
        query="배치 작업은 언제 재시도하나",
        query_type="TURN",
        effective_bm25_weight=0.0,
        query_time_ms=0.0,
        total_chunks_searched=0,
        results=results or [],
        timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        project_root=Path("/tmp/demo"),
        trigger="stop_hook" if answer else "user_prompt_submit",
        answer_chars=len(answer) if answer else None,
        answer_excerpt=answer,
    ))


ANSWERED = _qa("실패한 배치는 지수 백오프로 세 번까지 재시도한다.")
QUESTION_ONLY = _qa(None, results=[{
    "chunk_id": "c1",
    "file_path": ".hybrid-search/qa/2026/09/01-000000-aaaa.md",
    "snippet": "[qa - stop_hook] " + ANSWERED[ANSWERED.index("- **answer_chars**"):],
}])

CARD_TEMPLATE = """---
type: memory_card
query: "배치 재시도"
---

## Summary

{summary}

## When to use

Use when answering follow-up questions related to: 배치 재시도
"""
METADATA_CARD = CARD_TEMPLATE.format(
    summary="- **query_type**: TURN - **bm25_weight**: 0.0 - **time**: 0.0 ms")
PROSE_CARD = CARD_TEMPLATE.format(summary="실패한 배치는 세 번까지 재시도한다.")

QA_ANSWERED = ".hybrid-search/qa/2026/09/01-000001-bbbb.md"
QA_QUESTION = ".hybrid-search/qa/2026/09/01-000000-cccc.md"


class TestPredicate:
    def test_qa_logs(self):
        assert not is_withheld_memory(QA_ANSWERED, ANSWERED)
        assert is_withheld_memory(QA_QUESTION, QUESTION_ONLY)

    def test_cards(self):
        card = ".hybrid-search/memory/cards/2026/04/28-000000-dddd.md"
        assert is_withheld_memory(card, METADATA_CARD)
        assert not is_withheld_memory(card, PROSE_CARD)

    def test_other_documents_are_never_withheld(self):
        assert not is_withheld_memory("docs/notes.md", QUESTION_ONLY)

    def test_windows_separators(self):
        assert is_withheld_memory(QA_QUESTION.replace("/", "\\"), QUESTION_ONLY)


def _write(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_chunker_gives_withheld_records_no_chunk(tmp_path: Path) -> None:
    ans = _write(tmp_path, QA_ANSWERED, ANSWERED)
    q = _write(tmp_path, QA_QUESTION, QUESTION_ONLY)
    assert len(chunk_doc_file(ans, tmp_path, "p", "markdown")) == 1
    assert chunk_doc_file(q, tmp_path, "p", "markdown") == []


class _Index:
    """Open the stores of an indexed repo for inspection."""

    def __init__(self, config, repo: Path) -> None:
        self.pid = project_hash(str(repo.resolve()))
        self.paths = IndexPaths(get_project_dir(config.projects_dir, self.pid))

    def qa_chunks(self) -> list[str]:
        db = StoreDB(self.paths.store_db)
        try:
            return [c.id for c in db.get_chunks_by_node_type(self.pid, "qa_log")]
        finally:
            db.close()

    def file_row(self, rel: str):
        db = StoreDB(self.paths.store_db)
        try:
            return db.get_file_by_path(self.pid, rel)
        finally:
            db.close()

    def engine_counts(self) -> tuple[int, int]:
        vec = VectorEngine(self.paths.vectors_dir, 8)
        bm25 = BM25Engine(self.paths.tantivy_dir)
        return vec.count, bm25.count

    def clear_marker(self) -> None:
        db = StoreDB(self.paths.store_db)
        try:
            with db.transaction() as conn:
                conn.execute("DELETE FROM index_meta WHERE key = ?",
                             (pipeline_mod.MEMORY_GATE_KEY,))
        finally:
            db.close()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _write(repo, QA_ANSWERED, ANSWERED)
    _write(repo, QA_QUESTION, QUESTION_ONLY)
    return repo


def test_new_withheld_record_gets_a_row_and_no_chunk(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pipe, config = _make_pipeline(tmp_path)
    pipe.index_project(str(repo))
    idx = _Index(config, repo)

    assert len(idx.qa_chunks()) == 1
    row = idx.file_row(QA_QUESTION)
    assert row is not None and row.chunk_count == 0 and row.file_hash

    # The hash is on record, so the next scan does not pick the file up again.
    again = pipe.index_project(str(repo))
    assert again.files_added == 0 and again.files_changed == 0


def test_chunks_of_a_file_that_now_yields_none_are_removed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    pipe, config = _make_pipeline(tmp_path)
    pipe.index_project(str(repo))
    idx = _Index(config, repo)
    assert len(idx.qa_chunks()) == 1

    # The answered record's file is rewritten as a question-only one.
    _write(repo, QA_ANSWERED, QUESTION_ONLY)
    pipe.index_project(str(repo))
    assert idx.qa_chunks() == []


def test_index_built_before_the_gate_is_purged_once(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    pipe, config = _make_pipeline(tmp_path)
    idx = _Index(config, repo)

    # Build the index as the code before the gate did.
    with monkeypatch.context() as m:
        m.setattr(doc_chunker, "is_withheld_memory", lambda *a: False)
        m.setattr(pipeline_mod, "is_withheld_memory", lambda *a: False)
        pipe.index_project(str(repo))
    assert len(idx.qa_chunks()) == 2
    vec_before, bm25_before = idx.engine_counts()
    idx.clear_marker()

    # Files unchanged — only the one-time purge can remove the chunk.
    result = pipe.index_project(str(repo))
    assert result.files_added == 0 and result.files_changed == 0
    assert len(idx.qa_chunks()) == 1
    assert idx.file_row(QA_QUESTION).chunk_count == 0
    assert idx.engine_counts() == (vec_before - 1, bm25_before - 1)

    # Idempotent: the marker is set, nothing further moves.
    pipe.index_project(str(repo))
    assert len(idx.qa_chunks()) == 1
    assert idx.engine_counts() == (vec_before - 1, bm25_before - 1)
