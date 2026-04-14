---
name: search
description: "코드베이스를 검색합니다. 질문 의도에 따라 Wiki(구조)와 hybrid_search(디테일)를 병렬 실행하고, 부족하면 Grep/Read로 보충합니다."
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Agent, mcp__hybrid-search__hybrid_search
---

# Search — 의도 기반 병렬 검색

코드베이스를 검색하는 통합 스킬. 질문 유형을 판단하고 최적 경로로 검색한다.

## CLI 단독 사용

Claude Code 없이도 터미널에서 바로 검색 가능:

```bash
hybrid-search-mcp search "authentication flow"
hybrid-search-mcp search "인증 로직" --json
hybrid-search-mcp search "handleSubmit" --node-types function
hybrid-search-mcp search "schema migration" --file-pattern "*.sql"
```

## Step 1: 의도 분류

사용자 질문을 아래 5가지 중 하나로 판단한다:

| 유형 | 신호 | 예시 |
|------|------|------|
| **구조/관계** | "누가 호출", 의존, 모듈 구조, 전체 그림 | "A가 B를 호출하나?" |
| **기능 탐색** | 자연어, 한국어, 넓은 기능 질문 | "문제 업로드 기능 설명해줘" |
| **정밀 조회** | 정확한 심볼명, 파일명, 에러 문자열 | "handleSubmit 어디?" |
| **설계/맥락** | "왜 이렇게", QA 히스토리, 계획 문서 | "스키마 왜 이래?" |
| **스키마/DB** | 마이그레이션, DDL, 테이블 구조 변화 | "problems 테이블 히스토리" |

## Step 2: 실행

### 구조/관계 질문

**병렬 실행:**
- Wiki: `.hybrid-search/wiki/index.md` Read → 관련 페이지 Read (콜그래프, 모듈 관계)
- hybrid_search: 동시에 시맨틱 검색 실행

두 결과를 합쳐서 답변. Wiki가 구조를 잡고 hybrid_search가 디테일을 채운다.

### 기능 탐색 질문

**병렬 실행:**
- hybrid_search: 시맨틱 검색 (1차, 코드+문서+계획 문서 크로스 도메인)
- Wiki: `.hybrid-search/wiki/index.md` Read → 관련 모듈 페이지 확인

hybrid_search 결과가 주축, Wiki는 호출 흐름 보충.

### 정밀 조회 질문

**Grep first:**
- Grep으로 정확한 심볼/문자열 검색
- 결과가 부족하거나 맥락이 필요하면 → hybrid_search 또는 Wiki 보충

### 설계/맥락 질문

**hybrid_search first:**
- hybrid_search로 설계 문서, QA 기록, 계획 문서 검색
- 관련 모듈 구조가 필요하면 → Wiki 보충

### 스키마/DB 질문

**hybrid_search first:**
- hybrid_search 호출 시 `file_pattern: "*.sql"` 또는 `node_types` 활용
- 부족하면 → Grep으로 마이그레이션 디렉토리 직접 탐색

## Step 3: 보충 (fallback)

1차에서 답이 부족하면 도구를 **바꾸지 말고 보충**한다:
- hybrid_search 결과 부족 → Wiki에서 콜그래프/모듈 관계 확인
- Wiki 결과 부족 → hybrid_search로 문서/코드 디테일 보충
- Grep 결과에 맥락 부족 → Read로 파일 전문 확인

## 운영 규칙

- Wiki `[[링크]]`가 있으면 따라갈 것
- hybrid_search는 한국어 자연어 질의 가능 (크로스 언어 검색)
- 심볼명 검색은 BM25가 자동으로 가중치 올림 (자동 감지)
- `file_pattern`으로 범위 좁히기 가능 (예: `*.ts`, `migrations/*.sql`)
