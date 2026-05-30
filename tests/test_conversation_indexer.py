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
