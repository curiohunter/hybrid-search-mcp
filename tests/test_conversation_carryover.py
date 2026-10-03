"""A rebuild carries over conversations whose transcript no longer exists.

The agent cleans its transcripts up after ~30 days. A rebuild that only
re-derives conversations from the transcripts still on disk therefore cuts
conversation memory back to a month; the previous index is the only copy of
the older sessions. All fixtures here are synthetic.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from hybrid_search.config import Config, EmbeddingConfig
from hybrid_search.index import conversation_carryover
from hybrid_search.index.conversation_indexer import ConversationIndexer
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.index.transcript_source import claude_slug_for
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import FileRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

_DIM = 8
_GONE = ".conversations/claude/gone.jsonl"
_KEPT = ".conversations/claude/kept.jsonl"


class _FakeEmbedder:
    """Text-dependent vectors, so a reused vector is distinguishable."""

    def __init__(self, fingerprint: str | None = None) -> None:
        self.embedded: list[str] = []
        if fingerprint is not None:
            self.fingerprint = fingerprint

    @property
    def embedding_dim(self) -> int:
        return _DIM

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        self.embedded.extend(texts)
        rows = [
            np.frombuffer(hashlib.sha256(t.encode()).digest()[:_DIM], dtype=np.uint8)
            for t in texts
        ]
        return np.asarray(rows, dtype=np.float32).reshape(len(texts), _DIM) + 1.0


class _World:
    def __init__(self, tmp_path: Path, fingerprint: str | None = None) -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        self.claude_root = tmp_path / "claude"
        self.codex_root = tmp_path / "codex"
        self.config = Config(
            data_dir=tmp_path / "data", embedding=EmbeddingConfig(batch_size=8)
        )
        self.registry = ProjectRegistry(self.config.global_dir)
        self.use_embedder(_FakeEmbedder(fingerprint))
        self.pid = project_hash(str(self.repo.resolve()))
        self.paths = IndexPaths(get_project_dir(self.config.projects_dir, self.pid))

    def use_embedder(self, embedder: _FakeEmbedder) -> None:
        self.embedder = embedder
        self.pipeline = IndexingPipeline(self.config, self.registry, embedder)
        self.indexer = ConversationIndexer(self.config, self.registry, embedder)

    def transcript(self, session: str, topic: str, turns: int = 2) -> Path:
        d = self.claude_root / claude_slug_for(self.repo)
        d.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(turns):
            lines.append({
                "type": "user",
                "message": {"role": "user", "content": f"explain {topic} step {i}"},
                "timestamp": f"2026-01-0{i + 1}T00:00:00Z", "cwd": str(self.repo),
            })
            lines.append({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": f"{topic} answer number {i}"},
                ]},
            })
        path = d / f"{session}.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return path

    def index_conversations(self):
        return self.indexer.index_conversations(
            str(self.repo), claude_root=self.claude_root, codex_root=self.codex_root
        )

    def seed(self) -> None:
        """A code index plus two sessions, one of whose transcripts then vanishes."""
        self.pipeline.index_project(str(self.repo))
        gone = self.transcript("gone", "quokkafrost")
        self.transcript("kept", "lemurharbor")
        assert self.index_conversations().sessions_indexed == 2
        gone.unlink()

    def snapshot(self) -> dict:
        db = StoreDB(self.paths.store_db)
        try:
            chunks = db.get_chunks_by_project(self.pid)
            conv = [c for c in chunks if c.node_type == "conv_turn"]
            return {
                "chunks": len(chunks),
                "unfinished": db.count_unfinished_chunks(self.pid),
                "conv_ids": sorted(c.id for c in conv),
                "meta": db.get_conversation_meta_batch([c.id for c in conv]),
                "files": {
                    f.relative_path: f for f in db.get_all_files(self.pid)
                    if f.relative_path.startswith(".conversations/")
                },
                "bm25": BM25Engine(self.paths.tantivy_dir, read_only=True),
                "vector": VectorEngine(self.paths.vectors_dir, _DIM),
            }
        finally:
            db.close()


def _assert_consistent(snap: dict) -> None:
    settled = snap["chunks"] - snap["unfinished"]
    assert snap["bm25"].count == settled
    assert snap["vector"].count == settled


def test_session_without_transcript_survives_rebuild(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.seed()
    before = world.snapshot()

    result = world.pipeline.index_project(str(world.repo), force=True)

    assert result.conversations_carried == 2
    assert result.conversation_chunks_carried == len(before["conv_ids"])
    assert result.conversations_dropped == 0
    after = world.snapshot()
    assert after["conv_ids"] == before["conv_ids"]
    assert after["files"][_GONE].file_hash == before["files"][_GONE].file_hash
    assert after["files"][_GONE].language == "conversation"
    gone_ids = [i for i in after["conv_ids"] if ":gone:" in i]
    assert gone_ids and all(i in after["meta"] for i in gone_ids)
    assert after["meta"][gone_ids[0]].session_id == "gone"
    assert after["meta"][gone_ids[0]].ts == before["meta"][gone_ids[0]].ts
    assert after["unfinished"] == 0
    assert result.chunks_total == after["chunks"]
    _assert_consistent(after)
    hits = {r.chunk_id for r in after["bm25"].search("quokkafrost", limit=10)}
    assert hits and hits <= set(gone_ids)
    assert all(after["vector"].get_vector(i) is not None for i in gone_ids)


def test_session_with_transcript_is_not_duplicated(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.seed()
    before = world.snapshot()

    world.pipeline.index_project(str(world.repo), force=True)
    rederived = world.index_conversations()

    # Carried over with its hash, so the delta pass has nothing to do.
    assert rederived.sessions_indexed == 0
    after = world.snapshot()
    assert after["conv_ids"] == before["conv_ids"]
    assert after["chunks"] == before["chunks"]
    assert after["files"][_KEPT].file_hash == before["files"][_KEPT].file_hash
    _assert_consistent(after)


def test_transcript_that_grew_is_caught_up_by_the_delta_pass(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.seed()
    before = world.snapshot()
    world.transcript("kept", "lemurharbor", turns=3)

    world.pipeline.index_project(str(world.repo), force=True)
    rederived = world.index_conversations()

    assert rederived.sessions_indexed == 1
    after = world.snapshot()
    assert set(before["conv_ids"]) < set(after["conv_ids"])
    assert len(after["conv_ids"]) == len(set(after["conv_ids"]))
    _assert_consistent(after)


def test_vectors_are_reused_when_the_embedding_model_is_unchanged(tmp_path: Path) -> None:
    world = _World(tmp_path, fingerprint="fake:model-a")
    world.seed()
    before = world.snapshot()
    old = {i: before["vector"].get_vector(i) for i in before["conv_ids"]}
    world.embedder.embedded.clear()

    world.pipeline.index_project(str(world.repo), force=True)

    assert world.embedder.embedded == [], "nothing may be re-embedded"
    after = world.snapshot()
    for chunk_id, vec in old.items():
        np.testing.assert_allclose(after["vector"].get_vector(chunk_id), vec)
    _assert_consistent(after)


def test_vectors_are_reembedded_when_the_embedding_model_changed(tmp_path: Path) -> None:
    world = _World(tmp_path, fingerprint="fake:model-a")
    world.seed()
    before = world.snapshot()
    world.use_embedder(_FakeEmbedder(fingerprint="fake:model-b"))

    result = world.pipeline.index_project(str(world.repo), force=True)

    assert result.conversations_carried == 2
    conv_texts = [t for t in world.embedder.embedded if "explain" in t]
    assert len(conv_texts) == len(before["conv_ids"]), "old-space vectors must not be copied"
    after = world.snapshot()
    assert after["conv_ids"] == before["conv_ids"]
    _assert_consistent(after)


def test_unfinished_session_is_carried_and_stamped(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.seed()
    # A crash in the old index: rows in SQLite, nothing in the engines, no stamp.
    db = StoreDB(world.paths.store_db)
    bm25 = BM25Engine(world.paths.tantivy_dir)
    vector = VectorEngine(world.paths.vectors_dir, _DIM)
    rec = db.get_file_by_path(world.pid, _GONE)
    ids = db.get_chunk_ids_by_file(rec.id)
    with db.transaction() as conn:
        db.upsert_file(conn, FileRecord(
            id=rec.id, project_id=world.pid, relative_path=_GONE, file_hash="",
            language="conversation",
        ))
    db.close()
    bm25.delete_batch(ids)
    bm25.commit()
    vector.remove_batch(ids)
    vector.save()
    del bm25, vector

    world.pipeline.index_project(str(world.repo), force=True)

    after = world.snapshot()
    assert after["files"][_GONE].file_hash
    assert after["unfinished"] == 0
    assert set(ids) <= set(after["conv_ids"])
    _assert_consistent(after)


def test_carryover_failure_does_not_fail_the_rebuild(tmp_path: Path, monkeypatch) -> None:
    world = _World(tmp_path)
    world.seed()

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(conversation_carryover, "_write_session", boom)

    result = world.pipeline.index_project(str(world.repo), force=True)

    assert result.conversations_carried == 0
    assert result.conversations_dropped == 2
    after = world.snapshot()
    assert after["chunks"] - len(after["conv_ids"]) > 0, "the code index was rebuilt"
    _assert_consistent(after)

    # The caller's re-derive still restores what the transcripts hold.
    monkeypatch.undo()
    assert world.index_conversations().sessions_indexed == 1
    restored = world.snapshot()
    assert _KEPT in restored["files"]
    _assert_consistent(restored)


def test_failure_midway_leaves_the_engines_consistent(tmp_path: Path, monkeypatch) -> None:
    world = _World(tmp_path)
    world.seed()
    calls = {"n": 0}

    class _FlakyBM25(BM25Engine):
        def add(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("synthetic bm25 failure")
            return super().add(*args, **kwargs)

    # Only the carry-over's engine: the rebuild's own writes must succeed.
    monkeypatch.setattr(conversation_carryover, "BM25Engine", _FlakyBM25)

    result = world.pipeline.index_project(str(world.repo), force=True)

    monkeypatch.undo()
    assert result.conversations_carried == 0
    after = world.snapshot()
    # Rows may remain as an unfinished session, but never ahead of the engines.
    _assert_consistent(after)
    assert all(not f.file_hash for f in after["files"].values())


def test_carryover_is_a_noop_without_a_previous_index(tmp_path: Path) -> None:
    result = conversation_carryover.carry_over_conversations(
        tmp_path / "missing", tmp_path / "new", _FakeEmbedder(), "p"
    )

    assert (result.sessions, result.chunks) == (0, 0)
