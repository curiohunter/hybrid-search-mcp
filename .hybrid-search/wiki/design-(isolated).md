# Design (Isolated)
> synthesized: 2026-04-14

## Overview

The design document (`docs/design.md`) is the authoritative architectural specification for the Hybrid Search MCP Server, containing 18 sections that cover problem statement, competitive analysis, tech stack trade-offs, architecture diagrams, module structure, embedding model selection, AST chunking rules, indexing pipeline flow, MCP tool API specifications, RRF fusion algorithm, call graph resolution strategy, storage design, configuration schema, MindVault coexistence strategy, implementation phases, and dependencies. It exists to provide a single reference for all design decisions and their rationale, evolving through 5 revisions with Codex review and web research.

## Key Design Decisions

- **Python with native extensions over pure Rust**: Python was chosen for development speed and ML ecosystem maturity, while performance-critical components (BM25/tantivy in Rust, vector search/USearch in C++, AST parsing/tree-sitter in C) run as native bindings. The migration criterion is documented: switch to Rust only if Python orchestration exceeds 30% of total time (`docs/design.md:L72`)
- **RRF fusion with query-type-dependent BM25 weights**: Korean NL queries get low BM25 weight (0.15) since Korean tokens rarely match English code keywords, while exact symbol queries get high weight (0.8) since BM25 excels at exact matching ()
- **Structured embedding input over raw code**: Embedding input follows the "contextualizedText" pattern with `passage:` prefix, including scope chain, imports, docstring, and content -- improving semantic matching versus raw source code (`docs/design.md:L256`)
- **Byte-range-based chunk IDs**: Chunk IDs use `SHA256(project_id + file_path + start_byte + end_byte)` instead of name-based IDs, ensuring stability for overloaded functions, anonymous functions, and default exports (`docs/design.md:L334`)
- **Non-whitespace character count for chunk size thresholds**: Large chunk splitting uses 4000 non-whitespace characters rather than line count, avoiding distortion from blank lines and comments (cAST paper result: +1.2-4.3 Recall@5 points) (`docs/design.md:L361`)
- **Crash recovery via file_hash ordering**: The indexing pipeline updates `file_hash` last after inserting chunks, so a crash mid-write leaves `file_hash=""` which triggers re-indexing on next run (`docs/design.md:L373`)
- **Three-tier call graph confidence**: Resolution uses high (qualified name match), medium (unique name match), and low (ambiguous) tiers, with Phase 7 adding import-call binding and method receiver tracking to improve from 7.5% to 45-66% ()
- **MindVault coexistence, not replacement**: The design explicitly positions Hybrid Search alongside MindVault -- Hybrid Search handles semantic code search while MindVault handles BM25 text search, with a guide for when Claude should choose each tool ()

## Data Flow

```
docs/design.md (read by developers and AI)
  |
  +-- Section 5 (Architecture) --> system-level component diagram
  +-- Section 6 (Module Structure) --> file layout and responsibilities
  +-- Section 9 (Indexing Pipeline) --> delta indexing flow diagram
  +-- Section 10 (MCP Tools) --> API contracts for 9 tools
  +-- Section 11 (RRF) --> fusion algorithm and weight strategy
  +-- Section 12 (Call Graph) --> extraction + resolution strategy
  +-- Section 13 (Storage) --> SQLite schema + index file layout
  +-- Section 16 (Phases) --> Phase 1-8d implementation roadmap
```

## Caveats

- The document lists `semantic_search` as a separate MCP tool (Section 10.2), but per [[handoff-(isolated)]], this was later merged into `hybrid_search` with `bm25_weight=0`. The design doc may be stale on tool count ()
- The module structure in Section 6 does not include `index/dag.py`, `storage/wiki.py`, or `tools/wiki.py` which were added in later phases -- the file tree is incomplete relative to the current codebase (`docs/design.md:L164`)
- Non-goal "100% local execution (no API key)" conflicts with the actual implementation which defaults to OpenAI `text-embedding-3-small` requiring an API key. The ONNX backend provides the local option but is not the default (`docs/design.md:L30`)
- Performance goals (Section 9) specify <500ms search and <5min initial indexing for 10K files, but measured valuein-homepage results show 229s for 1,757 files, suggesting the 10K-file target may not be met (`docs/design.md:L451`)
- The 105 symbols in this file are all markdown sections, not code symbols -- this is a documentation-only module with no executable code

## Related Modules

- [[architecture]] -- provides a condensed view of the same system; design.md is the detailed specification
- [[handoff-(isolated)]] -- tracks implementation status against the phases defined in design.md
- [[index-(isolated)]] -- implements the AST chunking rules specified in Section 8
- [[tests]] -- validates the behaviors specified in the design document

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `docs/design.md`

## Symbols

### `docs/design.md`

- **Hybrid Search MCP Server — Design Document** (section, L1)
- **1. Problem Statement** (section, L6)
- **2. Goals & Non-Goals** (section, L18)
- **Goals** (section, L20)
- **Non-Goals** (section, L30)
- **3. 경쟁 분석 & 차별점** (section, L37)
- **4. Tech Stack Trade-off: Rust vs Python** (section, L52)
- **Option A: Rust** (section, L54)
- **Option B: Python** (section, L63)
- **결론: Python with Native Extensions — **권장**** (section, L72)
- **5. Architecture** (section, L94)
- **6. Module Structure** (section, L164)
- **7. Embedding Model Selection** (section, L217)
- **요구사항** (section, L219)
- **후보 비교** (section, L225)
- **권장: Phase 1에서 벤치마크 후 결정** (section, L236)
- **임베딩 입력 형식** (section, L256)
- **토큰 예산 & Truncation 정책** (section, L282)
- **8. AST-Based Code Chunking** (section, L300)
- **TreeSitter 지원 언어** (section, L302)
- **Phase 1 (MVP): 핵심 3개 언어** (section, L304)
- **Phase 3: 확장 (10개 추가)** (section, L312)
- **AST 파싱 실패 시 폴백** (section, L327)
- **청크 구조** (section, L334)
- **청킹 규칙** (section, L361)
- **9. Indexing Pipeline** (section, L371)
- **Delta Indexing Flow** (section, L373)
- **성능 목표** (section, L451)
- **10. MCP Tool Specifications** (section, L461)
- **10.1 `hybrid_search`** (section, L463)
- **10.2 `semantic_search`** (section, L544)
- **10.3 `trace_callers`** (section, L580)
- **10.4 `trace_callees`** (section, L625)
- **Trace 응답 형식 (trace_callers / trace_callees 공통)** (section, L663)
- **10.5 `index_project`** (section, L692)
- **10.6 `search_symbols`** (section, L722)
- **10.7 `index_status`** (section, L748)
- **10.8 `list_projects`** (section, L786)
- **10.9 `remove_project`** (section, L801)
- **11. RRF (Reciprocal Rank Fusion) Algorithm** (section, L827)
- **쿼리 분류 & 가중치 전략** (section, L861)
- **12. Call Graph** (section, L885)
- **추출 방법** (section, L887)
- **저장 구조 (SQLite, store.db 내)** (section, L897)
- **Resolution 전략** (section, L916)
- **해결된 한계 (Phase 7에서 해결 ✅)** (section, L924)
- **13. Storage Design** (section, L934)
- **단일 SQLite (store.db per project)** (section, L936)
- **글로벌 프로젝트 레지스트리 (`~/.hybrid-search/global/project_registry.db`)** (section, L980)
- **인덱스 파일** (section, L1006)
- **동시성 모델** (section, L1013)
- **Multi-store 업데이트 순서 (원자성)** (section, L1021)
- **인덱스 버전 관리 & 마이그레이션** (section, L1047)
- **14. Configuration** (section, L1071)
- **`~/.hybrid-search/config.toml`** (section, L1073)
- **... more projects** (section, L1122)
- **15. MindVault 공존 전략** (section, L1125)
- **역할 분리** (section, L1127)
- **충돌 분석** (section, L1137)
- **Claude의 도구 선택 가이드** (section, L1146)
- **사용 시나리오** (section, L1152)
- **16. Implementation Phases** (section, L1159)
- **Phase 1~4: 핵심 검색 파이프라인 ✅ 완료** (section, L1161)
- **Phase 5: Reactive Wiki Layer ✅ 완료** (section, L1171)
- **Phase 6: Background Indexing + Wiki Infra ✅ 완료** (section, L1177)
- **Phase 7: Call Graph Resolution 90%+ (구현 완료 ✅)** (section, L1184)
- **현재 상태와 근본 원인** (section, L1188)
- **Step 1: Import-Call 바인딩 (예상 7.5% → ~55%)** (section, L1199)
- **현재: call만 추출** (section, L1204)
- **개선: import와 연결** (section, L1207)
- **Step 2: Module Path → File 역인덱스 (누적 ~70-75%)** (section, L1220)
- **현재: qualified_name으로만 검색** (section, L1223)
- **개선: import path로도 검색** (section, L1226)
- **Step 3: 메서드 Receiver 추적 (누적 ~85-90%)** (section, L1236)
- **현재: this.validate() → callee_name="validate" (COMMON_NAME, 해결 불가)** (section, L1239)
- **개선: this.validate() → callee_name="validate", parent_class="AuthService"** (section, L1240)
- **→ AuthService.validate()로 매칭** (section, L1241)
- **Step 4: COMMON_NAMES 정책 완화 (누적 ~90-95%)** (section, L1249)
- **검증 계획** (section, L1256)
- **Phase 7 구현 전후 비교** (section, L1259)
- **출력: total_edges, resolved, high, medium, low, unresolved, resolution_rate** (section, L1262)
- **Phase 8: CodeWiki 자동 Wiki 생성 (미구현, Phase 7 전제)** (section, L1267)
- **왜 Call Graph 기반이 디렉토리 스캔보다 나은가** (section, L1272)
- **8a: 모듈 트리 자동 생성 (`generate-wiki-plan`)** (section, L1281)
- **Module Tree (21 modules):** (section, L1304)
- **1. auth-system (5 files, 12 chunks) — app/(auth)/, lib/auth/, proxy.ts** (section, L1305)
- **2. tuition-billing (8 files, 23 chunks) — services/tuition-*, hooks/use-tuition*** (section, L1306)
- **3. makeup-attendance (3 files, 8 chunks) — services/makeup-service.ts, hooks/use-makeup*** (section, L1307)
- **4. textbook-grading (4 files, 11 chunks) — services/textbook-grading-*, hooks/use-textbook*** (section, L1308)
- **5. consultation (6 files, 15 chunks) — services/consultation-*, hooks/use-consultation*** (section, L1309)
- **...** (section, L1310)
- **8b: 리프 → 부모 상향식 Wiki 생성** (section, L1313)
- **8c: 검수 자동화** (section, L1323)
- **출력:** (section, L1327)
- **Module Tree: 21 modules** (section, L1328)
- **Wiki pages: 21/21 (100%)** (section, L1329)
- **Coverage: 1,423/1,757 files covered (81%)** (section, L1330)
- **Uncovered: 334 files (node_modules 제외 후 실질 12 files)** (section, L1331)
- **Dependencies tracked: 156 deps** (section, L1332)
- **8d: 전체 파이프라인 (완전 자동)** (section, L1335)
- **17. Dependencies (Python)** (section, L1354)
- **Note: tree-sitter-languages는 Python 3.13 미지원으로 개별 grammar 패키지 사용** (section, L1385)
- **18. Open Questions** (section, L1392)
- **Resolved** (section, L1394)
- **Open** (section, L1409)

</details>