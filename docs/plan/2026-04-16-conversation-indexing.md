# Conversation Indexing — 대화 지식을 코드와 연결하는 설계

> 2026-04-16 작성. Codex 아키텍처 리뷰 반영.

## 배경

### 문제

- Claude Code 대화에는 **왜 그렇게 결정했는지**(의사결정 맥락)가 담겨 있지만, 세션이 끝나면 사라짐
- 코드/문서 인덱싱(hybrid-search)은 **어디에 무엇이 있는지**는 잘 찾지만, **왜 그렇게 했는지**는 모름
- claude-memory-compiler 같은 추상 추출 방식은 교훈은 남기지만 정확한 코드 위치를 잃음

### 핵심 통찰

> Tool calls ARE the natural bridge between conversation and code.

대화에서 `Edit monthly-plan.ts:302`가 발생하면, 그 tool call이 **왜(대화)** ↔ **어디(코드)**를 자연스럽게 연결한다.

### 원재료

```
~/.claude/projects/{project-hash}/{session-id}.jsonl
```

- 세션별 전체 대화 원문 (user, assistant, tool_use, tool_result)
- Compaction/clear는 API 컨텍스트만 줄임, JSONL은 삭제 안 됨
- 현재 프로젝트 기준 세션 6개, ~830KB — 규모 부담 없음

---

## 아키텍처 결정

### Codex 리뷰 핵심 피드백

1. **같은 인덱스에 섞으면 안 된다** — BM25 오염, 벡터 허브화, RRF 과대표집
2. **저장은 통합, 검색/랭킹은 분리** — unified store + separate retrieval
3. **메타데이터 배열이 아니라 explicit edge graph** — conversation ↔ code chunk 링크
4. **전체 대화가 아니라 decision moments** — 고가치 턴만 인덱싱

### 채택 아키텍처

```
unified store + separate retrieval + explicit graph edges
```

```
┌─────────────────────────────────────────────────────┐
│                   SQLite (통합 저장)                  │
│  chunks (chunk_type: code | doc | wiki | conv)      │
│  conversation_sessions                               │
│  conversation_edges (conv_chunk ↔ code_chunk)        │
├─────────────────────────────────────────────────────┤
│              Retrieval (분리 검색)                    │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │ Code/Doc/Wiki│  │ Conversation │                 │
│  │  BM25 + Vec  │  │  BM25 + Vec  │                 │
│  │  top-30      │  │  top-10      │                 │
│  └──────┬───────┘  └──────┬───────┘                 │
│         │                 │                          │
│         ▼                 ▼                          │
│      Typed Late Fusion (query intent 기반)           │
│      + Edge Expansion (code hit → linked conv)       │
└─────────────────────────────────────────────────────┘
```

---

## 구현 단계

### Phase 1: chunk_type 분리 (기존 코드 개선)

**목표**: 기존 검색 품질 보호 + 향후 대화 인덱싱 기반 마련

| 작업 | 파일 | 변경 |
|------|------|:----:|
| `chunks.chunk_type` 컬럼 추가 | `storage/db.py` | ~10줄 |
| 기존 chunk 생성 시 type 태깅 (code/doc/wiki) | `index/pipeline.py` | ~5줄 |
| orchestrator에서 type-aware top-K 분리 | `search/orchestrator.py` | ~30줄 |
| type별 quota 설정 | `search/orchestrator.py` | ~15줄 |

**의존성**: 없음 (기존 코드만 수정)
**난이도**: 낮음

---

### Phase 2: 대화 JSONL 파서 + Decision Moment 추출

**목표**: Claude Code 세션에서 고가치 턴을 식별하고 chunk로 변환

#### 2a. JSONL 파서

| 작업 | 파일 | 변경 |
|------|------|:----:|
| JSONL 파서 (user/assistant/tool_use/tool_result) | `index/conv_parser.py` | 신규 ~100줄 |
| tool_use에서 file_path, symbol, grep pattern 추출 | 위 동일 | 포함 |
| 세션 메타데이터 (session_id, project, timestamp) | 위 동일 | 포함 |

#### 2b. Decision Moment 필터링

고가치 턴 휴리스틱:

```
포함:
- Edit/Write/MultiEdit가 발생한 turn
- Read/Grep 후 특정 파일로 수렴한 turn
- 에러 → 수정으로 이어진 turn
- 동일 파일/심볼 2회+ 참조 구간
- 대안 비교/tradeoff 언급 turn
- commit/PR/issue 번호 포함 turn

제외:
- 인사/메타 대화
- 단순 탐색성 Read
- verbose Bash stdout 전문
- 같은 grep 패턴 반복
- tool 결과 없는 잡담
```

**의존성**: 없음
**난이도**: 낮음~중간

---

### Phase 3: Event-Centered Conversation Chunking

**목표**: 대화를 "문맥 + 행동 + 결과" 단위로 청킹

#### Chunk 단위: Decision Episode

```
decision episode = user turn + assistant reasoning + tool_use cluster + outcome
```

#### 경계 신호

- tool 호출 발생
- 파일/심볼 focus 변경
- 에러 발생 → 해결
- 명시적 결정 표현: "let's", "instead", "rename", "split", "move"
- 10~20턴 무툴 대화 → topic split

| 작업 | 파일 | 변경 |
|------|------|:----:|
| event-centered chunker | `index/conv_chunker.py` | 신규 ~150줄 |
| chunk에 linked metadata 부착 | 위 동일 | 포함 |
| 임베딩 입력 포맷 (대화 텍스트 + 메타) | 위 동일 | 포함 |

**의존성**: Phase 2
**난이도**: 중간

---

### Phase 4: Edge Graph (Conversation ↔ Code)

**목표**: 대화 chunk와 코드 chunk를 명시적 edge로 연결

#### 스키마

```sql
CREATE TABLE conversation_sessions (
    session_id   TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    turn_count   INTEGER,
    decision_count INTEGER
);

CREATE TABLE conversation_edges (
    id            INTEGER PRIMARY KEY,
    conv_chunk_id TEXT NOT NULL,
    code_chunk_id TEXT,
    file_path     TEXT,
    symbol        TEXT,
    edge_type     TEXT NOT NULL,  -- 'edit', 'read', 'grep', 'reference'
    confidence    TEXT NOT NULL,  -- 'high', 'medium', 'low'
    evidence      TEXT,           -- tool_use ID or description
    session_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
```

#### Edge 해석 규칙

| Tool Call | Edge Type | Confidence | Resolve 방식 |
|-----------|-----------|------------|-------------|
| Edit file:L302-340 | edit | high | line range → code chunk |
| Read file | read | medium | file → all chunks |
| Grep "symbol" | grep | low~medium | matched chunks에 weak edge |
| Write new_file | write | high | file → new chunks |

| 작업 | 파일 | 변경 |
|------|------|:----:|
| conversation_sessions 테이블 | `storage/db.py` | ~15줄 |
| conversation_edges 테이블 | `storage/db.py` | ~20줄 |
| edge resolver (tool_use → code chunk 매핑) | `index/conv_edges.py` | 신규 ~120줄 |

**의존성**: Phase 2, 3
**난이도**: 중간

---

### Phase 5: Typed Late Fusion

**목표**: query intent에 따라 코퍼스별 가중치를 다르게 적용

#### Retrieval Policy

| Query Intent | Code Quota | Conv Quota | Expansion |
|-------------|-----------|-----------|-----------|
| EXACT_SYMBOL | top-30 | 배제 | 없음 |
| KOREAN_NL (코드 기능) | top-30 | top-5 | code→conv edge hop |
| WHY/DECISION ("왜", "배경", "의도") | top-10 | top-20 | conv→code backfill |
| ARCHITECTURE | wiki top-20 | top-5 | wiki→conv 보강 |

#### 검색 흐름

```
1. query intent 분류 (기존 query_classifier 확장)
2. intent별 코퍼스 분리 검색
   - code/doc/wiki: BM25 + Vector → RRF
   - conversation: BM25 + Vector → RRF
3. code hit → edge expansion (연결된 conv chunk 가져오기)
4. typed late fusion (intent별 가중치)
5. 최종 결과 반환
```

| 작업 | 파일 | 변경 |
|------|------|:----:|
| query_classifier에 WHY/DECISION intent 추가 | `search/query_classifier.py` | ~20줄 |
| orchestrator에 separate retrieval + fusion | `search/orchestrator.py` | ~60줄 |
| edge expansion 로직 | `search/orchestrator.py` | ~30줄 |

**의존성**: Phase 1, 4
**난이도**: 중간

---

### Phase 6: 운영 안정화

| 작업 | 설명 |
|------|------|
| recency decay | 오래된 conv chunk의 score 감쇠 |
| near-duplicate collapse | 같은 이슈 반복 논의 시 중복 제거 |
| stale marking | 현재 코드와 충돌하는 옛 결정 표시 |
| conversation GC | N일 이상 된 저가치 세션 정리 |
| max tool output truncation | Bash stdout, large diff 잘라내기 |
| 민감 정보 필터 | env vars, credentials, 내부 URL 제거 |

**의존성**: Phase 1~5 전체
**난이도**: 중간~높음

---

## 기술 스택 요약

| 필요한 것 | 현재 상태 |
|-----------|----------|
| BM25 (Tantivy) | ✅ 있음 |
| Vector (USearch) | ✅ 있음 |
| SQLite | ✅ 있음 (테이블 추가만) |
| 임베딩 (OpenAI) | ✅ 있음 |
| JSONL 파서 | Python 표준 라이브러리 |
| AST chunker | ✅ 있음 (conv_chunker는 별도) |
| Query classifier | ✅ 있음 (intent 확장만) |
| RRF fusion | ✅ 있음 (typed fusion 확장만) |
| **새 외부 의존성** | **없음** |

---

## MVP 우선순위

```
Phase 1 (chunk_type)  ←  가장 먼저, 기존 검색도 개선
  ↓
Phase 2 (JSONL 파서)  ←  원재료 파싱
  ↓
Phase 3 (conv chunker) ← 여기까지가 MVP
  ↓
Phase 4 (edge graph)  ←  품질 도약
  ↓
Phase 5 (typed fusion) ← 검색 완성
  ↓
Phase 6 (운영)        ←  scale 대비
```

Phase 1~3까지가 MVP — 대화를 인덱싱하고 검색할 수 있는 최소 단위.
Phase 4~5에서 "왜 ↔ 어디" 연결이 완성됨.
