# Hybrid Search MCP

코드베이스를 이해하는 AI를 만드는 도구.
코드 + 문서를 자동으로 인덱싱하고, 지식 그래프로 연결하고, 계속 갱신한다.

---

## 설치

Claude Code에서:

```
/setup-hybrid-search
```

이 한 줄이 자동으로:
1. 빌드 (`pip install`)
2. OpenAI API 키 확인 (없으면 물어봄)
3. MCP 서버 등록 (`~/.claude.json`)
4. 글로벌 hook 등록 (`~/.claude/settings.json`)

설치 후 Claude Code 재시작.

> 수동 설치가 필요하면 [수동 설치 가이드](#수동-설치) 참조.

---

## 사용법

**아무것도 안 해도 된다.**

Claude Code에서 아무 프로젝트를 열고 파일을 읽으면 자동으로 동작한다:

```
파일 Read
  → 백그라운드 인덱싱 (~30초)
  → Wiki 자동 생성 + [[wikilink]] 그래프
  → Post-commit hook 자동 설치

이후 커밋할 때마다:
  → Delta 리인덱싱 (~5초)
  → Wiki 재생성 + wikilink 갱신
  → Stale 페이지 감지

Claude Code가 Edit 시도:
  → "STALE wiki detected" 경고 (무시 불가)
  → Wiki 갱신 후 작업 진행
```

### 검색

Claude Code 대화에서 바로:

```
"로그인 에러 처리 어떻게 돼있어?"
"authentication middleware 찾아줘"
```

`hybrid_search` MCP 도구가 자동으로 BM25 + 벡터 검색 실행.
한국어 질문도 지원 (크로스 언어 검색).

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `query` | (필수) | 검색어 (한국어/영어) |
| `project` | 자동감지 | 프로젝트 이름 |
| `limit` | 10 | 결과 수 (1-50) |
| `bm25_weight` | 자동분류 | 0=벡터, 1=키워드 |
| `file_pattern` | 전체 | 글로브 필터 (`*.ts`, `src/**/*.py`) |

쿼리 자동 분류:
- `handleLogin` → EXACT_SYMBOL (BM25 우선)
- `로그인 처리` → KOREAN_NL (벡터 우선)
- `auth middleware` → ENGLISH_NL (하이브리드)

### LLM 합성 Wiki

자동 생성된 결정론적 wiki 위에 Claude Code가 자연어 설명을 추가하고 싶으면:

```
/bootstrap-wiki
```

---

## 자동화 4계층

| 계층 | 트리거 | 동작 | 사용자 개입 |
|------|--------|------|------------|
| **Auto-index** | Read | 인덱싱 + wiki + wikilink + hook 설치 | 없음 |
| **Git hook** | 커밋 | reindex + wiki 재생성 + stale 감지 | 없음 |
| **Stale hook** | Edit/Write 전 | STALE.md 경고 | wiki 갱신 |
| **Gap hook** | Edit/Write 후 | 미문서화 모듈 알림 | wiki 추가 |

---

## 지식 그래프 (자동)

Wiki 페이지 간 `[[wikilink]]`가 3가지 메커니즘으로 자동 생성된다:

| 연결 방식 | 예시 | 수동 작업 |
|-----------|------|-----------|
| **콜 엣지** | 함수 A가 함수 B 호출 → 모듈 연결 | 없음 |
| **파일 참조** | design.md에 `cli.py` 언급 → CLI 모듈 연결 | 없음 |
| **공통 참조** | design.md와 HANDOFF.md가 같은 코드 모듈 참조 → 문서끼리 연결 | 없음 |

이 그래프가 staleness 전파의 기반:

```
cli.py 수정 → CLI 모듈 stale
  → wikilink BFS 1-hop
  → design.md, HANDOFF.md도 간접 stale
  → Claude Code Edit 시 전부 경고
```

---

## CLI 레퍼런스

보통은 CLI를 직접 쓸 일이 없다. 디버깅용.

```bash
source /path/to/hybrid-search-mcp/.venv/bin/activate
python -m hybrid_search.cli <command>
```

| 명령어 | 설명 |
|--------|------|
| `setup` | MCP 서버 + 글로벌 hook 자동 등록 |
| `reindex --cwd . --synthesize` | 인덱싱 + wiki 생성 + 합성 준비 |
| `reindex --cwd . --force` | 전체 재인덱싱 |
| `status` | 등록된 프로젝트 목록 |
| `stale --cwd .` | stale wiki 확인 |
| `install-hook --cwd .` | post-commit hook 설치 |
| `generate-wiki --cwd .` | wiki 재생성 |
| `synthesize-wiki --finalize --cwd .` | LLM 합성 확정 |
| `verify-synthesis --fix --cwd .` | 환각 검증 + 정리 |

---

## 기술 스택

| 컴포넌트 | 스택 |
|----------|------|
| 임베딩 | OpenAI `text-embedding-3-small` (로컬 리소스 제로) |
| BM25 | tantivy-py (Rust) |
| Vector DB | USearch HNSW (C++) |
| AST 파싱 | tree-sitter (C), 14개 언어 |
| 스토리지 | SQLite WAL |

지원 언어: TypeScript, JavaScript, Python, Rust, Go, Ruby, Java, C, C++, Swift, Kotlin, CSS, HTML, SQL

---

## 데이터 위치

```
~/.hybrid-search/                        # 글로벌 (자동 생성)
├── config.toml
└── projects/{hash}/store.db

<project>/.hybrid-search/                # 프로젝트별 (자동 생성)
├── wiki/
│   ├── index.md                         # wiki 목록
│   ├── STALE.md                         # stale 경고 (자동)
│   └── {module}.md                      # 모듈별 wiki
└── wiki-gaps.txt                        # 미문서화 파일
```

---

## 성능

| 작업 | 시간 | 비용 |
|------|------|------|
| 첫 인덱싱 (1,000 파일) | ~30초 | ~$0.04 |
| 커밋 후 Delta | ~5초 | 미미 |
| 검색 | <100ms | 무료 |

---

## 트러블슈팅

| 문제 | 해결 |
|------|------|
| 인덱싱이 안 됨 | `~/.env.local`에 `OPENAI_API_KEY` 확인 |
| 검색 결과가 적음 | `reindex --force --cwd .` |
| Wiki가 안 갱신됨 | `reindex --synthesize --cwd .` |
| hook이 안 먹힘 | `python -m hybrid_search.cli setup` 재실행 |

---

## 수동 설치

Claude Code에서 `/setup-hybrid-search`를 쓸 수 없는 경우:

```bash
# 1. 빌드
git clone <repo-url> && cd hybrid-search-mcp
python3.11 -m venv .venv && source .venv/bin/activate && pip install -e .

# 2. API 키
echo "OPENAI_API_KEY=sk-proj-..." > ~/.env.local

# 3. 자동 설정 (MCP 등록 + hook 등록)
python -m hybrid_search.cli setup
```

---

## 라이선스

MIT
