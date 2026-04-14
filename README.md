# Hybrid Search MCP

코드베이스를 이해하는 AI를 만드는 도구.
BM25 + Vector 하이브리드 검색으로 코드와 문서를 한국어/영어 크로스 언어 검색.

---

## 시작하기

### 1회: 글로벌 설치

```
/setup-hybrid-search
```

MCP 서버 등록 + hook 설치 + Claude Code 재시작.

### 프로젝트당 1회: 위키 생성

```
/bootstrap-wiki
```

인덱싱 + wiki 생성 + CLAUDE.md 업데이트 + post-commit hook 설치.

### 이후: 그냥 쓰면 됨

```
"학원비 세션 연동 알려줘"          → hybrid_search가 찾아줌
"handleSubmit 어디?"              → Grep이 찾아줌
"이 모듈 전체 구조 알려줘"         → Wiki + hybrid_search 병렬
```

커밋할 때마다 자동으로 변경 파일만 재인덱싱 + 영향 wiki 갱신.

---

## 스킬 5개

| 스킬 | 언제 | 빈도 |
|------|------|------|
| `/setup-hybrid-search` | 첫 설치 | 글로벌 1회 |
| `/bootstrap-wiki` | 프로젝트 온보딩 | 프로젝트당 1회 |
| `/search` | 코드/문서 검색 | 매번 |
| `/save-wiki` | 분석 결과 wiki 저장 | 선택적 |
| `/maintain` | 인덱스/wiki 정리 | 가끔 |

---

## 검색 전략 — 의도 기반 라우팅

고정 순서가 아니라 질문 유형에 따라 최적 도구를 자동 선택:

| 질문 유형 | 1차 | fallback | 예시 |
|-----------|-----|----------|------|
| 구조/관계 | Wiki | hybrid_search | "누가 이 함수 호출해?" |
| 기능 탐색 | hybrid_search | Wiki | "숙제분석 기능 설명해줘" |
| 정밀 조회 | Grep | Read | "handleSubmit 어디?" |
| 설계/맥락 | hybrid_search | Wiki | "왜 이렇게 설계했어?" |
| 스키마/DB | hybrid_search | Grep | "problems 테이블 히스토리" |

### 실사용 벤치마크 (valuein-homepage, 1776파일)

| 지표 | hybrid+Wiki | Grep+Read |
|------|-------------|-----------|
| 도구 호출 | 2~3회 | 10~15회 |
| 소요 시간 | ~3초 | 20~30초 |
| 정확도 | 90%+ | 노이즈 많음 |
| 토큰 소비 | 적음 | 많음 |

---

## 자동화

| 트리거 | 동작 | 사용자 개입 |
|--------|------|------------|
| 커밋 | git delta reindex + 영향 wiki 갱신 | 없음 |
| Edit/Write 전 | STALE.md 경고 | wiki 갱신 |
| Edit/Write 후 | 미문서화 모듈 알림 | wiki 추가 |

커밋 후 인덱싱은 `git diff`로 변경 파일만 수집 → 해당 파일만 재임베딩.
전체 스캔 없이 파일 1개 수정 시 ~2초.

---

## hybrid_search 파라미터

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `query` | (필수) | 검색어 (한국어/영어) |
| `project` | cwd 자동감지 | 프로젝트 이름 |
| `limit` | 10 | 결과 수 (1-50) |
| `bm25_weight` | 자동분류 | 0=벡터, 1=키워드 |
| `file_pattern` | 전체 | 글로브 필터 (`*.ts`, `migrations/*.sql`) |
| `cwd` | 자동 | 해당 프로젝트만 검색 (자동 스코핑) |

쿼리 자동 분류:
- `handleLogin` → BM25 우선 (정확한 심볼)
- `로그인 처리` → 벡터 우선 (한국어 자연어)
- `auth middleware` → 하이브리드 (영어 자연어)

---

## 기술 스택

| 컴포넌트 | 스택 |
|----------|------|
| 임베딩 | OpenAI `text-embedding-3-small` |
| BM25 | tantivy-py (Rust) |
| Vector DB | USearch HNSW (C++) |
| AST 파싱 | tree-sitter (C), 14개 언어 |
| 스토리지 | SQLite WAL |

지원 언어: TypeScript, JavaScript, Python, Rust, Go, Ruby, Java, C, C++, Swift, Kotlin, CSS, HTML, SQL

---

## CLI 레퍼런스

보통은 스킬로 충분. 디버깅용:

```bash
source .venv/bin/activate
python -m hybrid_search.cli <command>
```

| 명령어 | 설명 |
|--------|------|
| `reindex --git-delta --cwd .` | 변경 파일만 재인덱싱 |
| `reindex --force --cwd .` | 전체 재인덱싱 |
| `reindex --wiki-scope affected --cwd .` | 영향 wiki만 재생성 |
| `status` | 등록된 프로젝트 목록 |
| `stale --cwd .` | stale wiki 확인 |
| `remove-project <name>` | 프로젝트 제거 |

---

## 트러블슈팅

| 문제 | 해결 |
|------|------|
| tiktoken 에러 | `.venv/bin/pip install tiktoken` |
| 검색 결과가 다른 프로젝트 | cwd 파라미터 확인, 또는 project 명시 |
| 검색 결과가 적음 | `reindex --force --cwd .` |
| rate limit | 자동 retry + 배치 간 0.2초 간격 |
| hook 안 먹힘 | `python -m hybrid_search.cli setup` 재실행 |

---

## 데이터 위치

```
~/.hybrid-search/                        # 글로벌
├── config.toml
└── projects/{hash}/store.db

<project>/.hybrid-search/                # 프로젝트별
├── wiki/
│   ├── index.md
│   ├── STALE.md
│   └── {module}.md
└── wiki-gaps.txt
```

---

## 성능

| 작업 | 시간 | 비용 |
|------|------|------|
| 첫 인덱싱 (1,776 파일) | ~165초 | ~$0.04 |
| 커밋 후 git delta | ~2초 | 미미 |
| 검색 | <2초 | 무료 |

---

## 라이선스

MIT
