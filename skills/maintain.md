---
name: maintain
description: "hybrid-search 인덱스와 wiki를 유지보수합니다. delta reindex + stale wiki 갱신 + wiki gaps 생성 + CLAUDE.md 최신화."
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

# Maintain — 인덱스/위키 유지보수

검색과 분리된 유지보수 스킬. 비싼 작업(wiki 합성, gap 채우기)을 한 번에 처리한다.

## Step 1: 상태 확인

```bash
hybrid-search-mcp status
```

현재 프로젝트의 인덱스 상태, 마지막 인덱싱 시간, chunk 수를 확인한다.

## Step 2: Delta Reindex

```bash
hybrid-search-mcp reindex --cwd {project_root}
```

- 변경된 파일만 재인덱싱 (delta)
- 임베딩 API 호출은 새로 추가/변경된 파일에만 발생
- 결과 요약: added/changed/deleted 수 보고

## Step 3: Stale Wiki 갱신

```bash
hybrid-search-mcp stale --cwd {project_root}
```

stale 페이지가 있으면:

1. `.hybrid-search/wiki/STALE.md` 내용을 Read — 어떤 페이지가 stale인지, 어떤 소스 파일이 변경됐는지 확인
2. 변경된 소스 파일을 Read
3. 해당 wiki 페이지를 Edit으로 현재 코드에 맞게 갱신
4. 모든 stale 페이지 갱신 후 STALE.md 삭제

stale 페이지가 3개 이상이면 Agent를 병렬로 사용하여 동시 갱신한다.

## Step 4: Wiki Gaps 채우기

`.hybrid-search/wiki-gaps.txt`가 있으면:

1. gap 목록 확인 — wiki가 없는 모듈/디렉토리
2. 중요도 판단: 파일 3개 이상인 디렉토리만 대상
3. `/bootstrap-wiki`와 동일한 형식으로 새 wiki 페이지 생성
4. `index.md` 갱신

gap이 3개 이상이면 Agent 병렬로 생성.

## Step 5: DB 동기화

```bash
hybrid-search-mcp sync-wiki --cwd {project_root}
```

디스크 wiki 파일을 DB에 동기화하여 staleness 추적을 활성화한다.

## Step 6: CLAUDE.md 확인

프로젝트의 CLAUDE.md에 `<!-- hybrid-search -->` 마커가 있는지 확인.
- 있으면: 의도 기반 라우팅 표가 최신인지 확인
- 없으면: 새 라우팅 표 삽입

## 결과 보고

```
Maintain 완료
- Reindex: +N added, ~N changed, -N deleted
- Stale wiki: N개 갱신
- Wiki gaps: N개 생성
- DB sync: N pages
- CLAUDE.md: OK
```
