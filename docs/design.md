# Hybrid Search MCP Server — Design Document

> **Status**: Draft v6 | **Date**: 2026-04-14 | **Author**: Ian + Claude
> **Review**: Codex review x3 + Brightdata 웹 리서치 반영 (v1→v5) + Karpathy LLM Wiki 비교 분석 (v6)

---

## Part I: 완료된 기반 (Phase 1-8a 요약)

### 검색 인프라 (Phase 1-4) ✅

BM25(Tantivy/Rust) + Vector(USearch/C++ HNSW) 하이브리드 검색 엔진. RRF(k=60) 합산, 3단계 쿼리 분류기(EXACT_SYMBOL 0.8 / KOREAN_NL 0.15 / ENGLISH_NL 0.4).

| 컴포넌트 | 스택 | 비고 |
|----------|------|------|
| MCP 서버 | `mcp[cli]` Python stdio | MCP 도구 3개 노출 |
| 임베딩 | OpenAI `text-embedding-3-small` | ~$0.04/인덱싱, 검색 무료 |
| AST 파싱 | tree-sitter (C) | 14개 언어, 함수/클래스 단위 청킹 |
| BM25 | tantivy-py (Rust) | content + name + qname + docstring |
| Vector DB | USearch (C++ HNSW) | cosine, M=16, ef=200 |
| 메타 | SQLite WAL | files, chunks, call_edges, wiki_* |

**MCP 도구**: `hybrid_search`, `trace_callers`, `trace_callees`
**CLI**: reindex, status, stale, sync-wiki, generate-wiki-plan, generate-wiki, verify-wiki 등 12개

### Call Graph (Phase 7) ✅

4전략 resolution: self/this → import path → qualified name → name-only. Module-linked resolve rate: Python 45.3%, TS/JS 66.2%. Built-in 필터 60+개.

### 자동 Wiki 생성 (Phase 8a) ✅

DAG 구축 → connected components (BFS) → Kahn's topological sort → 모듈 이름 자동 유도. 결정론적(deterministic) wiki — LLM 없이 파일 목록, entry point, 콜 관계 기술. Staleness 추적: `wiki_dependencies` 테이블의 file_hash 스냅샷.

### Wikilink 그래프 (GraphRAG) ✅

Wiki 페이지 내 `[[링크]]` 패턴을 자동 파싱하여 페이지 간 방향성 그래프를 구축. `wiki_links` 테이블(source_page_id, target_page_id, link_text)에 저장.

| 기능 | 구현 | 파일 |
|------|------|------|
| 링크 파싱 + DB 동기화 | `_sync_wikilinks()` | `storage/wiki.py:337` |
| BFS 양방향 그래프 탐색 | `_expand_graph()` | `storage/wiki.py:379` |
| 조회 시 자동 확장 | `lookup_page()` → `linked_pages` | `storage/wiki.py:179` |

`lookup_page()` 호출 시 `_expand_graph(max_hops=2, max_pages=10)`가 자동 실행되어 연결된 페이지의 title, snippet, hop 거리를 반환한다. outgoing + incoming 양방향 탐색.

### Phase 9a: LLM Wiki Synthesis (prepare/finalize) ✅

Claude Code 자체가 LLM이므로 외부 API 키 없이 합성하는 2단계 아키텍처:

| 단계 | 명령 | 토큰 | 설명 |
|------|------|:----:|------|
| Prepare | `synthesize-wiki --cwd .` | 0 | CLI가 DB에서 컨텍스트 수집 → `_synthesis_input/*.md` |
| Synthesize | Claude Code Read → Write | Claude Code 자체 | 컨텍스트 파일 읽고 합성 작성 → `_synthesis_output/*.md` |
| Finalize | `synthesize-wiki --finalize` | 0 | 참조 검증 + 병합 + `_raw/` 백업 + DB 저장 |

구현 파일:
- `index/synthesizer.py` — prepare/finalize + verify_references + merge + hash
- `storage/db.py` — 스키마 v3 (synthesis_* 4컬럼), int 기반 마이그레이션
- `storage/wiki.py` — WikiPage synthesis 필드 + WikiStore 헬퍼 메서드 7개
- `config.py` — SynthesisConfig (enabled 필드만, YAGNI)
- `cli.py` — `synthesize-wiki` (--dry-run, --module, --finalize)

E2E 검증 (AST Chunker 모듈):
- 9개 파일:라인 참조 → **100% 검증 통과**, 0개 제거
- 합성 결과: Overview + Key Design Decisions + Data Flow + Caveats + Related Modules
- 결정론적 wiki는 `<details>` 접기로 보존, `_raw/`에 원본 백업

### Phase 9b: 전체 모듈 Bottom-Up 합성 ✅

28개 모듈 일괄 합성 완료. 슬러그-타이틀 매칭 버그 발견 및 수정:
- `finalize_module`/`collect_module_context`에서 원본 이름 → 대시-공백 변환 2단계 fallback
- 참조 검증: 108 verified, 29 removed (73% 검증률)
- 28/28 pages synthesized, 중복 RAW 페이지 18개 정리

### 현재 상태 요약

```
인덱싱:    4개 프로젝트 등록, valuein-homepage 1,770파일/9,806청크
검색:      한→영 크로스 언어 동작, RRF fusion 정상
Wiki:      구조적 페이지 자동 생성 (call graph 기반 모듈 분해)
Wikilink:  페이지 간 [[링크]] 그래프 + BFS 탐색 동작
Synthesis: Phase 9b 완료 — 28/28 모듈 합성, API 키 불필요
```

**여기까지의 한계 (Phase 9b 이후)**:

1. **수동 트리거**: `synthesize-wiki --prepare` → Claude Code 합성 → `--finalize`를 사람이 실행해야 함. reindex 후 stale 감지 시 자동 합성 파이프라인은 아직 없음.

2. **참조 검증률 73%**: 29/137 참조가 검증 실패로 제거됨. 주로 정확한 라인 번호 불일치 — 환각은 아니나 정밀도 개선 여지.

---

## Part II: 다음 단계 — LLM 합성 Wiki 레이어

### 배경: Karpathy LLM Wiki와의 비교에서 배운 것

| 관점 | Karpathy LLM Wiki | hybrid-search-mcp |
|------|-------------------|-------------------|
| wiki 작성 | LLM이 전부 작성 (합성 설명) | 결정론적 코드 생성 (구조 나열) |
| 지식 복리 | 새 소스 → 기존 10-15페이지 업데이트 | 코드 변경 → stale 마킹만 |
| 환각 위험 | 높음 (소스와 drift 가능) | 없음 (코드에서 직접 생성) |
| 설명 품질 | 높음 (자연어 합성) | 낮음 (파일 목록, 콜 관계만) |
| 검색 | qmd 의존 (외부) | 내장 하이브리드 (BM25+Vector) |

**핵심 통찰**: 두 접근을 합치면 된다.
- 우리의 **결정론적 구조 데이터**(AST, call graph, staleness)를 ground truth로 유지
- 그 위에 **LLM 합성 레이어**를 얹어 설명적 wiki를 생성
- 환각을 구조 데이터로 제약 (grounded generation)

---

### Phase 9: LLM-Grounded Wiki Synthesis

> **목표**: 결정론적 코드 분석(AST, call graph) 위에 LLM 합성을 얹어,
> "왜 이 코드가 존재하는지"를 설명하는 wiki를 자동 생성.
> Karpathy식 지식 복리 + 우리의 환각 방지 인프라.

#### 9.1 아키텍처

```
[ 기존 인프라 — 변경 없음 ]

소스 코드
  │
  ▼
Scanner → AST Chunker → Embedder → Store
  │
  ▼
Call Graph Resolution → DAG → Module Tree (WikiPlan)
  │
  ▼
결정론적 Wiki (구조 데이터: 파일 목록, entry point, 콜 관계)
  │
  ▼
[ 새로운 레이어 ]
  │
  ▼
┌─────────────────────────────────────────────┐
│          LLM Synthesis Layer                 │
│                                              │
│  Input:                                      │
│    1. 결정론적 wiki (모듈 구조)              │
│    2. 실제 코드 (hybrid_search로 조회)       │
│    3. 연관 wiki 페이지 (wikilink graph)      │
│    4. git blame/log (변경 이력)              │
│                                              │
│  Output:                                     │
│    - 모듈 역할 설명 (한 문단)                │
│    - 핵심 설계 결정 & 이유                   │
│    - 데이터 흐름 다이어그램                   │
│    - 주의사항 / 함정                         │
│    - 연관 모듈과의 관계 설명                 │
│                                              │
│  Constraint:                                 │
│    - 결정론적 wiki의 사실과 모순 불가        │
│    - 코드에 없는 내용 생성 금지              │
│    - 출처(파일:라인) 필수 첨부               │
│                                              │
└─────────────────────────────────────────────┘
  │
  ▼
합성 Wiki 페이지 (.hybrid-search/wiki/*.md)
  │
  ▼
Staleness 추적 (기존 인프라 재사용)
```

#### 9.2 Wiki 페이지 구조 (before → after)

**Before (현재 — 결정론적)**:
```markdown
# auth-system
> 5 files, 12 chunks

## Files
- src/auth/signIn.ts
- src/auth/signOut.ts
- src/middleware.ts
...

## Entry Points
- signIn (0 callers → entry)
- createSupabaseMiddlewareClient (0 callers → entry)

## Call Relationships
- signIn → validateEmail, createSession
- signOut → clearSession, redirect
```

**After (LLM 합성 추가)**:
```markdown
# auth-system
> 5 files, 12 chunks | 마지막 합성: 2026-04-14

## 개요
Supabase Auth 기반 인증 시스템. 이메일/비밀번호 로그인과 세션 관리를 담당한다.
미들웨어(`createSupabaseMiddlewareClient`)가 모든 서버 요청에서 세션을 검증하고,
만료된 토큰을 자동 갱신한다.

## 핵심 설계 결정
- **서버 사이드 세션 검증**: 클라이언트가 아닌 미들웨어에서 토큰 검증
  → XSS로 탈취된 토큰의 서버 접근 차단 (`src/middleware.ts:L23`)
- **쿠키 기반 세션**: localStorage 대신 httpOnly 쿠키 사용
  → CSRF 보호는 Supabase SDK가 내장 (`src/auth/signIn.ts:L15`)

## 데이터 흐름
```
브라우저 → middleware (세션 검증) → signIn/signOut → Supabase Auth API
                ↓ 실패 시
           redirect → /login
```

## 주의사항
- `signIn`에서 `validateEmail`이 실패해도 Supabase에 요청이 전송됨
  — rate limiting 의존 (`src/auth/signIn.ts:L31`)
- `clearSession`은 서버 쿠키만 삭제, 클라이언트 상태는 별도 처리 필요

## 연관 모듈
- [[tuition-billing]] — 인증된 사용자만 접근, middleware 의존
- [[consultation]] — 상담 예약 시 세션에서 user_id 추출

## 구조 (자동 생성)
<details>
<summary>파일 목록 & 콜 관계 (결정론적)</summary>

### Files
- src/auth/signIn.ts
- src/auth/signOut.ts
...

### Call Relationships
- signIn → validateEmail, createSession
...
</details>
```

#### 9.3 합성 프로세스 (Bottom-Up, CodeWiki 방식)

Phase 8a에서 이미 구현된 WikiPlan의 위상정렬 순서를 그대로 활용:

```
Step 1: Leaf 모듈 합성 (병렬)
  - 각 leaf 모듈의 코드를 hybrid_search로 조회
  - 결정론적 wiki + 실제 코드 → LLM에게 합성 요청
  - 출력: 개요, 설계 결정, 데이터 흐름, 주의사항

Step 2: Parent 모듈 합성 (의존 순서)
  - 자식 wiki 합성 결과를 입력으로 포함
  - "이 모듈이 자식 모듈들을 어떻게 조합하는가" 관점

Step 3: architecture.md 합성
  - 모든 모듈 wiki를 합성하여 프로젝트 전체 개요
  - 모듈 간 의존 관계 다이어그램

Step 4: 검증 (자동)
  - 합성된 내용이 결정론적 wiki와 모순되지 않는지 확인
  - 참조된 파일:라인이 실제 존재하는지 확인
  - 실패 시 해당 섹션 제거하고 재합성
```

#### 9.4 지식 복리 (Compounding Updates)

Karpathy의 핵심 아이디어를 staleness + wikilink 그래프 인프라 위에 구현.

**이미 구현된 인프라**:
- `wiki_links` 테이블 + `_sync_wikilinks()` — 페이지 간 `[[링크]]` 그래프 ✅
- `_expand_graph()` — BFS 양방향 탐색으로 연결 페이지 식별 ✅
- `wiki_dependencies` + file_hash 비교 — stale 감지 ✅

**Phase 9에서 추가할 것**: stale 감지 → **LLM 자동 재합성** 파이프라인

```
코드 변경 (git commit)
  │
  ▼
Delta reindex (✅ 구현됨)
  │
  ▼
Staleness 감지 (✅ 구현됨: file_hash 비교)
  │
  ▼
[ Phase 9 — 새로운 로직 ]
  │
  ├─ 직접 변경: stale 페이지의 합성 섹션 재생성
  │    └─ LLM이 변경된 코드를 읽고 wiki 내용 갱신
  │
  └─ 간접 영향: _expand_graph()로 연결 페이지 식별 (✅ 구현됨)
     └─ 연결된 페이지의 "연관 모듈" 섹션만 선택적 재합성
        (전체 재합성 아님 — 비용 제어)
```

**현재 gap**: stale 감지 → 자동 갱신 사이의 "코드 변경의 의미를 이해"하는 단계가 없다. file_hash가 바뀐 건 알지만, "어떤 함수가 추가됐고 wiki의 어떤 섹션을 어떻게 고쳐야 하는지"는 결정론적으로 판단 불가. **이것이 LLM 합성이 필수인 이유.**

**비용 모델**:
| 시나리오 | 재합성 범위 | 예상 토큰 |
|----------|-----------|----------|
| 함수 1개 수정 | 해당 모듈 1페이지 | ~2K input + ~1K output |
| 새 모듈 추가 | 해당 모듈 + 연관 모듈 "관계" 섹션 | ~5K input + ~2K output |
| 대규모 리팩토링 | 영향받는 모듈 전체 | ~20K input + ~10K output |
| 최초 전체 합성 (20모듈) | 전체 | ~50K input + ~20K output |

Claude API 기준 예상 비용: 최초 합성 ~$0.30, 일상 업데이트 ~$0.01-0.05/회

#### 9.5 합성 프롬프트 설계

```
System: 너는 코드베이스 문서 작성 전문가다.
결정론적 분석 결과(구조 데이터)와 실제 소스 코드를 기반으로
개발자가 이 모듈을 이해하는 데 필요한 문서를 작성하라.

규칙:
1. 구조 데이터에 있는 사실과 모순되면 안 된다
2. 코드에 없는 기능을 추측하면 안 된다
3. 모든 주장에 파일:라인 출처를 달아라
4. "개요"는 이 모듈이 왜 존재하는지 한 문단으로
5. "설계 결정"은 비자명한 선택만 (자명한 것은 생략)
6. "주의사항"은 버그 가능성, 엣지 케이스, 암묵적 의존성

Input:
- 모듈 이름: {module_name}
- 결정론적 wiki: {deterministic_wiki}
- 소스 코드: {source_chunks}
- 연관 모듈 요약: {related_module_summaries}
- git log (최근 10커밋): {git_context}
```

#### 9.6 환각 방지 메커니즘

| 계층 | 방법 | 구현 |
|------|------|------|
| **입력 제약** | 실제 코드만 컨텍스트로 제공 | hybrid_search + Read |
| **출력 검증** | 참조된 파일:라인 존재 확인 | post-processing 스크립트 |
| **사실 대조** | 결정론적 wiki와 합성 결과 비교 | diff-based 검증 |
| **소스 표시** | 모든 주장에 출처 필수 | 프롬프트 규칙 |
| **단계적 폴백** | 검증 실패 → 해당 섹션 제거, 결정론적 wiki만 표시 | graceful degradation |

#### 9.7 LLM 백엔드 선택

| 옵션 | 장점 | 단점 |
|------|------|------|
| **Claude API** | 최고 품질, 긴 컨텍스트 | 비용 |
| **Local LLM (Ollama)** | 무료, 오프라인 | 품질 ↓, 긴 코드 이해력 ↓ |
| **Hybrid** | 최초 합성은 Claude, 부분 업데이트는 Local | 복잡도 |

**권장**: Claude API 단일 백엔드로 시작. 비용이 문제가 되면 Hybrid 전환.
`config.toml`에 `[wiki.synthesis]` 섹션 추가:

```toml
[wiki.synthesis]
enabled = true
backend = "claude"                    # "claude" | "ollama" | "none"
model = "claude-sonnet-4-6"           # 합성 품질 vs 비용 균형
api_key_env = "ANTHROPIC_API_KEY"     # 환경변수명
max_input_tokens = 30000              # 모듈당 입력 제한
temperature = 0.2                     # 낮은 temperature = 사실 기반
```

#### 9.8 MCP 도구 확장

기존 3개 도구에 합성 관련 파라미터 추가:

```
hybrid_search — 변경 없음
trace_callers — 변경 없음
trace_callees — 변경 없음
```

새 CLI 명령:

| 명령 | 설명 |
|------|------|
| `synthesize-wiki` | 전체 또는 stale 모듈의 LLM 합성 실행 |
| `synthesize-wiki --module <name>` | 특정 모듈만 합성 |
| `synthesize-wiki --dry-run` | 합성 대상과 예상 비용만 출력 |
| `verify-synthesis` | 합성 결과의 출처 검증 (파일:라인 존재 확인) |

새 스킬:

| 스킬 | 설명 |
|------|------|
| `/bootstrap-wiki` 확장 | `--synthesize` 플래그로 LLM 합성까지 일괄 실행 |
| `/sync-wiki` 확장 | stale 감지 시 합성 섹션 자동 재생성 옵션 |

#### 9.9 DB 스키마 확장

```sql
-- wiki_pages에 합성 메타데이터 컬럼 추가
ALTER TABLE wiki_pages ADD COLUMN synthesis_model TEXT;      -- "claude-sonnet-4-6"
ALTER TABLE wiki_pages ADD COLUMN synthesis_version INTEGER DEFAULT 0;
ALTER TABLE wiki_pages ADD COLUMN synthesis_hash TEXT;        -- 합성 입력의 해시 (변경 감지)
ALTER TABLE wiki_pages ADD COLUMN last_synthesized_at TEXT;
```

`synthesis_hash`는 (결정론적 wiki + 소스 코드 해시)의 조합. 해시가 같으면 재합성 스킵 → 불필요한 API 호출 방지.

#### 9.10 디스크 구조

```
.hybrid-search/wiki/
  ├── index.md                    # 모듈 목록 (기존)
  ├── architecture.md             # 합성: 프로젝트 전체 개요
  ├── auth-system.md              # 합성: 개요 + 설계 결정 + 구조 데이터
  ├── tuition-billing.md          # 합성
  ├── ...
  └── _raw/                       # 결정론적 원본 (합성 전 백업)
      ├── auth-system.raw.md
      └── tuition-billing.raw.md
```

`_raw/`는 합성 실패 시 폴백용. 항상 결정론적 wiki를 보존.

---

### Phase 10: LLM 재랭킹 (검색 품질 향상)

> Phase 9 완료 후 진행. 현재 RRF 퓨전 결과를 LLM이 재랭킹.

#### 10.1 현재 한계

RRF는 순위 기반 합산이라 "쿼리의 의도"를 이해하지 못함:
- "로그인 에러 처리" → `signIn`(rank 1)보다 `handleAuthError`(rank 5)가 더 적절할 수 있음
- RRF는 두 엔진의 순위만 합산, 쿼리-결과 적합도를 직접 판단하지 않음

#### 10.2 재랭킹 파이프라인

```
쿼리
  │
  ▼
기존 RRF (top-20 후보)
  │
  ▼
LLM Re-ranker
  │  Input: 쿼리 + 20개 후보의 (name, file_path, snippet)
  │  Output: 재정렬된 순위 + 적합도 점수
  │
  ▼
최종 결과 (top-10)
```

**비용 제어**: re-ranking은 snippet만 전송 (전체 코드 X). 20개 후보 × ~100토큰 = ~2K 토큰 입력.

#### 10.3 설정

```toml
[search.reranking]
enabled = false                      # Phase 10 완료 전까지 기본 off
model = "claude-haiku-4-5"           # 빠르고 저렴한 모델
max_candidates = 20                  # RRF에서 가져올 후보 수
```

---

### Phase 11: 검색 기반 자동 답변 (RAG)

> Phase 9+10 완료 후. wiki + 검색 결과를 조합하여 자연어 답변 생성.

```
사용자 질문 ("로그인이 어떻게 동작해?")
  │
  ▼
1. Wiki 조회: auth-system.md (합성 wiki)
  │
  ▼
2. 부족하면: hybrid_search("로그인")
  │
  ▼
3. LLM 답변 생성 (wiki + 검색 결과 + 코드)
  │
  ▼
4. 답변에 출처 첨부 (파일:라인)
```

이 단계에서 hybrid-search-mcp는 **코드베이스 Q&A 시스템**이 된다.

---

## Part III: 구현 로드맵

### 우선순위

| Phase | 이름 | 의존성 | 핵심 가치 |
|:-----:|------|--------|----------|
| **9a** | 단일 모듈 합성 ✅ | 없음 | prepare/finalize 아키텍처, AST Chunker E2E 검증 |
| **9b** | Bottom-up 전체 합성 ✅ | 9a | 28/28 모듈 합성, 슬러그 매칭 버그 수정 |
| **9c** | 지식 복리 (incremental) | 9b | 코드 변경 → 연쇄 wiki 업데이트 |
| **9d** | 환각 검증 자동화 | 9b | 출처 검증 + 사실 대조 |
| **10** | LLM 재랭킹 | 없음 (9와 독립) | 검색 정확도 향상 |
| **11** | RAG 답변 생성 | 9b + 10 | 코드베이스 Q&A |

### 9a 세부 태스크 (첫 번째 구현)

```
1. config.toml에 [wiki.synthesis] 섹션 추가
2. Claude API 클라이언트 래퍼 (src/hybrid_search/index/synthesizer.py)
   - 단일 모듈 합성 함수
   - 프롬프트 템플릿
   - 출처 검증 로직
3. CLI: `synthesize-wiki --module <name> --cwd <path>`
4. 테스트: hybrid-search-mcp 자체를 대상으로 1개 모듈 합성
5. 합성 결과를 기존 wiki 페이지에 병합 (구조 데이터는 <details>로)
6. DB: synthesis_* 컬럼 마이그레이션
```

### 검증 기준

| 기준 | 통과 조건 |
|------|----------|
| 출처 정확도 | 참조된 파일:라인의 95%+ 실존 |
| 사실 일관성 | 결정론적 wiki와 모순 0건 |
| 유용성 | "이 모듈이 뭐하는지" 질문에 wiki만으로 답변 가능 |
| 비용 | 20모듈 프로젝트 최초 합성 < $0.50 |
| 시간 | 단일 모듈 합성 < 30초 |

---

## Open Questions

### Resolved (Phase 1-8)
- 임베딩 모델 선택 → OpenAI text-embedding-3-small
- Call graph resolution rate → module-linked 45-66%
- Multi-store 일관성 → 시나리오별 복구 전략
- (이전 18개 항목 — v5 design.md 참조)

### Open (Phase 9+)
1. **합성 LLM 모델 선택**: Sonnet(빠름/저렴) vs Opus(품질)? → 9a에서 A/B 비교
2. **합성 언어**: wiki를 한국어로? 영어로? 혼합? → 사용자 설정으로 (default: 프로젝트 주 언어)
3. **합성 깊이**: 모든 모듈을 합성할 것인가, 핵심 모듈만? → LRU 기반: 자주 조회되는 모듈 우선
4. **MindVault 통합**: Hybrid Search wiki가 MindVault의 graph/wiki를 완전 대체 가능한가?
5. **Karpathy식 슬라이드/차트 출력**: wiki를 Marp 슬라이드나 Mermaid 다이어그램으로도 렌더링?
6. **Obsidian 연동**: `.hybrid-search/wiki/`를 Obsidian vault로 열 수 있게 wikilink 호환?
7. **재합성 트리거 정책**: 모든 stale을 즉시 재합성? lazy (조회 시)? cron?
