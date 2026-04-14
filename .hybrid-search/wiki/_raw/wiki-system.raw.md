# Wiki System
> 마지막 업데이트: 2026-04-14 | 상태: fresh

## 개요

Wiki 시스템은 코드베이스 분석 결과를 구조화된 페이지로 저장하고, 소스 파일 변경 시 자동으로 staleness를 감지하는 반응형(reactive) 문서 계층이다. 두 가지 저장소(디스크 `.md` + SQLite DB)를 동기화하며, MCP 도구와 CLI 양쪽에서 접근 가능하다.

핵심 파일:
- `src/hybrid_search/storage/wiki.py` — WikiStore 클래스 (CRUD + staleness)
- `src/hybrid_search/tools/wiki.py` — MCP 도구 핸들러 4개
- `src/hybrid_search/cli.py` — CLI 명령 6개

## 이중 저장 (디스크 .md + DB wiki_pages)

| 저장소 | 경로 | 용도 |
|--------|------|------|
| 디스크 | `.hybrid-search/wiki/*.md` | 사람이 읽는 마크다운, CLAUDE.md에서 참조 |
| DB | `wiki_pages` 테이블 | 검색/조회/staleness 추적, LRU 관리 |

`sync-wiki` 명령이 디스크 -> DB 단방향 동기화를 수행한다. `generate-wiki`는 양쪽 모두에 쓴다. `reindex` 후 wiki 디렉토리가 존재하면 자동으로 `sync-wiki`가 실행된다.

DB 스키마 (`storage/db.py`):
- **wiki_pages**: id, project_id, query_key, title, content, tags(JSON), created_at, updated_at, accessed_at, access_count, version
- **wiki_dependencies**: wiki_page_id, file_id, file_hash_at_compile, chunk_ids(JSON) — 복합 PK (wiki_page_id, file_id), 두 FK 모두 ON DELETE CASCADE

## WikiStore 클래스

`WikiStore(conn, max_pages=100)` — StoreDB의 sqlite3 연결을 래핑한다.

### 주요 메서드

| 메서드 | 역할 |
|--------|------|
| `compile_page(project_id, query, title, content, tags, file_dependencies)` | 페이지 upsert + 의존성 스냅샷 저장. query를 정규화하여 deterministic page_id 생성. 초과 시 LRU evict |
| `lookup_page(project_id, query=, tag=)` | query 또는 tag로 조회. 접근 시 access_count 증가, staleness 자동 첨부 |
| `check_staleness(project_id, page_id=)` | 단일 페이지 또는 프로젝트 전체의 staleness 반환 |
| `refresh_page(page_id, content, file_dependencies=)` | 내용 갱신 + 해시 재스냅샷. file_dependencies 미제공 시 기존 deps의 현재 해시로 업데이트 |
| `delete_page(page_id)` | 삭제 (CASCADE로 deps도 제거) |
| `list_pages(project_id, limit, offset)` | updated_at DESC 정렬 목록 |

### 내부 유틸

- `normalize_query(query)` — lowercase, 공백 축소, 단어 정렬, 200자 제한 -> deterministic key
- `_page_id(project_id, query_key)` — SHA-256 해시의 앞 16자
- `_evict_lru(project_id)` — max_pages 초과 시 accessed_at ASC로 삭제

## Staleness 추적 (wiki_dependencies -- 파일 해시 스냅샷)

페이지 compile 시 각 소스 파일의 `file_hash`를 `file_hash_at_compile`로 저장한다. staleness 체크는:

1. `wiki_dependencies`와 `files` 테이블을 LEFT JOIN
2. `file_hash`가 NULL이면 -> 파일 삭제됨 (stale)
3. `file_hash != file_hash_at_compile`이면 -> 파일 변경됨 (stale)
4. 변경된 파일 목록(`changed_files`)과 총 의존성 수(`total_dependencies`) 반환

`reindex` 시 파일이 변경/삭제되면 `_mark_stale_wikis()`가 자동 호출되어 stale 페이지 수를 출력한다.

## MCP 도구 (tools/wiki.py)

4개의 핸들러 함수. 모두 `_open_store()`로 프로젝트 DB를 열고, 완료 후 `db.close()`.

| 도구 | 함수 | 입력 | 출력 |
|------|------|------|------|
| `compile_to_wiki` | `handle_compile_to_wiki` | project, query, title, content, tags?, source_chunk_ids? | page_id, query_key, evicted_count, dependencies_count |
| `lookup_wiki` | `handle_lookup_wiki` | project, query?, tag? | found, page_id, title, content, tags, stale, changed_files, version, access_count |
| `check_wiki_staleness` | `handle_check_wiki_staleness` | project, page_id? | pages: [{page_id, title, stale, changed_files, total_dependencies}] |
| `refresh_wiki_page` | `handle_refresh_wiki_page` | project, page_id, content, source_chunk_ids? | page_id, version, dependencies_updated |

`_resolve_file_deps(db, source_chunk_ids)` — chunk_id 목록에서 file_id + file_hash + chunk_ids 매핑을 구축한다.

## CLI 명령

| 명령 | 함수 | 설명 |
|------|------|------|
| `stale` | `cmd_stale` | 프로젝트 wiki 페이지별 staleness 출력 (OK/STALE + changed files) |
| `sync-wiki` | `cmd_sync_wiki` | 디스크 `.md` 파일을 DB에 동기화. 백틱 경로에서 file deps 추출 |
| `generate-wiki-plan` | `cmd_generate_wiki_plan` | DAG 모듈 트리에서 wiki 계획 생성, `.hybrid-search/wiki-plan.json` 저장 |
| `generate-wiki` | `cmd_generate_wiki` | 모듈 트리 기반 wiki 페이지 생성 (디스크 + DB 동시 기록) |
| `verify-wiki` | `cmd_verify_wiki` | wiki 커버리지를 모듈 트리 대비 검증 |
| `lookup-wiki` | CLI에서 query/tag로 wiki 조회 |

`reindex` 시 자동 동작:
- `--wiki` 플래그: `generate-wiki` 실행
- wiki 디렉토리 존재 시: `sync-wiki` 자동 실행
- 파일 변경/삭제 시: `_mark_stale_wikis()` 호출
- 새 파일 추가 시: `.hybrid-search/wiki-gaps.txt`에 gap 플래그 기록

## bootstrap-wiki 스킬과의 관계

`bootstrap-wiki`는 Claude Code 스킬(대화형)로, 위 시스템을 orchestration한다:

1. **hybrid_search 인덱스 확인** — 인덱스가 없으면 `index_project` 먼저 실행
2. **generate-wiki-plan** 호출 — DAG에서 모듈 트리 추출, 커버리지 계산
3. **generate-wiki** 실행 — 모듈별 wiki 페이지를 디스크 + DB에 생성
4. **CLAUDE.md 갱신** — 생성된 wiki 페이지를 CLAUDE.md에서 참조할 수 있게 경로 추가

스킬은 MCP 도구(`compile_to_wiki`, `lookup_wiki` 등)를 직접 호출하지 않고, CLI 명령을 통해 일괄 처리한다. MCP 도구는 대화 중 개별 페이지 조회/갱신에 사용된다.
