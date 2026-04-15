---
name: rebuild-index
description: "인덱스 손상/불일치 시 복구. 상태 진단 → 불일치면 force rebuild, 아니면 delta reindex → 결과 검증."
allowed-tools: Bash, Read
---

# Rebuild Index — 인덱스 복구

인덱스에 문제가 있을 때 사용하는 **복구 전용** 스킬.
`/maintain`이 일상 유지보수(delta reindex + wiki 갱신)라면, `/rebuild-index`는 인덱스 자체가 깨졌을 때 쓴다.

## 역할 구분

| 스킬 | 용도 | 비용 |
|------|------|------|
| `/maintain` | 일상 유지보수 — delta reindex + stale wiki + gaps 채우기 | 낮음 (변경분만) |
| `/rebuild-index` | 인덱스 복구 — 손상/불일치 진단 + 필요시 full rebuild | 높음 (전체 재임베딩) |

## 언제 사용하는가

- 검색 결과가 이상하거나 인덱스 손상이 의심될 때
- ConsistencyMismatchError가 발생했을 때
- auto-rebuild가 실패해서 최신 변경이 미반영될 때
- embedding 설정이나 chunking 로직이 변경된 후

## Step 1: 상태 진단

현재 프로젝트의 인덱스 상태와 3-store 일치 여부를 확인한다.

```bash
PROJECT_ROOT=$(git rev-parse --show-toplevel)
VENV=/Users/ian/project/claude_project/hybrid-search-mcp/.venv/bin/python
"$VENV" -m hybrid_search.cli status --cwd "$PROJECT_ROOT"
```

추가로 3-store chunk 수를 직접 비교한다:

```bash
"$VENV" -c "
from hybrid_search.config import load_config
from hybrid_search.storage.indexes import get_project_dir, IndexPaths
from hybrid_search.storage.db import StoreDB
from hybrid_search.project import project_hash
import tantivy, usearch.index
from pathlib import Path

pid = project_hash(str(Path('$PROJECT_ROOT').resolve()))
config = load_config()
project_dir = get_project_dir(config.projects_dir, pid)
idx = IndexPaths(project_dir)

if not idx.store_db.exists():
    print('인덱스 없음 — delta reindex로 충분')
else:
    db = StoreDB(idx.store_db)
    sqlite = db.get_chunk_count(pid)
    db.close()
    t = tantivy.Index.open(str(idx.tantivy_dir))
    bm25 = t.searcher().num_docs
    v = usearch.index.Index.restore(str(idx.vector_index))
    vec = len(v)
    print(f'SQLite={sqlite}, Tantivy={bm25}, USearch={vec}')
    if sqlite == bm25 == vec:
        print('일치 — force rebuild 불필요')
    else:
        print('불일치 — force rebuild 필요')
"
```

사용자에게 결과를 보고한다.

## Step 2: 판단 분기

### 불일치 발견 → Force Rebuild

전체 파일을 다시 chunking + embedding한다.
- atomic rebuild로 실행되므로, 실패해도 기존 인덱스는 보존된다
- 파일 수에 따라 수 분~수십 분 소요된다
- OpenAI embedding API 비용이 발생한다

```bash
"$VENV" -m hybrid_search.cli reindex --force --cwd "$PROJECT_ROOT"
```

파일이 500개 이상이면 백그라운드 실행을 권장한다.

### 불일치 없음 → Delta Reindex

변경된 파일만 재인덱싱한다. 비용이 훨씬 적다.

```bash
"$VENV" -m hybrid_search.cli reindex --cwd "$PROJECT_ROOT"
```

### 인덱스 자체가 없음 → 첫 인덱싱

```bash
"$VENV" -m hybrid_search.cli reindex --cwd "$PROJECT_ROOT"
```

## Step 3: 결과 검증

rebuild/reindex 완료 후 상태를 다시 확인한다.

```bash
"$VENV" -m hybrid_search.cli status --cwd "$PROJECT_ROOT"
```

검증 항목:
- chunk 수가 합리적인지
- 에러 없이 완료되었는지
- 마지막 인덱싱 시간이 방금으로 갱신되었는지
- (force rebuild 한 경우) 3-store 일치 여부 재확인

결과를 사용자에게 보고한다.

## 주의사항

- force rebuild는 OpenAI embedding API 호출 비용이 발생한다
- 대형 프로젝트(1000+ 파일)는 수 분 이상 걸린다
- 실패해도 기존 인덱스는 유지된다 (atomic rebuild)
- 동시에 다른 reindex가 돌고 있으면 lock file 충돌 가능 — 먼저 확인할 것
- 단순히 최신 반영이 필요한 거면 `/maintain`을 사용할 것
