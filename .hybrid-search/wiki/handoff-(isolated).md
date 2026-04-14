# Handoff (Isolated)
> synthesized: 2026-04-14

## Overview

The Handoff module (`HANDOFF.md` + `pyproject.toml`) serves as the canonical project status document and dependency manifest, recording what has been built across Phases 1-8d, what remains to be done, and how to run the system. It exists so that any developer (or future AI session) can pick up the project without re-discovering the architecture -- it captures completed milestones, measured performance data, known issues, design decisions, and exact environment setup instructions.

## Key Design Decisions

- **Phase-based incremental documentation**: Each phase is documented with a table mapping items to design.md sections, implementation files, and line counts, providing traceability from design to code (`HANDOFF.md:L14`)
- **MCP tool count reduced from 13 to 3**: Management and wiki tools were moved to CLI to reduce system prompt token overhead per conversation. Only `hybrid_search`, `trace_callers`, and `trace_callees` remain as MCP tools (`HANDOFF.md:L235`)
- **`semantic_search` merged into `hybrid_search`**: Setting `bm25_weight=0` in `hybrid_search` gives pure vector search, eliminating a redundant MCP tool (`HANDOFF.md:L235`)
- **Measured resolution rates documented alongside design**: Call graph resolution percentages (45.3% for hybrid-search-mcp, 66.2% for valuein-homepage) are recorded with context explaining why raw rates appear low (external library calls inflate the denominator) (`HANDOFF.md:L137`)
- **Full automation pipeline via post-commit hook**: `git commit -> post-commit hook -> reindex -> call graph re-resolve -> wiki sync` enables zero-touch wiki maintenance (`HANDOFF.md:L214`)

## Data Flow

```
HANDOFF.md (read by developer/AI)
  |
  +-- "Completed" sections --> understand what exists
  +-- "Execution Environment" --> exact commands to run
  +-- "MCP Config" --> ~/.claude.json server registration
  +-- "Known Issues" --> avoid past pitfalls
  |
pyproject.toml
  |
  +-- [project.dependencies] --> runtime packages
  +-- [project.optional-dependencies] --> dev/test packages
  +-- [tool.pytest] --> test configuration
```

## Caveats

- The "immediately do" section still references Phase 8 as next, but Phase 8a-8d are already marked complete further up -- the document has some internal inconsistency in ordering (`HANDOFF.md:L179` vs `HANDOFF.md:L164`)
- The "not yet done" section mentions "Call Graph Resolution 90%+ (currently 7.5%)" as a prerequisite, but Phase 7 above reports this as completed with 45-66% on import-bound edges -- the prerequisite text appears stale (`HANDOFF.md:L301`)
- pyproject.toml is included in this module but contains no source code in the synthesis input, so dependency version details are not available for verification (`pyproject.toml:L1`)

## Related Modules

- [[design-(isolated)]] -- the design document referenced throughout HANDOFF.md as the architectural specification
- [[architecture]] -- describes the same system from a structural perspective rather than a historical one
- [[tests]] -- Phase 4 and Phase 7 sections reference test counts (218 tests across 13 files)

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `HANDOFF.md`
- `pyproject.toml`

## Symbols

### `HANDOFF.md`

- **Hybrid Search MCP — Handoff Document** (section, L1)
- **프로젝트 한줄 요약** (section, L6)
- **완료된 것** (section, L12)
- **Phase 1: MVP — 시맨틱 검색 파이프라인 ✅** (section, L14)
- **Phase 2: Hybrid + BM25 ✅** (section, L30)
- **지원 모듈 ✅** (section, L40)
- **Phase 3a: Call Graph ✅** (section, L50)
- **Phase 3a Code Review 수정 ✅** (section, L61)
- **Phase 3b: 추가 언어 지원 ✅** (section, L72)
- **Phase 4: Polish ✅** (section, L81)
- **Phase 5: Reactive Wiki Layer ✅** (section, L94)
- **Phase 6a: CLI + Hook + 스킬 ✅** (section, L107)
- **Phase 7: Call Graph Resolution 90%+ ✅** (section, L137)
- **실전 검증 결과** (section, L157)
- **breeze 프로젝트 (소규모)** (section, L159)
- **valuein-homepage 프로젝트 (대규모) — 2026-04-13 추가** (section, L164)
- **공통** (section, L172)
- **즉시 해야 할 것 — Phase 8: CodeWiki 자동 Wiki 생성** (section, L179)
- **검증 필요** (section, L186)
- **Phase 7 구현 효과 실측 — force reindex 후 stats 확인** (section, L188)
- **아직 안 한 것** (section, L197)
- **Phase 6b 후보: CodeWiki 자동 wiki 생성** (section, L199)
- **ONNX INT8 quantization** (section, L208)
- **MindVault BM25 대체** (section, L212)
- **실행 환경** (section, L218)
- **가상환경 활성화** (section, L221)
- **서버 실행 (Claude Code MCP로 자동 실행됨)** (section, L225)
- **CLI 명령** (section, L228)
- **테스트** (section, L235)
- **인덱스 데이터 위치** (section, L238)
- **MCP 설정 위치** (section, L244)
- **스킬 위치** (section, L259)
- **알려진 이슈 & 교훈** (section, L269)
- **핵심 설계 결정 (빠른 참조)** (section, L289)

### `pyproject.toml`

- **pyproject** (document, L1)

</details>