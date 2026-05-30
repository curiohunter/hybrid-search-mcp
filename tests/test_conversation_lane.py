"""A5 — conversation retrieval lane in the orchestrator.

Proves the two properties the lane must hold (the retrieval eval's GO/NO-GO):
  1. POLLUTION GUARD — a plain code query never surfaces conv_turn chunks,
     even though they share the index.
  2. RECALL — a recall-shaped (memory-intent) query surfaces the right conv
     turn among the top results.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from hybrid_search.config import Config, EmbeddingConfig
from hybrid_search.index.conversation_indexer import ConversationIndexer
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.index.transcript_source import claude_slug_for
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator


class _DetEmbedder:
    """Token-hash unit vectors — discriminates without OpenAI."""

    @property
    def embedding_dim(self) -> int:
        return 16

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.embedding_dim, dtype=np.float32)
        for tok in (text or "").lower().split():
            v[sum(ord(c) for c in tok) % self.embedding_dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.embedding_dim), dtype=np.float32)
        return np.stack([self._vec(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def _write_claude(claude_root: Path, project: Path) -> None:
    d = claude_root / claude_slug_for(project)
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"role": "user",
         "content": "홈 디렉토리 git 루트 분리 작업"},
         "timestamp": "2026-04-29T04:59:35Z", "cwd": str(project)},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "git 루트를 프로젝트로 분리했습니다"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "git init"}},
        ]}},
    ]
    (d / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def _setup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def sign_in(user):\n    '''Handle user login.'''\n    return True\n",
        encoding="utf-8",
    )
    config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig())
    registry = ProjectRegistry(config.global_dir)
    embedder = _DetEmbedder()

    IndexingPipeline(config, registry, embedder).index_project(str(repo))
    claude_root = tmp_path / "claude"
    _write_claude(claude_root, repo)
    ConversationIndexer(config, registry, embedder).index_conversations(
        str(repo), claude_root=claude_root, codex_root=tmp_path / "none",
    )
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
    return orch, repo


def test_code_query_does_not_surface_conversations(tmp_path: Path) -> None:
    orch, repo = _setup(tmp_path)
    # Plain code query, no recall intent — conv turns must stay out.
    resp = orch.hybrid_search(query="sign_in user login", cwd=str(repo), limit=10)
    node_types = [r.node_type for r in resp.results]
    assert "conv_turn" not in node_types, f"conv leaked into code lane: {node_types}"


def test_recall_query_surfaces_conversation(tmp_path: Path) -> None:
    orch, repo = _setup(tmp_path)
    # Recall-shaped query ("이전에 … 했지") with token overlap on the conv turn.
    resp = orch.hybrid_search(
        query="이전에 git 루트 분리 어떻게 했지", cwd=str(repo), limit=10,
    )
    node_types = [r.node_type for r in resp.results]
    assert "conv_turn" in node_types, (
        f"conv turn not surfaced for recall query. node_types={node_types}"
    )
    conv = next(r for r in resp.results if r.node_type == "conv_turn")
    assert conv.trust_meta == "[conversation - claude]"
