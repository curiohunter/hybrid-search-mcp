"""A4 — conversation indexer: external transcripts → unified stores.

Conv turns are embedded and written to SQLite (node_type='conv_turn') +
conversation_meta + BM25 + vector, so the project-wide
chunk==vector==bm25 invariant holds. A reserved ``.conversations/`` file
namespace keeps a full project rescan from deleting them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from hybrid_search.config import Config, EmbeddingConfig
from hybrid_search.index.conversation_indexer import ConversationIndexer
from hybrid_search.index.scanner import scan_project
from hybrid_search.index.transcript_source import claude_slug_for
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


class _FakeEmbedder:
    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.embedded = 0

    @property
    def embedding_dim(self) -> int:
        return self._dim

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        self.embedded += len(texts)
        return np.ones((len(texts), self._dim), dtype=np.float32)


def _write_claude(claude_root: Path, project_path: Path, text: str, session: str = "s1") -> Path:
    d = claude_root / claude_slug_for(project_path)
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"role": "user", "content": text},
         "timestamp": "2026-04-29T04:59:35Z", "cwd": str(project_path)},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "고쳤습니다"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/hook_runtime.py"}},
        ]}},
    ]
    path = d / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _write_codex(codex_root: Path, project_path: Path, session: str = "rollout-x") -> Path:
    d = codex_root / "2026" / "05" / "04"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session_meta", "payload": {"id": "x", "cwd": str(project_path)}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "git 루트 분리해줘"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "분리했습니다"}]}},
        {"type": "response_item", "payload": {"type": "function_call",
         "name": "exec_command", "arguments": json.dumps({"cmd": "git init"})}},
    ]
    path = d / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return path


def _setup(tmp_path: Path) -> tuple[ConversationIndexer, Config, Path, _FakeEmbedder, dict]:
    project = tmp_path / "proj"
    project.mkdir()
    config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig(batch_size=8))
    registry = ProjectRegistry(config.global_dir)
    embedder = _FakeEmbedder()
    indexer = ConversationIndexer(config, registry, embedder)
    roots = {"claude_root": tmp_path / "claude", "codex_root": tmp_path / "codex"}
    return indexer, config, project, embedder, roots


def _engines(config: Config, project: Path, dim: int = 8) -> tuple[StoreDB, BM25Engine, VectorEngine, str]:
    pid = project_hash(str(project.resolve()))
    paths = IndexPaths(get_project_dir(config.projects_dir, pid))
    db = StoreDB(paths.store_db)
    return db, BM25Engine(paths.tantivy_dir), VectorEngine(paths.vectors_dir, dim), pid


def test_index_conversations_writes_all_stores(tmp_path: Path) -> None:
    indexer, config, project, embedder, roots = _setup(tmp_path)
    _write_claude(roots["claude_root"], project, "hook cwd 버그 고쳐줘")
    _write_codex(roots["codex_root"], project)

    result = indexer.index_conversations(str(project), **roots)
    assert result.chunks_total > 0
    assert result.sessions_indexed == 2  # one claude, one codex

    db, bm25, vector, pid = _engines(config, project)
    try:
        chunk_count = db.get_chunk_count(pid)
        assert chunk_count == result.chunks_total
        # Unified-store invariant: all three stores agree.
        assert vector.count == chunk_count
        assert bm25.count == chunk_count
        # Every conv chunk has metadata, both sources present.
        conv_chunks = [c for c in db.get_chunks_by_project(pid) if c.node_type == "conv_turn"]
        assert len(conv_chunks) == chunk_count
        metas = db.get_conversation_meta_batch([c.id for c in conv_chunks])
        assert {m.source for m in metas.values()} == {"claude", "codex"}
    finally:
        db.close()


def test_idempotent_run_skips_unchanged(tmp_path: Path) -> None:
    indexer, config, project, embedder, roots = _setup(tmp_path)
    _write_claude(roots["claude_root"], project, "hook cwd 버그 고쳐줘")
    indexer.index_conversations(str(project), **roots)
    embedded_after_first = embedder.embedded

    result2 = indexer.index_conversations(str(project), **roots)
    assert result2.sessions_indexed == 0
    assert result2.sessions_skipped == 1
    # Unchanged session must not be re-embedded.
    assert embedder.embedded == embedded_after_first


def test_changed_session_reindexes(tmp_path: Path) -> None:
    indexer, config, project, embedder, roots = _setup(tmp_path)
    _write_claude(roots["claude_root"], project, "old question")
    indexer.index_conversations(str(project), **roots)

    db, _, _, pid = _engines(config, project)
    first_ids = {c.id for c in db.get_chunks_by_project(pid)}
    db.close()

    _write_claude(roots["claude_root"], project, "completely different question now")
    result = indexer.index_conversations(str(project), **roots)
    assert result.sessions_indexed == 1

    db, bm25, vector, pid = _engines(config, project)
    try:
        new_ids = {c.id for c in db.get_chunks_by_project(pid)}
        assert new_ids != first_ids  # content hash changed → new chunk ids
        assert vector.count == db.get_chunk_count(pid) == bm25.count
    finally:
        db.close()


def test_full_scan_does_not_delete_conv_files(tmp_path: Path) -> None:
    indexer, config, project, embedder, roots = _setup(tmp_path)
    _write_claude(roots["claude_root"], project, "hook cwd 버그 고쳐줘")
    indexer.index_conversations(str(project), **roots)

    db, _, _, pid = _engines(config, project)
    try:
        scan = scan_project(project, pid, db, config.indexing)
        conv_deleted = [p for p in scan.deleted if p.startswith(".conversations/")]
        assert conv_deleted == []
    finally:
        db.close()


def _write_claude_turns(claude_root: Path, project: Path, user_texts: list[str],
                        session: str = "s1") -> Path:
    """Write a Claude transcript with one (user, assistant) turn per text."""
    d = claude_root / claude_slug_for(project)
    d.mkdir(parents=True, exist_ok=True)
    lines: list[dict] = []
    for i, text in enumerate(user_texts):
        lines.append({"type": "user", "message": {"role": "user", "content": text},
                      "timestamp": f"2026-04-29T0{i}:00:00Z", "cwd": str(project)})
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"answer {i}"}]}})
    path = d / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return path


def test_index_transcript_incremental_only_embeds_new_turns(tmp_path: Path) -> None:
    indexer, config, project, embedder, roots = _setup(tmp_path)
    path = _write_claude_turns(roots["claude_root"], project, ["first question"])

    r1 = indexer.index_transcript(path, str(project), source="claude")
    assert r1.chunks_total == 1
    assert embedder.embedded == 1

    db, _, _, pid = _engines(config, project)
    ids_after_first = {c.id for c in db.get_chunks_by_project(pid)}
    db.close()

    # Append a second turn to the same session, re-index.
    _write_claude_turns(roots["claude_root"], project, ["first question", "second question"])
    r2 = indexer.index_transcript(path, str(project), source="claude")

    # Only the new turn is embedded; the first turn's chunk is untouched.
    assert r2.chunks_total == 1
    assert embedder.embedded == 2  # 1 + 1, not 1 + 2

    db, bm25, vector, pid = _engines(config, project)
    try:
        ids_after_second = {c.id for c in db.get_chunks_by_project(pid)}
        assert ids_after_first <= ids_after_second  # first turn's id preserved
        assert len(ids_after_second) == 2
        assert vector.count == 2 == bm25.count
    finally:
        db.close()


def test_index_transcript_auto_detects_source(tmp_path: Path) -> None:
    indexer, config, project, embedder, roots = _setup(tmp_path)
    cx = _write_codex(roots["codex_root"], project)
    result = indexer.index_transcript(cx, str(project))  # no source → auto
    assert result.sessions_indexed == 1

    db, _, _, pid = _engines(config, project)
    try:
        metas = db.get_conversation_meta_batch(
            [c.id for c in db.get_chunks_by_project(pid)]
        )
        assert all(m.source == "codex" for m in metas.values())
    finally:
        db.close()


def test_cli_index_conversations_command(tmp_path: Path, monkeypatch, capsys) -> None:
    """A6 — the `index-conversations` CLI command indexes Claude transcripts."""
    from hybrid_search import cli

    project = tmp_path / "proj"
    project.mkdir()
    config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig())
    claude_root = tmp_path / "claude"
    _write_claude(claude_root, project, "hook cwd 버그 고쳐줘")

    # The command discovers transcripts under ~/.claude; point HOME at our fixture.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    real_slug_dir = claude_root / claude_slug_for(project)
    target = tmp_path / ".claude" / "projects" / claude_slug_for(project)
    target.mkdir(parents=True, exist_ok=True)
    for f in real_slug_dir.glob("*.jsonl"):
        (target / f.name).write_text(f.read_text(), encoding="utf-8")

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "Embedder", lambda embedding, models_dir: _FakeEmbedder())

    cli.cmd_index_conversations(argparse.Namespace(cwd=str(project)))

    out = capsys.readouterr().out
    assert "sessions indexed" in out

    db, bm25, vector, pid = _engines(config, project)
    try:
        assert db.get_chunk_count(pid) > 0
        assert vector.count == db.get_chunk_count(pid) == bm25.count
    finally:
        db.close()


class TestBackfillGate:
    """The pre-flight gate must decide before anything is written."""

    def _gate(self, tmp_path, node_types, limit=0.40, planned=10):
        import hybrid_search.cli as cli
        from hybrid_search.storage.db import StoreDB
        from hybrid_search.storage.indexes import IndexPaths

        project_dir = tmp_path / "idx"
        paths = IndexPaths(project_dir)
        paths.ensure_dirs()
        if node_types:
            db = StoreDB(paths.store_db)
            try:
                conn = db._conn
                conn.execute(
                    "INSERT INTO files (id, project_id, relative_path, language, file_hash)"
                    " VALUES ('f1', 'p', 'a.py', 'python', 'h')"
                )
                for i, nt in enumerate(node_types):
                    conn.execute(
                        "INSERT INTO chunks (id, file_id, project_id, node_type, name,"
                        " qualified_name, content, start_line, end_line)"
                        f" VALUES ('c{i}', 'f1', 'p', ?, 'n', 'q', 'x', 1, 2)",
                        (nt,),
                    )
                conn.commit()
            finally:
                db.close()

        import hybrid_search.index.transcript_source as ts
        original = ts.collect_project_chunks
        ts.collect_project_chunks = lambda *a, **k: [object()] * planned
        try:
            return cli._conv_backfill_gate(tmp_path, project_dir, limit)
        finally:
            ts.collect_project_chunks = original

    def test_passes_when_memory_stays_under_the_limit(self, tmp_path):
        gate = self._gate(tmp_path, ["function"] * 90 + ["qa_log"] * 10, planned=10)

        assert gate["ok"] and "PASS" in gate["line"]

    def test_blocks_when_backfill_would_cross_the_limit(self, tmp_path):
        gate = self._gate(tmp_path, ["function"] * 10 + ["qa_log"] * 5, planned=20)

        assert not gate["ok"] and "FAIL" in gate["line"]

    def test_empty_index_is_not_blocked(self, tmp_path):
        """A fresh project reads as 100% memory — it has nothing to dilute."""
        gate = self._gate(tmp_path, [], planned=5)

        assert gate["ok"] and "SKIP" in gate["line"]

    def test_already_indexed_conv_chunks_are_not_double_counted(self, tmp_path):
        gate = self._gate(tmp_path, ["function"] * 90 + ["conv_turn"] * 10, planned=10)

        assert gate["added"] == 0, "10 planned, 10 already present"


class TestMemorableTurnGate:
    """The conversation lane applies the same debris filter as the qa lane."""

    def test_keeps_a_turn_with_a_real_exchange(self):
        from hybrid_search.index.transcript_source import is_memorable_turn

        assert is_memorable_turn(
            "환불 흐름이 어떻게 되나",
            "정산 확정 시점에 결제선생 청구서를 파기합니다.",
        )

    def test_drops_turns_the_qa_lane_already_calls_debris(self):
        from hybrid_search.index.transcript_source import is_memorable_turn

        assert not is_memorable_turn("[Request interrupted by user]", "네 알겠습니다.")
        assert not is_memorable_turn("src/foo/bar.py", "확인했습니다.")
        assert not is_memorable_turn("---------------", "확인했습니다.")

    def test_drops_a_turn_where_nothing_was_said(self):
        """Assistant side is all code fence / log lines — no answer in it."""
        from hybrid_search.index.transcript_source import is_memorable_turn

        assert not is_memorable_turn("3011 떠 있나", "```\n3011 PID 91838\n```")
        assert not is_memorable_turn("빌드 돌려줘", "")

    def test_a_fenced_answer_with_prose_survives(self):
        from hybrid_search.index.transcript_source import is_memorable_turn

        assert is_memorable_turn(
            "왜 실패했지",
            "포트가 이미 점유돼 있었습니다.\n```\nEADDRINUSE\n```",
        )

    def test_filtered_turns_do_not_renumber_the_rest(self, tmp_path):
        """turn_index is a position in the conversation, not a running count."""
        import json as _json
        from hybrid_search.index.transcript_source import parse_claude_transcript

        def user(text):
            return _json.dumps({"type": "user", "message": {"role": "user", "content": text}})

        def assistant(text):
            return _json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": text}]},
            })

        path = tmp_path / "s.jsonl"
        path.write_text("\n".join([
            user("첫 질문은 무엇이었나"), assistant("첫 답변입니다."),
            user("3011 떠 있나"), assistant("```\n3011 PID 91838\n```"),
            user("셋째 질문은 무엇이었나"), assistant("셋째 답변입니다."),
        ]) + "\n", encoding="utf-8")

        chunks = parse_claude_transcript(path)

        assert [c.turn_index for c in chunks] == [0, 2]


class TestReflectionBacklogMarker:
    """A stalled Reflector must announce itself, like STALE.md does for wiki."""

    def _run(self, tmp_path, n_clusters):
        import hybrid_search.cli as cli
        from hybrid_search.memory import reflector

        class _C:
            def __init__(self, q):
                self.representative_query = q

        original = reflector.collect_clusters
        reflector.collect_clusters = lambda root: [_C(f"질문 {i}") for i in range(n_clusters)]
        try:
            cli._report_pending_reflection(tmp_path)
        finally:
            reflector.collect_clusters = original
        return tmp_path / ".hybrid-search" / cli.REFLECT_MARKER_NAME

    def test_writes_a_marker_when_the_backlog_is_real(self, tmp_path, capsys):
        marker = self._run(tmp_path, 5)

        assert marker.is_file()
        assert "5 cluster(s)" in marker.read_text()
        assert "Reflection backlog: 5" in capsys.readouterr().out

    def test_stays_quiet_on_ordinary_churn(self, tmp_path, capsys):
        marker = self._run(tmp_path, 2)

        assert not marker.exists()
        assert "Reflection backlog" not in capsys.readouterr().out

    def test_clears_a_stale_marker(self, tmp_path):
        import hybrid_search.cli as cli

        marker = tmp_path / ".hybrid-search" / cli.REFLECT_MARKER_NAME
        marker.parent.mkdir(parents=True)
        marker.write_text("old backlog")

        assert not self._run(tmp_path, 0).exists()


class TestMemoryShareReport:
    """The corpus's memory share must be visible, not discovered at a gate."""

    def _seed(self, tmp_path, node_types):
        from hybrid_search.storage.db import StoreDB
        from hybrid_search.storage.indexes import IndexPaths

        project_dir = tmp_path / "idx"
        paths = IndexPaths(project_dir)
        paths.ensure_dirs()
        db = StoreDB(paths.store_db)
        try:
            conn = db._conn
            conn.execute(
                "INSERT INTO files (id, project_id, relative_path, language, file_hash)"
                " VALUES ('f1', 'p', 'a.py', 'python', 'h')"
            )
            for i, nt in enumerate(node_types):
                conn.execute(
                    "INSERT INTO chunks (id, file_id, project_id, node_type, name,"
                    " qualified_name, content, start_line, end_line)"
                    f" VALUES ('c{i}', 'f1', 'p', ?, 'n', 'q', 'x', 1, 2)",
                    (nt,),
                )
            conn.commit()
        finally:
            db.close()
        return project_dir

    def test_reports_the_share(self, tmp_path, capsys):
        import hybrid_search.cli as cli

        cli._report_memory_share(self._seed(tmp_path, ["function"] * 8 + ["qa_log"] * 2))

        assert "Memory share: 2/10 chunks (20.0%)" in capsys.readouterr().out

    def test_warns_when_the_next_backfill_would_be_blocked(self, tmp_path, capsys):
        import hybrid_search.cli as cli

        cli._report_memory_share(
            self._seed(tmp_path, ["function"] * 5 + ["conv_turn"] * 5)
        )

        out = capsys.readouterr().out
        assert "50.0%" in out and "backfill gate" in out

    def test_silent_on_an_empty_index(self, tmp_path, capsys):
        import hybrid_search.cli as cli

        cli._report_memory_share(self._seed(tmp_path, []))

        assert capsys.readouterr().out == ""


class TestRebuildPreservesConversations:
    """A rebuild reconstructs from disk; conversations have no file on disk."""

    def _index_with_conv(self, tmp_path, n_sessions=2):
        from hybrid_search.storage.db import StoreDB
        from hybrid_search.storage.indexes import IndexPaths

        project_dir = tmp_path / "idx"
        IndexPaths(project_dir).ensure_dirs()
        db = StoreDB(IndexPaths(project_dir).store_db)
        try:
            conn = db._conn
            conn.execute(
                "INSERT INTO files (id, project_id, relative_path, language, file_hash)"
                " VALUES ('src', 'p', 'a.py', 'python', 'h')"
            )
            for i in range(n_sessions):
                conn.execute(
                    "INSERT INTO files (id, project_id, relative_path, language, file_hash)"
                    f" VALUES ('c{i}', 'p', '.conversations/claude/s{i}.jsonl', 'jsonl', 'h')"
                )
            conn.commit()
        finally:
            db.close()
        return project_dir

    def test_counts_what_a_rebuild_would_lose(self, tmp_path):
        from hybrid_search.index.pipeline import IndexingPipeline

        project_dir = self._index_with_conv(tmp_path, n_sessions=3)
        counter = IndexingPipeline.__dict__["_count_conversation_files"]

        assert counter(None, project_dir) == 3, "on-disk source files are not counted"

    def test_zero_for_an_index_without_conversations(self, tmp_path):
        from hybrid_search.index.pipeline import IndexingPipeline

        project_dir = self._index_with_conv(tmp_path, n_sessions=0)
        counter = IndexingPipeline.__dict__["_count_conversation_files"]

        assert counter(None, project_dir) == 0

    def test_zero_for_a_missing_index(self, tmp_path):
        from hybrid_search.index.pipeline import IndexingPipeline

        counter = IndexingPipeline.__dict__["_count_conversation_files"]

        assert counter(None, tmp_path / "nope") == 0

    def test_result_carries_the_count_for_the_caller(self):
        from hybrid_search.index.pipeline import IndexingResult

        assert IndexingResult(project_id="p", project_name="n").conversations_dropped == 0


class TestCrashDurability:
    """A crash must never leave SQLite asserting what search does not have."""

    def _indexer(self, tmp_path):
        """Real DB/BM25/vector, stub embedder — the durability path is the point."""
        from hybrid_search.config import Config
        from hybrid_search.index.conversation_indexer import ConversationIndexer
        from hybrid_search.project import ProjectRegistry

        class _Embedder:
            embedding_dim = 8

            def embed_texts(self, texts):
                import numpy as np
                return [np.ones(8, dtype="float32") for _ in texts]

        config = Config(data_dir=tmp_path / "data")
        config.global_dir.mkdir(parents=True, exist_ok=True)
        registry = ProjectRegistry(config.global_dir)
        return ConversationIndexer(config, registry, _Embedder())

    def _transcript(self, root, session, n_turns):
        import json as _json

        d = root / ".claude" / "projects" / "p"
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(n_turns):
            lines.append(_json.dumps({
                "type": "user",
                "message": {"role": "user", "content": f"{session} 질문 {i} 무엇인가요"},
            }))
            lines.append(_json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"답변 {i} 입니다."}]},
            }))
        path = d / f"{session}.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_unstamped_session_is_rebuilt_not_skipped(self, tmp_path):
        """The exact 2026-09-05 drift: rows in SQLite, nothing in the engines."""
        from hybrid_search.storage.db import StoreDB
        from hybrid_search.storage.indexes import IndexPaths, get_project_dir
        from hybrid_search.project import project_hash

        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        transcript = self._transcript(tmp_path, "s1", 3)

        indexer = self._indexer(tmp_path)
        indexer.index_transcript(transcript, str(project), source="claude")

        pid = project_hash(str(project.resolve()))
        store = IndexPaths(
            get_project_dir(indexer._config.projects_dir, pid)
        ).store_db
        db = StoreDB(store)
        try:
            rel = ".conversations/claude/s1.jsonl"
            rec = db.get_file_by_path(pid, rel)
            assert rec is not None and rec.file_hash, "a clean run stamps the session"
            chunk_ids = db.get_chunk_ids_by_file(rec.id)
            assert chunk_ids
            # Simulate the crash: chunks stayed, the stamp never happened.
            with db.transaction() as conn:
                from hybrid_search.storage.db import FileRecord

                db.upsert_file(conn, FileRecord(
                    id=rec.id, project_id=pid, relative_path=rel,
                    file_hash="", language="conversation", chunk_count=0,
                ))
        finally:
            db.close()

        result = indexer.index_transcript(transcript, str(project), source="claude")

        assert result.sessions_skipped == 0, "an unstamped session must not be skipped"
        assert result.chunks_total == len(chunk_ids), "every turn is re-embedded"

    def test_a_clean_rerun_is_a_no_op(self, tmp_path):
        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        transcript = self._transcript(tmp_path, "s2", 2)

        indexer = self._indexer(tmp_path)
        indexer.index_transcript(transcript, str(project), source="claude")
        again = indexer.index_transcript(transcript, str(project), source="claude")

        assert again.sessions_indexed == 0 and again.sessions_skipped == 1


class TestDriftToleratesRecoverableWrites:
    """A crashed write is repaired by its own indexer, not by a rebuild."""

    def _db(self, tmp_path, finished: int, unfinished: int):
        from hybrid_search.storage.db import StoreDB
        from hybrid_search.storage.indexes import IndexPaths

        IndexPaths(tmp_path).ensure_dirs()
        db = StoreDB(IndexPaths(tmp_path).store_db)
        conn = db._conn
        conn.execute(
            "INSERT INTO files (id, project_id, relative_path, language, file_hash)"
            " VALUES ('done', 'p', 'a.py', 'python', 'h1')"
        )
        conn.execute(
            "INSERT INTO files (id, project_id, relative_path, language, file_hash)"
            " VALUES ('wip', 'p', '.conversations/claude/s.jsonl', 'conversation', '')"
        )
        for i in range(finished):
            conn.execute(
                "INSERT INTO chunks (id, file_id, project_id, node_type)"
                f" VALUES ('f{i}', 'done', 'p', 'function')"
            )
        for i in range(unfinished):
            conn.execute(
                "INSERT INTO chunks (id, file_id, project_id, node_type)"
                f" VALUES ('u{i}', 'wip', 'p', 'conv_turn')"
            )
        conn.commit()
        return db

    def test_unfinished_chunks_are_excluded_from_the_count(self, tmp_path):
        db = self._db(tmp_path, finished=10, unfinished=4)
        try:
            assert db.get_chunk_count("p") == 14
            assert db.count_unfinished_chunks("p") == 4
        finally:
            db.close()

    def test_a_settled_index_reports_no_unfinished_work(self, tmp_path):
        db = self._db(tmp_path, finished=10, unfinished=0)
        try:
            assert db.count_unfinished_chunks("p") == 0
        finally:
            db.close()
