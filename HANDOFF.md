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

**스킬 검색 체인** (`/search`):
```
1. lookup_wiki (DB) → found+fresh → 즉시 반환
2. wiki/index.md (디스크) → Read로 확인
3. hybrid_search (MCP) → 결과 좋으면 compile_to_wiki로 축적
4. Grep/Glob (폴백) → 직접 검색
```

**설치된 hook**: valuein-homepage, breeze 프로젝트에 post-commit hook 설치 완료.

**valuein-homepage wiki 부트스트랩 완료**: 10개 wiki 페이지 생성 + DB 동기화 (sync-wiki). architecture, students, tuition-billing, attendance, learning-data, homework-analysis, diagnosis, portal, consultation, edge-functions.

**총 코드**: ~7,300줄 (31개 파일) | **MCP 도구**: 13개 | **테스트**: 175개 (12개 파일) | **CLI 명령**: 5개 | **스킬**: 3개

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
- **임베딩 모델**: `intfloat/multilingual-e5-small` (sentence-transformers 백엔드)
  - **권장 설정**: `device = "cpu"` + `onnx_threads = 6`
  - **향후 최적화**: ONNX + INT8 arm64 quantization (예상 2.7-3.3x 가속)

---

## 즉시 해야 할 것 — Call Graph Resolution 고도화 (Step 1)

### 문제: Resolution Rate 7.5% (146/1934)

**근본 원인**: call 추출 시 import 정보를 연결하지 않음.

현재 흐름 (끊어진 체인):
```
_extract_imports() → ["./auth", "./billing"]   ← 파일 레벨에서 추출 ✅
_extract_calls()   → ["login", "charge"]       ← 함수 내부에서 추출 ✅
insert_call_edges() → callee_name="login", callee_module=NULL  ← 연결 안 됨! ❌
```

**결과**: High confidence 전략이 **절대 발동하지 않음** (callee_module이 항상 NULL)

### Step 1 구현 계획: Import-Call 바인딩 (예상 7.5% → 55-60%)

**변경 파일 3개**:

#### 1. `index/ast_chunker.py` — call 추출 시 import 정보 전달

현재 `_extract_call_name()` (줄 639-662): bare name만 반환 (`"login"`)

변경: import 목록을 받아서 call과 매칭, `(name, module)` 튜플 반환

```python
# 현재
def _extract_calls(node, source_bytes, language) -> list[str]:
    ...
    name = _extract_call_name(desc, source_bytes, language)
    calls.append(name)

# 변경
def _extract_calls(node, source_bytes, language, imports) -> list[tuple[str, str | None]]:
    ...
    name = _extract_call_name(desc, source_bytes, language)
    module = _match_import(name, imports, language)  # 새 함수
    calls.append((name, module))
```

핵심 새 함수 `_match_import()`:
```python
def _match_import(call_name: str, imports: list[str], language: str) -> str | None:
    """Match a call name to its import source."""
    # TS/JS: import { login } from "./auth"  →  call_name="login", return "./auth"
    # Python: from src.auth import login     →  call_name="login", return "src.auth"
    # 이미 추출된 import 문자열을 파싱하여 name → module 매핑 구축
```

**주의**: `_extract_imports()`는 현재 raw import 문자열을 반환 (예: `"./auth"`, `"from src.auth import login"`). TS/JS는 모듈 경로만, Python은 전체 import 문이 저장됨. `_match_import()`에서 언어별 파싱 필요:
- **TS/JS**: `import { A, B } from "./path"` → A→"./path", B→"./path"
- **Python**: `from X.Y import Z` → Z→"X.Y"
- **Go**: `import "pkg/path"` → 패키지 이름→"pkg/path"

#### 2. `storage/db.py` — insert_call_edges에 callee_module 전달

현재 (줄 318-333):
```python
def insert_call_edges(self, conn, caller_chunk_id, calls: list[str], project_id):
    conn.executemany(
        "INSERT INTO call_edges (caller_chunk_id, callee_name, project_id, confidence) VALUES (?, ?, ?, 'low')",
        [(caller_chunk_id, name, project_id) for name in calls],
    )
```

변경:
```python
def insert_call_edges(self, conn, caller_chunk_id, calls: list[tuple[str, str | None]], project_id):
    conn.executemany(
        "INSERT INTO call_edges (caller_chunk_id, callee_name, callee_module, project_id, confidence) VALUES (?, ?, ?, ?, 'low')",
        [(caller_chunk_id, name, module, project_id) for name, module in calls],
    )
```

#### 3. `index/pipeline.py` — imports를 _extract_calls에 전달

현재 (줄 254-256):
```python
for c in chunks:
    if c.calls:
        db.insert_call_edges(conn, c.id, c.calls, project_id)
```

`chunk_code_file()` 내부에서 이미 `imports`가 추출되고 `chunk.imports = imports` (줄 202)로 설정됨.
변경: `_extract_calls(node, source_bytes, language, imports)` 호출 시 imports 전달.

실제 변경은 `ast_chunker.py`의 `_walk_node()` (줄 313)에서 `_extract_calls()` 호출 부분. 현재는 imports가 나중에 설정되므로, `chunk_code_file()`에서 먼저 imports를 추출하고 `_extract_chunks()`에 전달하는 순서 조정 필요.

#### 4. `index/callgraph.py` — 이미 High confidence 로직 있음

`_resolve_single()` (줄 110-113)에서 `callee_module`이 있으면 High confidence로 resolve. **이 코드는 변경 불필요** — callee_module이 NULL이 아니게 되면 자동으로 High가 발동.

### Step 1 테스트 계획

```python
# tests/test_callgraph.py에 추가
def test_import_call_binding_ts():
    """TS: import { login } from './auth' → login() call has callee_module='./auth'"""

def test_import_call_binding_python():
    """Python: from src.auth import login → login() call has callee_module='src.auth'"""

def test_high_confidence_with_module():
    """callee_module 있으면 High confidence로 resolve"""

def test_unmatched_call_still_works():
    """import에 없는 call은 callee_module=None, 기존 medium/low 로직으로 동작"""
```

### Step 1 이후 로드맵

| 단계 | 예상 Resolution Rate | 작업량 | 설명 |
|:----:|:--------------------:|:------:|------|
| **Step 1**: Import-Call 바인딩 | ~55-60% | 중 | ast_chunker + db + callgraph |
| **Step 2**: Module Path 역인덱스 | ~70-75% | 소 | import path → file_id 매핑 테이블 |
| **Step 3**: 메서드 receiver 추적 | ~85-90% | 중 | `this.method()` → 클래스 매핑 |
| **Step 4**: COMMON_NAMES 완화 | ~90-95% | 소 | context 있을 때 confidence 상향 |

**90%+ 달성 시**: CodeWiki 논문의 위상정렬 기반 자동 wiki 생성이 실용적이 됨. `/bootstrap-wiki`가 의존성 그래프에서 모든 도메인 기능을 자동 식별 가능.

---

## 아직 안 한 것

### Phase 6b 후보: CodeWiki 자동 wiki 생성

CodeWiki (ACL 2026) 방법론:
1. AST + 의존성 그래프 → 위상정렬로 모듈 트리 생성
2. zero-in-degree 노드 = 엔트리 포인트 (아무도 호출하지 않는 최상위)
3. 리프 → 부모로 상향식 문서 생성

**전제 조건**: Call Graph Resolution 90%+ (현재 7.5%). Step 1-4 완료 후 진행.

### ONNX INT8 quantization

ARM64 전용 INT8 양자화로 CPU 임베딩 2.7-3.3x 가속. e5-small 기준 인덱싱 229초 → ~80초 예상.

### MindVault BM25 대체

Hybrid Search가 안정화되면 MindVault의 BM25를 대체, MindVault는 Graph/Wiki 전담.

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
| Wiki | DB(staleness) + 디스크(.md) 이중 저장 | Phase 5+6: DB로 추적, 디스크로 CLAUDE.md 참조 |
| CLI | sync-wiki로 확정적 DB 동기화 | Phase 6a: 스킬 의존 대신 CLI로 확실한 실행 |
