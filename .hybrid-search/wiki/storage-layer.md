# Storage Layer
> 마지막 업데이트: 2026-04-14 | 상태: fresh | synthesized: 2026-04-14

## Overview

hybrid-search의 스토리지 레이어는 세 가지 독립적인 스토어(SQLite WAL, USearch HNSW, Tantivy BM25)를 결합하여 하이브리드 검색을 구현한다. SQLite(`store.db`)는 메타데이터, 청크, 콜그래프, 위키를 관리하고, USearch(`vectors/vectors.usearch`)는 코사인 유사도 벡터 검색을, Tantivy(`tantivy/`)는 전문 검색을 담당한다. `IndexPaths` 유틸리티가 프로젝트별 경로를 캡슐화하며, 인덱싱 파이프라인이 세 스토어를 원자적으로 갱신한다.

## Key Design Decisions

- **INSERT + ON CONFLICT 대신 INSERT OR REPLACE 회피**: `upsert_file`에서 `REPLACE`는 DELETE+INSERT로 동작하여 FK CASCADE가 chunks를 삭제하므로, `ON CONFLICT(id) DO UPDATE`를 사용한다 (`src/hybrid_search/storage/db.py:L274`)
- **USearch 오버페치 (limit * 3)**: 벡터 검색 시 `chunk_ids_filter` 적용 후 결과가 부족할 수 있으므로 요청량의 3배를 미리 검색한다 (`src/hybrid_search/search/vector.py`)
- **양방향 키 매핑**: USearch는 정수 키만 지원하므로 `_key_to_id`/`_id_to_key` 딕셔너리로 chunk_id ↔ int 매핑을 유지하고, `key_mapping.npz`로 영속화한다 (`src/hybrid_search/search/vector.py`)
- **BEGIN IMMEDIATE 트랜잭션**: WAL 모드에서도 쓰기 충돌을 방지하기 위해 즉시 쓰기 락을 획득한다 (`src/hybrid_search/storage/db.py:L180`)
- **Tantivy 스키마 불일치 시 자동 recreate**: write 모드에서 기존 인덱스와 스키마가 다르면 자동으로 재생성하여 스키마 마이그레이션 문제를 방지한다 (`src/hybrid_search/search/bm25.py`)
- **콜그래프 confidence 3단계**: `call_edges.confidence`는 `low | medium | high`로 분류되며, `_confidence_filter`가 최소 신뢰도 이상만 반환한다 (`src/hybrid_search/storage/db.py:L19`)
- **WikiStore 팩토리 패턴**: `StoreDB.wiki_store()`가 동일한 DB 커넥션을 공유하는 `WikiStore` 인스턴스를 생성하여 별도 커넥션 오버헤드를 방지한다 (`src/hybrid_search/storage/db.py:L195`)

## Data Flow

```
인덱싱 파이프라인
    |
    +---> StoreDB.transaction()  -----> SQLite (store.db)
    |         |                           - files, chunks
    |         |                           - call_edges
    |         |                           - wiki_pages
    |         |                           - wiki_dependencies
    |         v
    +---> VectorEngine.add_batch() ---> USearch (vectors.usearch)
    |                                    + key_mapping.npz
    |
    +---> BM25Engine.add() -----------> Tantivy (tantivy/)
    |
    v
  save/commit 호출 시 디스크 영속화

검색 시:
  Query --> VectorEngine.search()  --> cosine similarity 결과
        --> BM25Engine.search()    --> BM25 score 결과
        --> StoreDB.get_chunk()    --> 메타데이터 보강
        --> RRF/가중 합성          --> 최종 결과
```

## Caveats

- **벡터/BM25 커밋 후 SQLite 롤백 시 불일치**: SQLite는 ROLLBACK으로 복구되지만, 이미 `save()`/`commit()`된 벡터/BM25 데이터는 복구되지 않는다. 다음 reindex에서 정합성이 복구된다 (`src/hybrid_search/storage/db.py:L180`)
- **`delete_all()`은 shutil.rmtree**: 프로젝트 디렉토리 전체를 삭제하므로, 잘못된 경로가 전달되면 의도하지 않은 데이터 손실이 발생할 수 있다 ()
- **distance -> similarity 변환 가정**: `similarity = 1.0 - distance`는 USearch의 코사인 거리가 [0, 2] 범위일 때만 유효하다; 다른 메트릭 사용 시 음수 similarity가 발생할 수 있다 (`src/hybrid_search/search/vector.py`)
- **check_same_thread=False**: SQLite 커넥션을 멀티스레드에서 사용 가능하게 하지만, WAL 모드에서도 동시 쓰기는 직렬화된다 (`src/hybrid_search/storage/db.py:L158`)

## Related Modules

- [[storage-(isolated)]] -- StoreDB와 IndexPaths의 구현 상세
- [[search-(isolated)]] -- 검색 오케스트레이터가 세 스토어를 조합하여 사용
- [[architecture]] -- 3중 스토어 아키텍처의 설계 근거

<details>
<summary>Structure (auto-generated)</summary>

## 개요 (3중 스토어 아키텍처)

hybrid-search는 세 가지 독립 스토어를 결합하여 하이브리드 검색을 구현한다.

| 스토어 | 엔진 | 파일 | 역할 |
|--------|------|------|------|
| **SQLite** | Python sqlite3 (WAL) | `store.db` | 메타데이터, 청크, 콜그래프, 위키 |
| **USearch** | Rust HNSW via usearch | `vectors/vectors.usearch` | 코사인 유사도 벡터 검색 |
| **Tantivy** | Rust BM25 via tantivy-py | `tantivy/` | 전문(full-text) 검색 |

세 스토어는 `IndexPaths` 유틸리티로 경로를 관리하며, 인덱싱 파이프라인이 원자적으로 갱신한다.

## SQLite WAL (store.db)

`StoreDB` 클래스 (`storage/db.py`)가 관리. `PRAGMA journal_mode=WAL` + `foreign_keys=ON`.

### 테이블 구조

- **index_meta** -- `(key PK, value)`. 스키마 버전(`SCHEMA_VERSION = "2"`), 임베딩 모델명 등
- **files** -- `(id PK, project_id, relative_path, file_hash, file_size, file_mtime, language, chunk_count)`. `UNIQUE(project_id, relative_path)`
- **chunks** -- `(id PK, file_id FK, project_id, name, qualified_name, node_type, start_line, end_line, start_byte, end_byte, content, embedding_input, docstring, parent_name)`. 인덱스 5개 (project, file, name, qualified_name, node_type)
- **call_edges** -- `(caller_chunk_id FK, callee_name, callee_qualified_name, callee_chunk_id, callee_module, project_id, confidence)`. confidence는 `low | medium | high`
- **wiki_pages** -- `(id PK, project_id, query_key, title, content, tags, created_at, updated_at, accessed_at, access_count, version)`. LRU 기반 eviction 지원
- **wiki_dependencies** -- `(wiki_page_id FK, file_id FK, file_hash_at_compile)`. 파일 변경 시 위키 staleness 감지용

### 주요 메서드

```
StoreDB.__init__(db_path)       # WAL 모드 연결 + 스키마 초기화
StoreDB.transaction()           # BEGIN IMMEDIATE → yield → COMMIT (실패 시 ROLLBACK)
StoreDB.upsert_file(record)     # INSERT OR REPLACE
StoreDB.get_callers(chunk_id)   # call_edges 역방향 조회
StoreDB.get_callees(chunk_id)   # call_edges 순방향 조회
StoreDB.wiki_store(max_pages)   # WikiStore 인스턴스 팩토리
```

## USearch (HNSW 벡터 인덱스)

`VectorEngine` 클래스 (`search/vector.py`).

### 파라미터 (design doc 13)
- **metric**: `MetricKind.Cos` (코사인 거리)
- **connectivity (M)**: 16
- **expansion_add (ef_construction)**: 200
- **ndim**: 임베딩 모델 차원 (런타임 주입)

### 키 매핑
USearch는 정수 키만 지원하므로 `_key_to_id: dict[int, str]` / `_id_to_key: dict[str, int]` 양방향 매핑을 유지한다. `key_mapping.npz`로 디스크 영속화.

### 검색 흐름
```
search(query_vector, limit=10, chunk_ids_filter=None)
  → USearch.search(limit * 3)   # 필터링 여유분 확보
  → distance → similarity 변환: similarity = 1.0 - distance
  → chunk_ids_filter 적용 후 상위 limit개 반환
```

### 영속화
- `vectors.usearch` -- HNSW 그래프
- `key_mapping.npz` -- int-key ↔ chunk_id 매핑

## Tantivy (Rust BM25)

`BM25Engine` 클래스 (`search/bm25.py`).

### 스키마 필드
| 필드 | stored | tokenizer | 용도 |
|------|--------|-----------|------|
| `chunk_id` | True | `raw` | 식별자 (토큰화 안 함) |
| `name` | True | default | 심볼 이름 검색 |
| `qualified_name` | True | default | 정규화된 이름 검색 |
| `content` | False | default | 코드 본문 전문 검색 |
| `docstring` | False | default | 독스트링 전문 검색 |

### 설정
- `heap_size`: 50MB (writer)
- 스키마 불일치 시 자동 recreate (write 모드 한정)
- `read_only=True` 옵션: writer 생성 스킵 (cross-project 검색용)

## 디렉토리 구조

```
~/.hybrid-search/projects/{project_hash}/
  ├── store.db            # SQLite WAL
  ├── store.db-wal        # WAL 저널
  ├── store.db-shm        # 공유 메모리
  ├── store.db.lock       # 파일 락
  ├── tantivy/            # BM25 인덱스 세그먼트
  └── vectors/
      ├── vectors.usearch # HNSW 그래프
      └── key_mapping.npz # int ↔ chunk_id
```

## 트랜잭션 & 일관성

인덱싱 파이프라인은 파일 단위로 3중 스토어를 갱신한다:

1. `StoreDB.transaction()` -- `BEGIN IMMEDIATE` 락으로 SQLite 원자성 보장
2. SQLite INSERT/UPDATE 완료 후 `VectorEngine.add_batch()` + `BM25Engine.add()`
3. 인덱싱 완료 시 `VectorEngine.save()` + `BM25Engine.commit()` 호출

**실패 시**: SQLite는 ROLLBACK으로 복구. 벡터/BM25는 save/commit 전이면 메모리에만 존재하므로 디스크 불일치 없음. 이미 커밋된 경우 다음 reindex에서 정합성 복구.

## IndexPaths 유틸리티

`storage/indexes.py`의 `IndexPaths` 클래스가 프로젝트별 경로를 캡슐화한다.

```python
paths = IndexPaths(project_dir)
paths.store_db      # → project_dir / "store.db"
paths.tantivy_dir   # → project_dir / "tantivy"
paths.vectors_dir   # → project_dir / "vectors"
paths.lock_file     # → project_dir / "store.db.lock"
paths.ensure_dirs() # tantivy + vectors 디렉토리 생성
paths.delete_all()  # shutil.rmtree(project_dir) — 전체 삭제
```

`get_project_dir(projects_dir, project_id)` 헬퍼로 프로젝트 ID → 디렉토리 매핑.

</details>