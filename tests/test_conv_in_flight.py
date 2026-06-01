"""Phase 5 (conv) — query-time overlay for not-yet-indexed conversation turns.

The per-turn Stop-hook indexer is async, so a live session's freshest turns lag
the store. These tests prove the overlay:
  1. surfaces a transcript turn that is on disk but absent from the store,
  2. dedups turns the indexer already embedded (no double-show),
  3. splices an in-flight turn into a recall query through the orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from hybrid_search.config import Config, EmbeddingConfig
from hybrid_search.index.conversation_indexer import ConversationIndexer
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.index.transcript_source import claude_slug_for
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search import conv_in_flight
from hybrid_search.search.orchestrator import SearchOrchestrator
from hybrid_search.storage.db import StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir


class _DetEmbedder:
    """Token-hash unit vectors — deterministic, no OpenAI."""

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
        return np.vstack([self._vec(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def _claude_turns(*prompts_and_answers: tuple[str, str]) -> str:
    lines: list[dict] = []
    for prompt, answer in prompts_and_answers:
        lines.append({
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "timestamp": "2026-05-31T03:00:00Z",
            "cwd": "PLACEHOLDER",
        })
        lines.append({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": answer},
            ]},
        })
    return "\n".join(json.dumps(x) for x in lines)


def _write_transcript(claude_root: Path, project: Path, name: str, body: str) -> Path:
    d = claude_root / claude_slug_for(project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(body.replace("PLACEHOLDER", str(project)), encoding="utf-8")
    return path


def _setup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def sign_in(u):\n    return True\n", encoding="utf-8")
    config = Config(data_dir=tmp_path / "data", embedding=EmbeddingConfig())
    registry = ProjectRegistry(config.global_dir)
    embedder = _DetEmbedder()
    IndexingPipeline(config, registry, embedder).index_project(str(repo))
    return config, registry, embedder, repo


def _open_db(config: Config, repo: Path) -> StoreDB:
    pid = project_hash(str(repo.resolve()))
    idx_paths = IndexPaths(get_project_dir(config.projects_dir, pid))
    return StoreDB(idx_paths.store_db)


def test_collect_surfaces_unindexed_turn(tmp_path: Path) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    _write_transcript(
        claude_root, repo, "live.jsonl",
        _claude_turns(("크레딧 정산 로직 어디", "billing.py 의 settle_credit 입니다")),
    )
    pid = project_hash(str(repo.resolve()))
    db = _open_db(config, repo)
    try:
        turns = conv_in_flight.collect_conv_in_flight(
            repo, pid, db, claude_root=claude_root, codex_root=tmp_path / "none",
        )
    finally:
        db.close()
    assert len(turns) == 1
    assert turns[0].source == "claude"
    assert "크레딧" in turns[0].chunk.user_prompt


def test_collect_dedups_indexed_turn(tmp_path: Path) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    _write_transcript(
        claude_root, repo, "s1.jsonl",
        _claude_turns(("형제 할인 계산", "sibling_discount 함수입니다")),
    )
    # Index that transcript — its single turn is now in the store.
    ConversationIndexer(config, registry, embedder).index_conversations(
        str(repo), claude_root=claude_root, codex_root=tmp_path / "none",
    )
    pid = project_hash(str(repo.resolve()))
    db = _open_db(config, repo)
    try:
        turns = conv_in_flight.collect_conv_in_flight(
            repo, pid, db, claude_root=claude_root, codex_root=tmp_path / "none",
        )
    finally:
        db.close()
    assert turns == [], "already-indexed turn must not appear as in-flight"


def test_collect_surfaces_only_the_new_tail(tmp_path: Path) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    # Index the session with one turn...
    _write_transcript(
        claude_root, repo, "s1.jsonl",
        _claude_turns(("입학 테스트 예약", "entrance_test 예약 흐름")),
    )
    ConversationIndexer(config, registry, embedder).index_conversations(
        str(repo), claude_root=claude_root, codex_root=tmp_path / "none",
    )
    # ...then a second turn lands on disk before the next reindex.
    _write_transcript(
        claude_root, repo, "s1.jsonl",
        _claude_turns(
            ("입학 테스트 예약", "entrance_test 예약 흐름"),
            ("상담 등록은", "consultation 등록 핸들러"),
        ),
    )
    pid = project_hash(str(repo.resolve()))
    db = _open_db(config, repo)
    try:
        turns = conv_in_flight.collect_conv_in_flight(
            repo, pid, db, claude_root=claude_root, codex_root=tmp_path / "none",
        )
    finally:
        db.close()
    prompts = [t.chunk.user_prompt for t in turns]
    assert any("상담" in p for p in prompts), f"new tail turn missing: {prompts}"
    assert not any("입학" in p for p in prompts), "indexed turn leaked into overlay"


def test_score_returns_conv_turn_with_in_flight_meta(tmp_path: Path) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    _write_transcript(
        claude_root, repo, "live.jsonl",
        _claude_turns(("크레딧 정산 로직", "settle_credit 함수")),
    )
    pid = project_hash(str(repo.resolve()))
    db = _open_db(config, repo)
    try:
        turns = conv_in_flight.collect_conv_in_flight(
            repo, pid, db, claude_root=claude_root, codex_root=tmp_path / "none",
        )
    finally:
        db.close()
    scored = conv_in_flight.score_conv_in_flight(
        turns, query="크레딧 정산", project_name="repo", project_id=pid, limit=3,
    )
    assert len(scored) == 1
    r = scored[0]
    assert r.node_type == "conv_turn"
    assert "in-flight" in r.trust_meta
    assert "크레딧" in r.content


def test_collect_max_turns_zero_returns_empty(tmp_path: Path) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    _write_transcript(
        claude_root, repo, "live.jsonl",
        _claude_turns(("크레딧 정산", "settle_credit")),
    )
    pid = project_hash(str(repo.resolve()))
    db = _open_db(config, repo)
    try:
        # max_turns=0 must mean "none" — not fresh[-0:] (the whole list).
        turns = conv_in_flight.collect_conv_in_flight(
            repo, pid, db, max_turns=0,
            claude_root=claude_root, codex_root=tmp_path / "none",
        )
    finally:
        db.close()
    assert turns == []


def test_orchestrator_recall_surfaces_in_flight_turn(
    tmp_path: Path, monkeypatch
) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    _write_transcript(
        claude_root, repo, "live.jsonl",
        _claude_turns(("환불 정책 결정", "refund_policy 모듈에서 처리")),
    )
    # Opt the pytest-hermetic overlay back in by pointing roots at fixtures.
    monkeypatch.setenv("HYBRID_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("HYBRID_SEARCH_CODEX_ROOT", str(tmp_path / "none"))
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
    resp = orch.hybrid_search(
        query="이전에 환불 정책 어떻게 결정했지", cwd=str(repo), limit=10,
    )
    conv = [r for r in resp.results if r.node_type == "conv_turn"]
    assert conv, f"in-flight conv turn not surfaced: {[r.node_type for r in resp.results]}"
    assert any("환불" in (r.content or "") for r in conv)
    assert any("in-flight" in (r.trust_meta or "") for r in conv)


def test_in_flight_leads_but_indexed_conv_still_shows(
    tmp_path: Path, monkeypatch
) -> None:
    config, registry, embedder, repo = _setup(tmp_path)
    claude_root = tmp_path / "claude"
    # An OLDER turn that is already indexed...
    _write_transcript(
        claude_root, repo, "old.jsonl",
        _claude_turns(("환불 정책 예전 논의", "refund_policy 초기안")),
    )
    ConversationIndexer(config, registry, embedder).index_conversations(
        str(repo), claude_root=claude_root, codex_root=tmp_path / "none",
    )
    # ...and a LIVE turn that is on disk but not yet indexed.
    _write_transcript(
        claude_root, repo, "live.jsonl",
        _claude_turns(("환불 정책 방금 결정", "refund_policy 최종안")),
    )
    monkeypatch.setenv("HYBRID_SEARCH_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setenv("HYBRID_SEARCH_CODEX_ROOT", str(tmp_path / "none"))
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
    resp = orch.hybrid_search(
        query="이전에 환불 정책 어떻게 결정했지", cwd=str(repo), limit=10,
    )
    conv = [r for r in resp.results if r.node_type == "conv_turn"]
    assert conv, "no conv turns surfaced"
    # The live (in-flight) turn must LEAD the conv head — its token score is not
    # comparable to indexed RRF, so it gets a rank-bounded priority slot.
    assert "in-flight" in (conv[0].trust_meta or ""), (
        f"in-flight turn did not lead conv head: {[r.trust_meta for r in conv]}"
    )
    # ...but the indexed turn still gets a slot (in-flight cedes one).
    assert any("in-flight" not in (r.trust_meta or "") for r in conv), (
        "indexed conv turn was starved by the in-flight head"
    )
