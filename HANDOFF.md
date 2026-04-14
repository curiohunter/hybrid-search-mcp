# Hybrid Search MCP — Handoff Document

> **Date**: 2026-04-14 | **Branch**: main
> **설계 문서**: `docs/design.md` (v7, Phase 1-10 완료 + LLM 재랭킹)

## 프로젝트 한줄 요약

BM25 + Vector(RRF) 하이브리드 검색 MCP 서버. 한국어 자연어 → 영어 코드 크로스 언어 검색이 핵심 가치.

---

## 완료된 것

### Phase 1: MVP — 시맨틱 검색 파이프라인 ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| 임베딩 모델 벤치마크 | §7 | `benchmarks/run_benchmark*.py` | — |
| MCP 서버 뼈대 (7개 도구) | §10 | `server.py` | 257 |
| File scanner + delta detection | §9 | `index/scanner.py` | 195 |
| AST chunker (TS/JS/Python) | §8 | `index/ast_chunker.py` | 604 |
| 문서 chunker (MD/JSON/YAML) | §8 | `index/doc_chunker.py` | 181 |
| Embedding 생성 (sentence-transformers) | §7 | `index/embedder.py` | 256 |
| USearch 벡터 인덱스 | §13 | `search/vector.py` | 163 |
| `semantic_search` tool | §10.2 | `tools/semantic_search.py` | 148 |
| `search_symbols` tool | §10.6 | `tools/symbols.py` | 60 |
| `index_project` / `index_status` | §10.5, §10.7 | `tools/index.py` | 55 |
| `list_projects` / `remove_project` | §10.8, §10.9 | `tools/projects.py` | 50 |

### Phase 2: Hybrid + BM25 ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Tantivy BM25 인덱스 | §13 | `search/bm25.py` | 156 |
| RRF fusion (k=60) | §11 | `search/fusion.py` | 51 |
| 쿼리 분류기 (SYMBOL/KR/EN) | §11 | `search/orchestrator.py` | 487 |
| `hybrid_search` tool | §10.1 | `tools/hybrid_search.py` | 51 |
| 멀티 프로젝트 + cross-project 검색 | §13 | `search/orchestrator.py` | (포함) |

### 지원 모듈 ✅

| 파일 | 역할 | 줄수 |
|------|------|:----:|
| `config.py` | TOML 설정 로딩, 모델 토큰 자동감지 | 207 |
| `project.py` | 글로벌 프로젝트 레지스트리 (SQLite) | 114 |
| `storage/db.py` | per-project store.db (WAL, FK CASCADE) | ~550 |
| `storage/indexes.py` | 인덱스 경로 관리 | 45 |
| `index/pipeline.py` | 인덱싱 오케스트레이션 (multi-store 트랜잭션) | ~315 |

### Phase 3a: Call Graph ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Call Graph Resolution (3단계 confidence) | §12 | `index/callgraph.py` | 155 |
| trace_callers/trace_callees 도구 | §10.3, §10.4 | `tools/trace.py` | 250 |
| StoreDB call graph 쿼리 (10개 메서드) | §12, §13 | `storage/db.py` (추가) | +180 |
| AST byte offset 버그 수정 | §8 | `index/ast_chunker.py` (수정) | — |

**검증**: 1,934 call edges, 146 resolved (7.5%), 0 dirty. trace depth 2에서 정확한 caller/callee 추적.

### Phase 3a Code Review 수정 ✅

| 수정 | 우선순위 |
|------|:--------:|
| `_process_file`에 `db.transaction()` 적용 (partial write 방지) | P1 |
| `db._conn` 직접 접근 제거 → public method/transaction 사용 | P1 |
| `call_edges.callee_chunk_id` 인덱스 추가 | P2 |
| `_get_file_from_chunks` O(N) → `file_index` dict O(1) | P2 |
| `lstrip("./")` → `removeprefix("./")` | P2 |
| 삭제 시 dangling callee edge 정리 (`delete_call_edges_by_callee`) | P2 |

### Phase 3b: 추가 언어 지원 ✅

| 항목 | design.md 섹션 | 구현 파일 | 변경 |
|------|:-------------:|-----------|:----:|
| 10개 언어 AST 청킹 (Rust/Go/Ruby/Java/C/C++/Swift/Kotlin/CSS/SQL) | §8 | `index/ast_chunker.py` | CHUNK_NODE_TYPES, CLASS_NODE_TYPES, _get_ts_language, _classify_node_type, _extract_name, _extract_imports, _extract_docstring, _extract_call_name 확장 |
| tree-sitter grammar 의존성 11개 추가 | §17 | `pyproject.toml` | +11 패키지 |

**검증**: 모든 14개 AST 언어 파싱 성공 (TS/JS/Python/Rust/Go/Ruby/Java/C/C++/Swift/Kotlin/CSS/SCSS/SQL). HTML은 fallback blank-line chunking 사용.

### Phase 4: Polish ✅

| 항목 | 설명 | 상태 |
|------|------|:----:|
| 테스트 확충 | 175개 테스트 (12개 파일) | ✅ |
| 크래시 복구 | (1) consistency mismatch → 자동 force rebuild (2) `file_hash=""` partial write 감지 → 재인덱싱 | ✅ |
| ONNX 백엔드 | `_embed_onnx_batch()` 완전 구현 (mean pooling + L2 normalize) | ✅ |
| Ollama 백엔드 | `POST /api/embed` HTTP API 백엔드 | ✅ |
| Apple Silicon MPS | ONNX: CoreMLExecutionProvider, ST: `device="mps"` | ✅ |
| 인덱싱 진행률 | `ProgressCallback(current, total, path)` | ✅ |
| config.toml 핫 리로드 | `_HotReloadableConfig` mtime 감지 | ✅ |
| CWD 프로젝트 부스트 | BM25 2:1 weighted interleave + Vector cosine +0.05 | ✅ |

### Phase 5: Reactive Wiki Layer ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| WikiConfig (config.toml `[wiki]` 섹션) | `config.py` | +15 |
| wiki_pages + wiki_dependencies 테이블 | `storage/db.py` | +25 |
| WikiStore (compile/lookup/staleness/refresh/eviction) | `storage/wiki.py` | ~270 |
| 4개 Tool 핸들러 | `tools/wiki.py` | ~180 |
| server.py 등록 (13개 도구) | `server.py` | +100 |
| 테스트 28개 | `tests/test_wiki.py` | ~280 |

**핵심 설계**: 서버는 저장 + 의존성 추적만 담당, 콘텐츠는 Claude가 작성. `source_chunk_ids`로 검색 결과 → 파일 해시 스냅샷 자동 연결. 변경 감지 시 stale 마킹.

### Phase 6a: CLI + Hook + 스킬 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| CLI 엔트리포인트 (`reindex`, `status`, `stale`, `install-hook`, `sync-wiki`) | `cli.py` | 402 |
| post-commit hook 스크립트 + 자동 설치 | `scripts/post-commit-hook.sh` | 11 |
| `/bootstrap-wiki` 스킬 | `~/.claude/skills/bootstrap-wiki/skill.md` | 142 |
| `/save-wiki` 스킬 | `~/.claude/skills/save-wiki/skill.md` | ~65 |
| `/search` 스킬 (wiki-first 4단계 폴백) | `~/.claude/skills/search/skill.md` | ~64 |

**CLI 명령어**:
- `python -m hybrid_search.cli reindex --cwd .` — delta 재인덱싱 + stale 마킹 + gap 플래그
- `python -m hybrid_search.cli status` — 전체 프로젝트 인덱스 상태
- `python -m hybrid_search.cli stale --cwd .` — wiki staleness 확인
- `python -m hybrid_search.cli install-hook --cwd .` — post-commit hook 자동 설치
- `python -m hybrid_search.cli sync-wiki --cwd .` — 디스크 wiki → DB 동기화 (backtick 경로에서 파일 의존성 자동 추출)
- `python -m hybrid_search.cli call-graph-stats --cwd .` — call graph resolution 통계 (Phase 7)

**스킬 검색 체인** (`/search`):
```
1. lookup_wiki (DB) → found+fresh → 즉시 반환
2. wiki/index.md (디스크) → Read로 확인
3. hybrid_search (MCP) → 결과 좋으면 compile_to_wiki로 축적
4. Grep/Glob (폴백) → 직접 검색
```

**설치된 hook**: valuein-homepage, breeze 프로젝트에 post-commit hook 설치 완료.

**valuein-homepage wiki 부트스트랩 완료**: 10개 wiki 페이지 생성 + DB 동기화 (sync-wiki). architecture, students, tuition-billing, attendance, learning-data, homework-analysis, diagnosis, portal, consultation, edge-functions.

### Phase 7: Call Graph Resolution 90%+ ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| Step 1: Import-Call 바인딩 | `index/ast_chunker.py` | `_extract_import_map()` 신규, `_extract_calls()` → `list[tuple[str, str\|None]]` 반환 |
| Step 2: Module Path → File 역인덱스 | `index/callgraph.py` | `_build_module_index()` 신규, 다양한 import path 형태 → 파일 chunks 매핑 |
| Step 3: 메서드 Receiver 추적 | `index/ast_chunker.py`, `index/callgraph.py` | `this`/`self` 감지 → `__self__::ClassName` 태그, `class_members` 인덱스 |
| Step 4: COMMON_NAMES 정책 완화 | `index/callgraph.py` | module context 있으면 common name도 medium으로 승격 |
| DB 인터페이스 변경 | `storage/db.py` | `insert_call_edges(calls: list[tuple[str, str\|None]])` callee_module 포함 |
| CLI call-graph-stats | `cli.py` | `python -m hybrid_search.cli call-graph-stats --cwd .` 명령 추가 |
| 테스트 10개 추가 | `tests/test_callgraph.py` | Import-Call 5개 + Self-Method 3개 + Common-Name 2개 |

**핵심 설계**: 기존 `_extract_imports()`는 raw string 리스트로 유지(임베딩용), 별도 `_extract_import_map()`으로 name→module 딕셔너리 생성. `_extract_call_name_ex()`에서 this/self receiver 감지. callgraph에 module_index + class_members 이중 인덱스 추가. `_BUILTIN_CALLS` + `_BUILTIN_METHOD_CALLS`로 built-in/라이브러리 호출 필터링.

**지원 언어 Import 파싱**: TS/JS (named/default/namespace import), Python (from...import, import...as), Go, Java, Rust, Ruby, Kotlin, Swift

**실측 결과**:

| 프로젝트 | Total Edges | Project Deps (H+M) | Module 있는 edge | 그 중 Resolved |
|----------|:-----------:|:-------------------:|:----------------:|:--------------:|
| hybrid-search-mcp (73파일) | 2,727 | 572 | 686 | **45.3%** |
| valuein-homepage (1,757파일) | 21,127 | 1,961 | 1,560 | **66.2%** |

**해석**: 전체 resolution rate(21-13%)는 외부 라이브러리 호출(`supabase.from()`, `Array.map()`)이 denominator를 부풀려 낮게 보임. import-call 바인딩이 성공한 edge는 45-66% resolve. CodeWiki에 필요한 **Project Deps (High+Medium)** = 572~1,961개 — 프로젝트 내부 의존성 그래프 구축에 충분.

**총 코드**: ~7,900줄 (31개 파일) | **MCP 도구**: 13개 | **테스트**: 185개 (12개 파일) | **CLI 명령**: 6개 | **스킬**: 3개

### Phase 8a: CodeWiki 모듈 트리 자동 생성 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| DAG 구축 (High+Medium confidence edges) | `index/dag.py` | ~310 |
| Connected Components (BFS 무방향 탐색) | `index/dag.py` | (포함) |
| Topological Sort (Kahn's algorithm, 사이클 내성) | `index/dag.py` | (포함) |
| 모듈 이름 자동 유도 (공통 디렉토리 기반) | `index/dag.py` | (포함) |
| 대형 모듈 분할 (MAX_MODULE_CHUNKS=40) | `index/dag.py` | (포함) |
| 고립 노드 디렉토리 기반 폴백 그룹핑 | `index/dag.py` | (포함) |
| `generate-wiki-plan` CLI 명령 | `cli.py` | +70 |
| `verify-wiki` CLI 명령 | `cli.py` | +50 |
| 테스트 24개 | `tests/test_dag.py` | ~280 |

**핵심 설계**: CodeWiki (ACL 2026) 파이프라인 Step 1-3 구현. call_edges에서 High+Medium confidence edge만 추출하여 방향성 DAG 구축 → 무방향 BFS로 connected component 식별 (= 1개 기능 모듈) → Kahn's algorithm으로 위상정렬 (bottom-up 처리 순서). 고립 노드(call edge 없는 청크)는 디렉토리 기반 그룹핑으로 폴백. `wiki-plan.json` 파일 출력으로 downstream 스킬/Agent 연동 가능.

**실측 결과** (hybrid-search-mcp):
- 9개 graph-based 모듈 + 10개 isolated 그룹
- 491/492 chunks 커버 (99.8%)
- Entry point 자동 식별: `cli.py::main`, `handle_hybrid_search`, `SearchOrchestrator.hybrid_search` 등

**총 코드**: ~8,500줄 (34개 파일) | **MCP 도구**: 13개 | **테스트**: 212개 (13개 파일) | **CLI 명령**: 8개 | **스킬**: 3개

### Phase 8b: Wiki 페이지 자동 생성 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| 모듈별 구조적 wiki 마크다운 생성 | `index/dag.py` (`generate_module_wiki`) | +130 |
| 전체 프로젝트 wiki 일괄 생성 | `index/dag.py` (`generate_all_wiki_pages`) | +40 |
| `generate-wiki` CLI 명령 (디스크 + DB sync) | `cli.py` | +75 |
| 테스트 6개 추가 | `tests/test_dag.py` | +95 |

**핵심 설계**: LLM 호출 없이 코드 메타데이터만으로 구조적 wiki 생성. 각 모듈 페이지에: 파일 목록, entry points, 심볼별 call/called-by 관계, 외부 의존성. `generate-wiki` CLI가 디스크 `.hybrid-search/wiki/`에 쓰고 DB에 자동 sync (staleness 추적 포함).

**실측 결과** (hybrid-search-mcp):
- 20개 wiki 페이지 생성 (index + 9 graph + 10 isolated)
- 18개 DB sync 완료
- 자동 생성된 페이지: tools.md (19 symbols, call 관계 포함), search.md, storage.md 등

**총 코드**: ~9,000줄 (34개 파일) | **MCP 도구**: 13개 | **테스트**: 218개 (13개 파일) | **CLI 명령**: 9개 | **스킬**: 3개

### Phase 8c: verify-wiki 강화 ✅

| 항목 | 변경 |
|------|:----:|
| query_key 기반 정확한 매칭 (title 대소문자 비교 → normalize_query) | 버그 수정 |
| uncovered 파일 목록 출력 | 신규 |
| `--json` 플래그 (JSON 구조화 출력) | 신규 |
| staleness 상세 리포트 (fresh/stale 카운트 + changed files) | 강화 |

### Phase 8d: 전체 파이프라인 자동화 ✅

| 항목 | 변경 |
|------|:----:|
| `reindex` 후 자동 call graph re-resolution | 신규 |
| `reindex --wiki` 플래그 → generate-wiki 자동 체인 | 신규 |

**전체 파이프라인**:
```
git commit
  └→ post-commit hook
     └→ reindex (delta)
        └→ call graph re-resolve (자동)
           └→ wiki sync (기존 wiki 있으면 자동)

reindex --wiki (명시적)
  └→ delta reindex
     └→ call graph re-resolve
        └→ generate-wiki (모듈 트리 → wiki 생성 + DB sync)
```

### Phase 8e: Wikilink 그래프 (GraphRAG) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| `[[링크]]` 파싱 + DB 동기화 (`_sync_wikilinks`) | `storage/wiki.py` | +40 |
| BFS 양방향 그래프 탐색 (`_expand_graph`) | `storage/wiki.py` | +80 |
| `wiki_links` 테이블 (source, target, link_text) | `storage/db.py` | +10 |
| lookup_page → linked_pages 자동 확장 | `storage/wiki.py` | (통합) |

**핵심 설계**: compile_page/refresh_page 시 `[[텍스트]]` 패턴을 자동 파싱하여 wiki_links 테이블에 저장. lookup_page 호출 시 BFS(max_hops=2, max_pages=10)로 양방향 탐색하여 linked_pages 반환. CLAUDE.md 규칙: "wiki에 [[링크]]가 있으면 연결된 페이지도 반드시 읽을 것."

### Phase 9a: LLM Wiki Synthesis (prepare/finalize) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| Synthesizer (prepare/finalize/verify/merge/hash) | `index/synthesizer.py` | ~430 |
| DB 스키마 v3 마이그레이션 (synthesis_* 4컬럼) | `storage/db.py` | +30 |
| WikiStore synthesis 필드 + 헬퍼 메서드 7개 | `storage/wiki.py` | +60 |
| SynthesisConfig | `config.py` | +5 |
| `synthesize-wiki` CLI (--dry-run, --module, --finalize) | `cli.py` | +130 |
| 테스트 27개 | `tests/test_synthesizer.py` | ~280 |

**핵심 설계**: Claude Code 자체가 LLM이므로 외부 API 키 불필요. 3단계 구조:
1. CLI `synthesize-wiki` → `_synthesis_input/*.md`에 컨텍스트 수집 (DB IO만, 토큰 0)
2. Claude Code가 컨텍스트 파일 Read → 합성 작성 → `_synthesis_output/*.md`에 Write
3. CLI `synthesize-wiki --finalize` → 참조 검증 + 결정론적 wiki 병합 + `_raw/` 백업 + DB 저장 (토큰 0)

합성 결과는 상단(Overview, Key Design Decisions, Data Flow, Caveats, Related Modules) + 하단 `<details>` 접기(결정론적 구조 데이터) 형태. synthesis_hash로 변경 감지하여 불필요한 재합성 방지.

**E2E 검증** (AST Chunker): 9개 참조 100% 검증 통과, 0개 제거.

**총 코드**: ~10,000줄 (36개 파일) | **MCP 도구**: 3개 | **테스트**: 241개 (14개 파일) | **CLI 명령**: 13개 | **스킬**: 3개

### Phase 9b: 전체 모듈 Bottom-Up 합성 ✅

| 항목 | 변경 |
|------|:----:|
| 28개 모듈 일괄 prepare → Claude Code 합성 → finalize | 완료 |
| `finalize_module` 타이틀 매칭 버그 수정 (slug vs 원본 이름) | `index/synthesizer.py` |
| 중복 RAW 페이지 정리 (18개 삭제) | DB cleanup |

**실행 결과**:
- 28/28 모듈 합성 완료 (100%)
- 참조 검증: 총 108개 refs verified, 29개 removed (73% 검증률)
- `_raw/` 백업: 20개 원본 결정론적 wiki 보존
- DB: 28 pages, 28 synthesized (중복 없음)

**발견된 버그 & 수정**:
1. `finalize_module`에서 `find_page_by_title`에 raw slug(대시 포함)를 전달 → LIKE 매칭 실패
   - 수정: 원본 이름 → 대시-공백 변환 순서로 2단계 fallback 시도
2. `collect_module_context`에도 동일 패턴 적용
3. `--` 포함 타이틀 (예: "Embedder -- OpenAI API Backend")은 `replace("-", " ")`로 4개 공백 생성 → LIKE 불일치
   - 수정: 원본 이름 먼저 시도하는 fallback 체인

### Phase 9c: 지식 복리 (Incremental Re-synthesis) ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| `should_skip_synthesis()` — staleness 기반 skip | `index/synthesizer.py` | +35줄 |
| `get_synthesis_hash()` — DB 저장 hash 조회 | `storage/wiki.py` | +7줄 |
| `find_indirectly_affected()` — wikilink BFS 간접 영향 | `storage/wiki.py` | +25줄 |
| `_auto_prepare_synthesis()` — reindex → prepare 체이닝 | `cli.py` | +70줄 |
| `reindex --synthesize` 플래그 | `cli.py` | +5줄 |
| CLI prepare에서 hash skip 로직 | `cli.py` | +10줄 |
| 테스트 8개 | `tests/test_synthesizer.py` | +100줄 |

**핵심 설계**: `should_skip_synthesis()`는 file_hash_at_compile vs 현재 file_hash 비교 (staleness 기반). synthesis_hash는 finalize 시 변경되므로 단순 hash 비교는 false positive 발생 — staleness 기반이 정확함. `reindex --synthesize`로 stale 감지 후 자동 prepare, `find_indirectly_affected()`로 wikilink 1-hop 이웃 모듈도 선택적 re-prepare.

**전체 파이프라인**:
```
reindex --synthesize
  └→ delta reindex
     └→ call graph re-resolve
        └→ _mark_stale_wikis → STALE.md
           └→ _auto_prepare_synthesis
              ├→ stale 모듈 prepare (skip if unchanged)
              └→ indirect 모듈 prepare (wikilink 1-hop)
```

### Phase 9d: 환각 검증 자동화 ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| `verify_symbols()` — backtick 심볼 DB 존재 검증 | `index/synthesizer.py` | +55줄 |
| `SymbolVerificationResult` 데이터 클래스 | `index/synthesizer.py` | +5줄 |
| `has_chunk_matching_name()` — qualified_name LIKE 검색 | `storage/db.py` | +7줄 |
| `verify-synthesis` CLI (--json, --fix) | `cli.py` | +110줄 |
| 테스트 6개 | `tests/test_synthesizer.py` | +60줄 |

**핵심 설계**: 2종 검증 — (1) file:line 참조 (기존 `verify_references()`) + (2) backtick 심볼명 (`verify_symbols()`). 심볼 검증은 PascalCase/snake_case 식별자를 추출하여 chunks.name 또는 chunks.qualified_name에서 확인. `_SYMBOL_SKIP` 집합으로 common words (true, false, self 등) 필터링. `--fix` 플래그로 bad refs 자동 제거.

**CLI 명령**:
```bash
python -m hybrid_search.cli verify-synthesis --cwd .         # 전체 합성 검증 리포트
python -m hybrid_search.cli verify-synthesis --json --cwd .  # JSON 출력
python -m hybrid_search.cli verify-synthesis --fix --cwd .   # bad refs 자동 제거
```

**총 코드**: ~9,700줄 (34개 파일) | **MCP 도구**: 3개 | **테스트**: 255개 (14개 파일) | **CLI 명령**: 15개 | **스킬**: 3개

### Phase 10: LLM 재랭킹 (Claude Code Native) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| RerankingConfig (`[search.reranking]` TOML 섹션) | `config.py` | +10 |
| Orchestrator — reranking 시 확장 후보 반환 | `search/orchestrator.py` | +10 |
| HybridSearchResponse에 `reranked` 필드 | `search/orchestrator.py` | +2 |
| `rerank_hint` — Claude Code 재랭킹 지시 | `tools/hybrid_search.py` | +20 |
| 테스트 16개 | `tests/test_reranker.py` | ~190 |

**핵심 설계**: Phase 9a와 동일 원칙 — Claude Code 자체가 LLM이므로 외부 API 키 불필요. `hybrid_search` MCP 도구가 RRF top-20 후보를 enriched 메타데이터(name, file_path, snippet, node_type)와 함께 반환. `rerank_hint` 메시지가 Claude Code에게 "쿼리 의도에 맞게 재정렬하여 상위 10개만 제시하라"고 지시. API 호출 0, 추가 비용 0, 지연 0.

**설정**:
```toml
[search.reranking]
enabled = true                       # 기본 false
max_candidates = 20                  # RRF에서 가져올 후보 수
```

**파이프라인**:
```
쿼리 → BM25 + Vector → RRF fusion (top-20 enriched) → Claude Code 재랭킹 → top-10
```

**총 코드**: ~9,800줄 (35개 파일) | **MCP 도구**: 3개 | **테스트**: 271개 (15개 파일) | **CLI 명령**: 15개 | **스킬**: 3개

### MCP 도구 슬림화: 13→3 ✅

**이유**: MCP 도구 스키마가 매 대화 시스템 프롬프트에 로드되어 토큰 소모. 관리/wiki 도구 10개를 CLI로 이관.

| 잔류 MCP 도구 | 이유 |
|:------------:|------|
| `hybrid_search` | 핵심 검색 (semantic_search 병합: bm25_weight=0) |
| `trace_callers` | 대화 중 역방향 call graph 추적 |
| `trace_callees` | 대화 중 순방향 call graph 추적 |

| CLI로 이관 (10개) | CLI 명령 |
|:----------------:|----------|
| `index_project` | `reindex` |
| `index_status` | `status` |
| `list_projects` | `status` |
| `remove_project` | `remove-project` (신규) |
| `search_symbols` | `search-symbols` (신규) |
| `semantic_search` | `hybrid_search`에 병합 |
| `compile_to_wiki` | `generate-wiki` |
| `lookup_wiki` | `lookup-wiki` (신규) |
| `check_wiki_staleness` | `stale` |
| `refresh_wiki_page` | `sync-wiki` |

**총 코드**: ~9,200줄 (34개 파일) | **MCP 도구**: 3개 | **테스트**: 218개 (13개 파일) | **CLI 명령**: 12개 | **스킬**: 3개

---

## 실전 검증 결과

### breeze 프로젝트 (소규모)
- **규모**: 155파일, 326 chunks, 90초 (CPU)
- **한국어 검색**: "할일 관리" → action-item-calendar.tsx, today-focus-hero.tsx 등 정확 매칭
- **검색 속도**: 741ms (hybrid_search)

### valuein-homepage 프로젝트 (대규모) — 2026-04-13 추가
- **규모**: 1,757파일, 9,559 chunks, 229초 (CPU) / 193초 (MPS)
- **한국어 검색 테스트** (4/4 정확 매칭):
  - "학원비 결제 처리" → `tuition-billing.md` 학원비 상세 (464ms)
  - "로그인 인증" → `auth/rules.md` + `login/page.tsx` (403ms)
  - "학생 출결 관리" → `learning-attendance.md` 출석부 개요 (369ms)
  - "캘린더 일정 표시" → `calendar_events.md` + `schedule/page.tsx` (364ms)

### 공통
- **임베딩 모델**: OpenAI `text-embedding-3-small` (1536차원, HTTP API)
  - **비용**: 인덱싱 ~$0.04/프로젝트, 검색 사실상 무료
  - **로컬 리소스**: CPU/메모리 부하 제로 (urllib만 사용)

---

## 즉시 해야 할 것

Phase 10 완료. 다음: Phase 11 (RAG 답변 생성).

---

## 아직 안 한 것

### Phase 11: RAG 답변 생성
- wiki + 검색 결과 → 자연어 답변 (코드베이스 Q&A)

---

## 실행 환경

```bash
# 가상환경 활성화
cd /Users/ian/project/claude_project/hybrid-search-mcp
source .venv/bin/activate

# 서버 실행 (Claude Code MCP로 자동 실행됨)
python -m hybrid_search.server

# CLI 명령
python -m hybrid_search.cli reindex --cwd .     # delta 재인덱싱
python -m hybrid_search.cli status               # 인덱스 상태
python -m hybrid_search.cli stale --cwd .        # wiki staleness
python -m hybrid_search.cli install-hook --cwd . # post-commit hook 설치
python -m hybrid_search.cli sync-wiki --cwd .    # 디스크 wiki → DB 동기화
python -m hybrid_search.cli reindex --synthesize --cwd .       # reindex + stale → auto prepare
python -m hybrid_search.cli synthesize-wiki --cwd .            # prepare: 컨텍스트 수집
python -m hybrid_search.cli synthesize-wiki --dry-run --cwd .  # dry-run: 토큰 추정
python -m hybrid_search.cli synthesize-wiki --finalize --cwd . # finalize: 검증+병합+DB저장
python -m hybrid_search.cli verify-synthesis --cwd .           # 합성 검증 (refs + symbols)
python -m hybrid_search.cli verify-synthesis --fix --cwd .     # 검증 + bad refs 자동 제거

# 테스트
python -m pytest tests/ -v

# 인덱스 데이터 위치
~/.hybrid-search/projects/{project_hash}/
~/.hybrid-search/global/project_registry.db
~/.hybrid-search/config.toml
```

### MCP 설정 위치

`~/.claude.json`에 등록됨 (글로벌 MCP 서버):

```json
{
  "mcpServers": {
    "hybrid-search": {
      "command": "/Users/ian/project/claude_project/hybrid-search-mcp/.venv/bin/python",
      "args": ["-m", "hybrid_search.server"]
    }
  }
}
```

### 스킬 위치

```
~/.claude/skills/bootstrap-wiki/skill.md  — 프로젝트 wiki 자동 생성
~/.claude/skills/save-wiki/skill.md       — 대화 중 분석 → wiki 저장
~/.claude/skills/search/skill.md          — wiki-first 4단계 검색 체인
```

---

## 알려진 이슈 & 교훈

1. **FK CASCADE 주의** (§18 #6): `INSERT OR REPLACE`는 SQLite에서 DELETE+INSERT로 동작 → FK CASCADE 발동. 반드시 `ON CONFLICT DO UPDATE` 사용.

2. **Python 3.13 sqlite3** (§18 #7): `isolation_level` 기본값 변경됨. `isolation_level=None` + 명시적 `conn.commit()` 패턴 사용 중.

3. **tree-sitter-languages 미지원**: Python 3.13에서 `tree-sitter-languages` 패키지가 안 됨. 개별 grammar 패키지로 전환 완료.

4. **MindVault 공존** (§15): MindVault hook 토큰 예산을 10000→3000으로 축소하고 글로벌 폴백을 비활성화함.

5. **tree-sitter byte offset** (§8, §18 #8): tree-sitter는 UTF-8 byte offset을 반환. 반드시 `source_bytes = source.encode()` 후 `source_bytes[start:end].decode()` 사용.

6. **Transaction 캡슐화**: `db._conn` 직접 접근은 partial write 위험. 항상 `db.transaction()` context manager 사용.

7. **callee_chunk_id에 FK 없음**: `call_edges.callee_chunk_id`는 FK 제약 없음 (resolve 전 NULL). 파일 삭제 시 `delete_call_edges_by_callee()`로 dangling reference 명시 정리 필요.

8. **스킬은 지시서일 뿐**: Claude가 스킬의 모든 단계를 실행한다고 보장할 수 없음. 핵심 동작(DB 동기화 등)은 CLI 명령으로 확정적으로 실행하는 것이 안전. 예: `sync-wiki` CLI가 `compile_to_wiki` MCP 도구 호출을 대체.

9. **DB 스키마 버전은 int 비교**: `_migrate_schema()`에서 int() 변환 후 비교. 문자열 비교 시 "9" < "10"이 False가 되는 문제 해결 (v3에서 수정).

10. **WikiStore 캡슐화**: `db._conn` 직접 접근 금지. synthesizer/CLI 등 외부에서는 WikiStore의 public 헬퍼 메서드(`get_page_row`, `find_page_by_title`, `get_page_file_hashes`, `get_page_deps`, `get_linked_page_ids`, `is_synthesized` 등) 사용.

11. **Slug↔Title 매칭 주의** (Phase 9b): `finalize_module`에서 파일명 slug(예: `call-graph-&-module-tree`)를 DB 타이틀(예: `Call Graph & Module Tree`)로 변환할 때, `replace("-", " ")`만으로는 부족. `--` 포함 타이틀은 공백 4개가 되어 LIKE 불일치 발생. 해결: 원본 이름 → 대시-공백 변환 2단계 fallback.

12. **합성 에이전트의 파일 쓰기 불안정**: Claude Code의 sub-agent(Agent 도구)에게 파일 쓰기를 위임하면 실제로 파일이 작성되지 않는 경우가 빈번. 핵심 파일 쓰기는 메인 세션에서 직접 수행하거나 Bash heredoc 사용이 안정적.

13. **synthesis_hash로 skip 판단 불가** (Phase 9c): `finalize_module()`이 merged content를 DB에 저장하므로, 이후 `collect_module_context()`가 읽는 deterministic_wiki가 달라져 hash가 불일치. 해결: staleness 기반(file_hash_at_compile vs 현재 file_hash) 비교가 정확.

14. **심볼 검증은 noise 관리가 핵심** (Phase 9d): backtick 안의 모든 텍스트가 심볼은 아님. `_SYMBOL_SKIP` (common words) + 파일 경로 필터 + 길이 제한으로 false positive 최소화.

---

## 핵심 설계 결정 (빠른 참조)

| 결정 | 선택 | 이유 (design.md 참조) |
|------|------|----------------------|
| 언어 | Python + 네이티브 확장 | §4: MCP SDK 성숙, 핵심 연산은 C++/Rust |
| 임베딩 | OpenAI text-embedding-3-small | §7: 로컬 리소스 제로, ~$0.04/프로젝트. urllib만 사용 |
| BM25 | tantivy-py | §4: Rust 백엔드, Lucene급 성능 |
| Vector DB | USearch HNSW | §4: C++ SIMD 최적화, M=16 |
| 청크 크기 | 비공백 4000자 | §8: cAST 논문 근거, 줄 수보다 정확 |
| RRF k값 | 60 | §11: Cormack et al. 원논문 표준값 |
| 쿼리 분류 | 3단계 (SYMBOL/KR/EN) | §11: 자동 BM25 가중치 조절 |
| Storage | per-project store.db (SQLite WAL) | §13: 트랜잭션 일관성 + 동시 읽기 |
| Call Graph | 4단계 resolution + module index + class members | §12 + Phase 7: import-call 바인딩, self/this 추적 |
| Wiki | DB(staleness) + 디스크(.md) 이중 저장 | Phase 5+6: DB로 추적, 디스크로 CLAUDE.md 참조 |
| CLI | sync-wiki로 확정적 DB 동기화 | Phase 6a: 스킬 의존 대신 CLI로 확실한 실행 |
| Wikilink | `[[링크]]` BFS 그래프 (max_hops=2) | Phase 8e: 페이지 간 관계 자동 추적 + 지식 복리 기반 |
| Synthesis | Claude Code가 직접 합성, API 키 불필요 | Phase 9a: CLI prepare/finalize로 토큰 최소화 |
| 전체 합성 | 28개 모듈 bottom-up 일괄, slug 2단계 fallback | Phase 9b: 참조 검증 73%, `_raw/` 백업 보존 |
| Skip 판단 | staleness 기반 (file_hash 비교), synthesis_hash 아님 | Phase 9c: finalize 후 content 변경으로 hash 비교 불가 |
| 간접 전파 | wikilink BFS 1-hop 이웃 모듈 auto-prepare | Phase 9c: stale 모듈의 이웃도 Related Modules 갱신 |
| 환각 검증 | file:line refs + symbol DB 존재 확인, `--fix` 자동 정리 | Phase 9d: 2종 검증으로 합성 품질 보장 |
| LLM 재랭킹 | Claude Code native, rerank_hint로 지시, API 키 불필요 | Phase 10: Phase 9a 원칙 동일 — Claude Code가 LLM |
