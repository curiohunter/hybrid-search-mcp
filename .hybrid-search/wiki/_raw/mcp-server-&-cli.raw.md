# MCP Server & CLI
> 마지막 업데이트: 2026-04-14 | 상태: fresh

## 개요

hybrid-search-mcp는 두 가지 인터페이스를 제공한다.

1. **MCP Server** (`server.py`) -- Claude Code가 stdio로 연결하여 3개 도구를 호출
2. **CLI** (`cli.py`) -- git hook, 관리 작업용 12개 명령 (MCP 오버헤드 없음)

서버는 도구 호출마다 config.toml의 mtime을 확인하고, 변경 시 hot-reload한다.

---

## MCP 도구 (3개)

### hybrid_search
BM25 + 시맨틱 벡터 검색을 RRF 퓨전으로 결합. 한영 크로스랭귀지 지원.

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `query` | string | Y | 검색 쿼리 (한국어/영어) |
| `project` | string | N | 프로젝트 이름. 생략 시 전체 검색 |
| `limit` | int | N | 결과 수 (1-50, 기본 10) |
| `file_pattern` | string | N | 파일 glob 필터 (예: `*.ts`) |
| `node_types` | string[] | N | 노드 타입 필터: function, class, method 등 |
| `bm25_weight` | float | N | 0=순수 시맨틱, 1=순수 키워드 (기본 0.5, 자동 분류) |
| `cwd` | string | N | 현재 디렉토리. 해당 프로젝트 결과 부스트 |

### trace_callers
역방향 콜 그래프 -- 주어진 함수를 호출하는 모든 함수를 탐색.

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `symbol` | string | N* | 함수/메서드 이름 또는 qualified name |
| `chunk_id` | string | N* | 이전 검색 결과의 chunk ID (더 정확) |
| `project` | string | N | 프로젝트 이름 |
| `depth` | int | N | 탐색 깊이 (1-10, 기본 2) |
| `min_confidence` | enum | N | low/medium/high (기본 medium) |

*`symbol` 또는 `chunk_id` 중 하나 필수. chunk_id가 우선.

### trace_callees
순방향 콜 그래프 -- 주어진 함수가 호출하는 모든 함수를 탐색. 파라미터는 trace_callers와 동일.

---

## CLI 명령 (12개)

```
python -m hybrid_search.cli <command>
```

| 명령 | 설명 |
|---|---|
| `reindex` | 프로젝트 델타 리인덱스 (--force로 전체, --wiki로 위키 자동 생성) |
| `status` | 등록된 전체 프로젝트의 인덱스 상태 출력 |
| `stale` | 위키 페이지 staleness 확인 (변경된 소스 파일 표시) |
| `install-hook` | .git/hooks/post-commit에 자동 리인덱스 훅 설치 |
| `sync-wiki` | 디스크의 위키 .md 파일을 DB에 동기화 (staleness 추적용) |
| `call-graph-stats` | 콜 그래프 해상도 통계 (High/Medium/Unresolved 비율) |
| `generate-wiki-plan` | 콜 그래프 기반 모듈 트리 생성 (--dry-run 지원) |
| `generate-wiki` | 모듈 트리 기반 위키 페이지 자동 생성 + DB 동기화 |
| `verify-wiki` | 위키 커버리지 검증 (모듈 매칭, 파일 커버리지, staleness) |
| `search-symbols` | 심볼 이름으로 퍼지 검색 (--type으로 필터) |
| `remove-project` | 프로젝트 등록 해제 + 인덱스 삭제 (--keep-index로 보존) |
| `lookup-wiki` | 쿼리 또는 태그로 위키 페이지 조회 |

---

## 서버 구조

### create_server (`server.py`)
`mcp.server.Server` 인스턴스를 생성하고 3개 도구를 등록한다.

공유 리소스 초기화 순서:
1. `ProjectRegistry` -- 프로젝트 목록 관리
2. `Embedder` -- 임베딩 모델 로드
3. `IndexingPipeline` -- 인덱싱 파이프라인
4. `SearchOrchestrator` -- BM25 + 벡터 검색 오케스트레이터

### _dispatch_tool
`match name:` 패턴 매칭으로 3개 도구 핸들러에 라우팅. 각 핸들러는 `tools/` 디렉토리의 별도 모듈에 위치.

### _HotReloadableConfig
config.toml의 mtime을 매 도구 호출 시 확인. 변경 감지 시:
- config 재로드
- embedding 설정이 바뀌었으면 Embedder 재초기화
- Pipeline과 Orchestrator 재생성

### stdio 전송
`_run_server` -> `stdio_server()` -> `server.run()`. MCP 표준 stdin/stdout 프로토콜.

---

## tools/ 디렉토리 구조

| 파일 | 역할 |
|---|---|
| `hybrid_search.py` | hybrid_search 도구 핸들러. Orchestrator에 위임 후 결과 직렬화 |
| `trace.py` | trace_callers/trace_callees 핸들러. 사이클 방지 visited set + 100노드 cap |
| `index.py` | index_project, index_status 핸들러 (MCP 확장용) |
| `semantic_search.py` | 순수 벡터 검색 핸들러 (MCP 확장용) |
| `symbols.py` | search_symbols 핸들러 -- 퍼지 심볼 이름 검색 |
| `projects.py` | list_projects, remove_project 핸들러 |
| `wiki.py` | compile_to_wiki, lookup_wiki, check_wiki_staleness, refresh_wiki_page 핸들러 |

현재 `server.py`가 직접 노출하는 도구는 3개(hybrid_search, trace_callers, trace_callees)이며,
나머지 핸들러들은 MCP 확장 도구 또는 내부 전용으로 사용된다.
