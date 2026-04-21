"""Apply hand-crafted relevance labels to label_me.tsv.

Labels are defined per-query as a dict:
    (query_id, predicate) → relevance
where predicate checks file_path/name against the row. This is a reproducible
artifact — the rules are auditable and can be revised later.

Rules are based on direct inspection of results.json and my own understanding
of the hybrid-search-mcp codebase. n=10 per query is directional only.
"""

from __future__ import annotations

import csv
from pathlib import Path

HERE = Path(__file__).parent
TSV = HERE / "label_me.tsv"


def _score(qid: str, file_path: str, name: str, snippet: str) -> int:
    """Return 0/1/2 relevance for a labeled chunk.

    Conservative rubric:
      2 = directly answers the query (the function/definition or its canonical doc)
      1 = relevant context (test of the target, caller, neighboring section)
      0 = unrelated or too generic
    """
    fp, n = file_path, (name or "")

    if qid == "S01":  # wiki staleness 감지 어디서
        if fp.endswith("storage/wiki.py") and n in {"check_staleness", "_check_page_staleness"}:
            return 2
        if fp.endswith("tools/wiki.py") and "staleness" in n:
            return 2
        if fp.endswith("cli.py") and n == "cmd_stale":
            return 2
        if fp.endswith("cli.py") and n == "cmd_sync_wiki":
            return 1
        if "Phase 5" in snippet and "Wiki" in snippet:
            return 1
        if fp.endswith("test_wiki.py") and "stale" in n:
            return 1
        return 0

    if qid == "S02":  # post-commit 훅이 reindex 트리거하는 흐름
        if "12.1 현재 문제" in snippet or "After" == n.split("::")[-1] if "::" in n else "After" in snippet[:30]:
            return 2
        if "post-commit" in snippet and ("reindex" in snippet or "nohup" in snippet):
            return 2
        if "M3: post-commit" in snippet or "Phase 8d" in snippet:
            return 2
        if "Phase 6a" in snippet or "M3 post-commit" in snippet:
            return 1
        if "rebuild-index" in fp:
            return 0
        if "PLAN_q1" in fp:
            return 1
        return 0

    if qid == "S03":  # RRF 결과가 orchestrator에서 enrich되는 경로
        if fp.endswith("orchestrator.py") and n == "_enrich_results":
            return 2
        if fp.endswith("orchestrator.py") and n == "hybrid_search":
            return 2
        if fp.endswith("orchestrator.py") and "HybridResult" in n:
            return 1
        if fp.endswith("fusion.py") and "reciprocal_rank_fusion" in n:
            return 1
        if "Phase 10" in snippet and "재랭킹" in snippet:
            return 1
        if "10.2 재랭킹" in snippet:
            return 1
        return 0

    if qid == "S04":  # call edge resolution에서 module index 역할
        if fp.endswith("callgraph.py") and n == "resolve_call_edges":
            return 2
        if fp.endswith("test_callgraph.py") and "module" in n and "import" in n:
            return 2
        if fp.endswith("test_callgraph.py") and ("insert_call_edges_with_module" in n or "unmatched_call" in n):
            return 1
        if "Phase 7" in snippet and "Call Graph" in snippet:
            return 1
        if "Call Graph (Phase 7)" in snippet:
            return 1
        if "update_call_edge_resolution" in n or "get_all_call_edges" in n:
            return 1
        if "conversation-indexing" in fp:
            return 0
        return 0

    if qid == "K01":  # CONFIDENCE_SCORES
        if fp.endswith("test_callgraph.py") and "high_medium_low_each_get_expected_score" in n:
            return 2
        if fp.endswith("test_store_db.py") and ("backfills_from_label" in n or "fresh_schema" in snippet[:200]):
            return 2
        if fp.endswith("test_store_db.py") and "authority_scores_takes_max" in n:
            return 2
        if fp.endswith("test_store_db.py") and "TestConfidenceScoreMigration" in n:
            return 2
        if fp.endswith("storage/db.py") and "get_chunk_authority_scores" in n:
            return 2
        if fp.endswith("callgraph.py") and n == "resolve_call_edges":
            return 1
        if "M1: Call edge numeric" in snippet:
            return 1
        if "unresolved_edge_keeps_default_score" in n:
            return 1
        if "TestReciprocalRankFusion" in n:
            return 0
        if fp.endswith("orchestrator.py") and n == "hybrid_search":
            return 0
        if "TestCommonNames" in n:
            return 0
        return 0

    if qid == "K02":  # _NEEDS_SYNTHESIS_FLAG
        if fp.endswith("cli.py") and "needs_synthesis_flag" in n:
            return 2
        if fp.endswith("cli.py") and "cmd_synthesize_wiki" in n:
            return 2
        if "M4: needs_synthesis flag 파일 패턴" in snippet:
            return 2
        if fp.endswith("search.md") and "needs_synthesis" in snippet:
            return 1
        if fp.endswith("test_cli_hook_install.py") and "json_with_expected_shape" in n:
            return 1
        if "M4" in snippet and "자율 루프" in snippet:
            return 1
        if "⬜ 다음 세션 제안" in snippet:
            return 0
        if fp.endswith("maintain.md"):
            return 1
        if "TestEnsureGitignoreEntries" in n:
            return 0
        return 0

    if qid == "K03":  # reciprocal_rank_fusion
        if fp.endswith("fusion.py") and n == "reciprocal_rank_fusion":
            return 2
        if fp.endswith("test_fusion.py") and "TestReciprocalRankFusion" in n:
            return 2
        if fp.endswith("test_fusion.py") and ("high_authority_chunk" in n or "low_authority_applies" in n):
            return 2
        if fp.endswith("test_fusion.py") and "chunks_outside_map_are_neutral" in n:
            return 2
        if fp.endswith("orchestrator.py") and n == "hybrid_search":
            return 2
        if fp.endswith("fusion.py") and "_apply_authority_nudge" in n:
            return 1
        if "M1: Call edge numeric" in snippet:
            return 1
        if "10.2 재랭킹" in snippet:
            return 1
        if fp.endswith("orchestrator.py") and n == "_enrich_results":
            return 1
        if "_interleave_round_robin" in n:
            return 0
        if "Typed Late Fusion" in snippet:
            return 0
        return 0

    if qid == "N01":  # 저신뢰 엣지가 검색 결과에 미치는 영향
        # No core authority-nudge design chunk surfaced in top-10.
        # Only weak contextual matches — hybrid_search ranks benchmark noise high.
        if "design.md" in fp and "검색 인프라" in snippet:
            return 1
        if "conversation-indexing" in fp and "검색 흐름" in snippet:
            return 1
        if fp.endswith("search.md") and "기능 탐색" in snippet:
            return 1
        return 0

    if qid == "N02":  # 한국어 쿼리 분류 전략
        if fp == "CLAUDE.md" and "라우팅" in snippet:
            return 2
        if fp.endswith("search.md") and "의도 분류" in snippet:
            return 2
        if fp.endswith("search.md") and "운영 규칙" in snippet:
            return 1
        if "10.1 현재 한계" in snippet:
            return 1
        if "프로젝트 한줄 요약" in snippet and "한국어" in snippet:
            return 1
        if "검색 인프라 (Phase 1-4)" in snippet:
            return 1
        if "benchmark_micro_results" in fp:
            return 0
        if "10.2 재랭킹" in snippet:
            return 1
        if "conversation-indexing" in fp:
            return 0
        return 0

    if qid == "N03":  # 동시 커밋 race 방지 메커니즘
        if "M3 race 방지" in snippet or "race" in snippet.lower():
            return 2
        if "M3: post-commit" in snippet:
            return 2
        if "주의사항" in snippet and "M3" in snippet:
            return 1
        if "Quick Wins" in snippet and "M3" in snippet:
            return 1
        if "핵심 설계 결정" in snippet:
            return 1
        if "maintain.md" in fp and "verify" in snippet:
            return 0
        if "9.6 환각 방지" in snippet:
            return 0
        if "12.5 다음 구현" in snippet:
            return 0
        if "bootstrap-wiki" in fp:
            return 0
        if "즉시 해야 할 것" in snippet:
            return 0
        if "conversation-indexing::Phase 6" in snippet:
            return 0
        return 0

    return 0


def main() -> None:
    rows = []
    with open(TSV, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames
        for row in reader:
            rel = _score(row["id"], row["file_path"], row["name"], row["snippet"])
            row["relevance"] = str(rel)
            rows.append(row)

    with open(TSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    # Distribution summary
    from collections import Counter
    dist = Counter((r["id"], r["relevance"]) for r in rows)
    print(f"Labeled {len(rows)} rows.")
    for qid in sorted({r["id"] for r in rows}):
        counts = {rel: dist.get((qid, rel), 0) for rel in ("0", "1", "2")}
        print(f"  {qid}: rel0={counts['0']:>2d} rel1={counts['1']:>2d} rel2={counts['2']:>2d}")


if __name__ == "__main__":
    main()
