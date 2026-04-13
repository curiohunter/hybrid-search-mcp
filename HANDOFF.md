# Hybrid Search MCP — Handoff Document

> **Date**: 2026-04-13 | **Branch**: main
> **설계 문서**: `docs/design.md` (v5, 전체 아키텍처 + 18개 섹션)

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
| 쿼리 분류기 (SYMBOL/KR/EN) | §11 | `search/orchestrator.py` | 405 |
| `hybrid_search` tool | §10.1 | `tools/hybrid_search.py` | 51 |
| 멀티 프로젝트 + cross-project 검색 | §13 | `search/orchestrator.py` | (포함) |

### 지원 모듈 ✅

| 파일 | 역할 | 줄수 |
|------|------|:----:|
| `config.py` | TOML 설정 로딩, 모델 토큰 자동감지 | 207 |
| `project.py` | 글로벌 프로젝트 레지스트리 (SQLite) | 114 |
| `storage/db.py` | per-project store.db (WAL, FK CASCADE) | ~500 |
| `storage/indexes.py` | 인덱스 경로 관리 | 45 |
| `index/pipeline.py` | 인덱싱 오케스트레이션 (multi-store 트랜잭션) | ~315 |

### Phase 3a: Call Graph ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Call Graph Resolution (3단계 confidence) | §12 | `index/callgraph.py` | 155 |
| trace_callers/trace_callees 도구 | §10.3, §10.4 | `tools/trace.py` | 250 |
| StoreDB call graph 쿼리 (10개 메서드) | §12, §13 | `storage/db.py` (추가) | +180 |
| AST byte offset 버그 수정 | §8 | `index/ast_chunker.py` (수정) | — |

**검증**: 1,934 call edges, 146 resolved, 0 dirty. trace depth 2에서 정확한 caller/callee 추적.

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

**총 코드**: ~5,200줄 (26개 파일) | **MCP 도구**: 9개 | **테스트**: 133개 (9개 파일)

---

## 실전 검증 결과

- **breeze 프로젝트**: 155파일, 326 chunks 인덱싱 완료
- **한국어 검색**: "할일 관리" → action-item-calendar.tsx, today-focus-hero.tsx 등 정확 매칭
- **검색 속도**: 741ms (hybrid_search)
- **임베딩 모델**: `intfloat/multilingual-e5-small` (sentence-transformers 백엔드)
  - 벤치마크 승자는 Qwen3-0.6B(R@1=0.83)이나, e5-small이 속도/크기 밸런스로 운영 중
  - config.toml에서 모델 변경 시 자동 rebuild

---

## 아직 안 한 것 — 다음 작업 가이드

### ~~Phase 3b: 추가 언어 지원~~ ✅ 완료

14개 AST 언어 지원 완료: TypeScript, JavaScript, Python, Rust, Go, Ruby, Java, C, C++, Swift, Kotlin, CSS, SCSS, SQL. HTML은 fallback chunking 사용.

### Phase 4: Polish (design.md §16) ✅

| 항목 | 설명 | 상태 |
|------|------|:----:|
| ~~테스트 확충~~ | 133개 테스트 (9개 파일): RRF, 쿼리 분류, AST 청킹(6개 언어), call graph, doc chunker, config, scanner, embedder, hot-reload | ✅ |
| ~~크래시 복구~~ | (1) consistency mismatch → 자동 force rebuild (2) `file_hash=""` partial write 감지 → 재인덱싱 | ✅ |
| ~~ONNX 백엔드~~ | `_embed_onnx_batch()` 완전 구현 (mean pooling + L2 normalize) | ✅ |
| ~~Ollama 백엔드~~ | `POST /api/embed` HTTP API 백엔드 (config: `backend = "ollama"`, `ollama_model = "nomic-embed-text"`) | ✅ |
| ~~Apple Silicon MPS~~ | ONNX: CoreMLExecutionProvider 자동 감지, ST: `device="mps"` 전달 | ✅ |
| ~~인덱싱 진행률~~ | `ProgressCallback(current, total, path)` — pipeline→tools 콜백 체인 | ✅ |
| ~~config.toml 핫 리로드~~ | `_HotReloadableConfig` mtime 감지 + mutable state dict 패턴으로 closure 버그 수정 | ✅ |

### 코드 리뷰에서 남긴 향후 리팩토링 대상

| 항목 | 파일 | 설명 |
|------|------|------|
| dict 반환 → exception | `tools/trace.py` `_open_stores` | 에러를 dict로 반환하는 패턴 → 타입 안전한 exception |
| fuzzy search 비결정성 | `tools/trace.py` `_resolve_start` | 짧은 symbol로 LIKE 검색 시 첫 번째 결과만 사용 → 비결정적 |
| _split_large_chunks start_line | `index/ast_chunker.py` | P3 — "rough char estimate" 근사치, 수용 가능 |

### Phase 5 후보: Reactive Wiki Layer (design.md §18 #9)

검색 결과를 wiki 페이지로 "컴파일"하여 반복 질문 시 검색 없이 즉시 답변. Phase 2 완성 + 실사용 데이터 축적 후 재검토.

---

## 실행 환경

```bash
# 가상환경 활성화
cd /Users/ian/project/claude_project/hybrid-search-mcp
source .venv/bin/activate

# 서버 실행 (Claude Code MCP로 자동 실행됨)
python -m hybrid_search.server

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

---

## 알려진 이슈 & 교훈

1. **FK CASCADE 주의** (§18 #6): `INSERT OR REPLACE`는 SQLite에서 DELETE+INSERT로 동작 → FK CASCADE 발동. 반드시 `ON CONFLICT DO UPDATE` 사용. (`storage/db.py`에서 이미 수정됨)

2. **Python 3.13 sqlite3** (§18 #7): `isolation_level` 기본값 변경됨. `isolation_level=None` + 명시적 `conn.commit()` 패턴 사용 중.

3. **tree-sitter-languages 미지원**: Python 3.13에서 `tree-sitter-languages` 패키지가 안 됨. 개별 grammar 패키지(`tree-sitter-python`, `tree-sitter-typescript` 등)로 전환 완료.

4. **MindVault 공존** (§15): MindVault hook 토큰 예산을 10000→3000으로 축소하고 글로벌 폴백을 비활성화함. 설정: `~/.claude/hooks/mindvault-hook.sh`

5. **tree-sitter byte offset** (§8, §18 #8): tree-sitter는 UTF-8 byte offset을 반환하지만 Python str은 문자 단위. 멀티바이트 문자가 있으면 `source[node.start_byte:node.end_byte]`는 틀린 결과를 줌. 반드시 `source_bytes = source.encode()` 후 `source_bytes[start:end].decode()` 사용. (`ast_chunker.py`에서 수정됨)

6. **Transaction 캡슐화**: `db._conn` 직접 접근은 partial write 위험. 항상 `db.transaction()` context manager 사용. (`pipeline.py`, `callgraph.py`에서 수정됨)

7. **callee_chunk_id에 FK 없음**: `call_edges.callee_chunk_id`는 FK 제약 없음 (resolve 전 NULL). 파일 삭제 시 `delete_call_edges_by_callee()`로 dangling reference 명시 정리 필요.

---

## 핵심 설계 결정 (빠른 참조)

| 결정 | 선택 | 이유 (design.md 참조) |
|------|------|----------------------|
| 언어 | Python + 네이티브 확장 | §4: MCP SDK 성숙, 핵심 연산은 C++/Rust |
| 임베딩 | e5-small (sentence-transformers) | §7: 속도/크기 밸런스, 품질 필요시 Qwen3 전환. 3개 백엔드: ST/ONNX/Ollama |
| BM25 | tantivy-py | §4: Rust 백엔드, Lucene급 성능 |
| Vector DB | USearch HNSW | §4: C++ SIMD 최적화, M=16 |
| 청크 크기 | 비공백 4000자 | §8: cAST 논문 근거, 줄 수보다 정확 |
| RRF k값 | 60 | §11: Cormack et al. 원논문 표준값 |
| 쿼리 분류 | 3단계 (SYMBOL/KR/EN) | §11: 자동 BM25 가중치 조절 |
| Storage | per-project store.db (SQLite WAL) | §13: 트랜잭션 일관성 + 동시 읽기 |
| Call Graph | 3단계 confidence + common name 필터 | §12: High/Medium/Low + noise 관리 |
