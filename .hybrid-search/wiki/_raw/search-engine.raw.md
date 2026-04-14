# Search Engine
> 마지막 업데이트: 2026-04-14 | 상태: fresh

## 개요

하이브리드 검색 엔진은 **BM25(키워드)** + **Vector(시맨틱)** 두 채널을 병렬 실행한 뒤
**RRF(Reciprocal Rank Fusion)** 로 순위를 합산하는 구조다.
쿼리 분류기가 가중치를 자동 조절하므로 심볼 검색과 자연어 검색 모두 단일 API로 처리된다.

핵심 파일:
- `src/hybrid_search/search/orchestrator.py` -- 쿼리 분류 + 검색 코디네이션
- `src/hybrid_search/search/bm25.py` -- Tantivy BM25 엔진
- `src/hybrid_search/search/vector.py` -- USearch HNSW 벡터 엔진
- `src/hybrid_search/search/fusion.py` -- RRF 알고리즘

## 검색 흐름

```
쿼리 입력
  │
  ├─ classify_query() ── EXACT_SYMBOL / KOREAN_NL / ENGLISH_NL
  │                        → bm25_weight 결정
  │
  ├─ Embedder.embed_query() ── 쿼리 벡터 생성
  │
  ├─ (단일 프로젝트)  _search_single()
  │   또는
  ├─ (멀티 프로젝트)  _search_cross_project()
  │     ├─ ThreadPoolExecutor(max_workers=4) 병렬 검색
  │     ├─ BM25: round-robin 인터리브 (primary 2:1 우선)
  │     └─ Vector: cosine 정렬 (primary +0.05 부스트)
  │
  ├─ reciprocal_rank_fusion(bm25_ids, vector_ids, k=60, bm25_weight)
  │
  └─ _enrich_results() ── StoreDB에서 chunk 메타데이터 조회 → HybridResult
```

retrieval_depth = `limit * 3` (요청 개수의 3배를 각 채널에서 가져온 뒤 fusion).

## 쿼리 분류기

`classify_query()` -- 3단계 분류, `QUERY_WEIGHTS` 딕셔너리로 BM25 가중치 매핑.

| 분류 | 조건 | BM25 가중치 | Vector 가중치 |
|------|------|-------------|---------------|
| **EXACT_SYMBOL** | camelCase, snake_case, SCREAMING_SNAKE, dot-qualified | 0.80 | 0.20 |
| **KOREAN_NL** | 한글 비율 > 50% | 0.15 | 0.85 |
| **ENGLISH_NL** | 그 외 | 0.40 | 0.60 |

혼합 쿼리(심볼 + 한글)는 중간 가중치 **0.40**을 사용한다.
`bm25_weight` 파라미터를 명시하면 자동 분류를 오버라이드할 수 있다.

정규식 패턴 (`_SYMBOL_RE`):
- `signIn`, `createUser` (camelCase)
- `tuition_fees` (snake_case)
- `MAX_RETRIES` (SCREAMING_SNAKE)
- `AuthService.signIn` (dot-qualified)

## BM25 (Tantivy, Rust 기반)

- **엔진**: `tantivy` Python 바인딩 (Rust 구현)
- **인덱스 필드**: content, name, qualified_name, docstring (청크당)
- **모드**: `read_only=True` (검색 전용) / `False` (쓰기 가능)
- **스키마 불일치 시**: write 모드에서 자동 재생성, read_only 모드에선 경고 후 None 반환
- **결과**: `BM25Result(chunk_id, score)`

## Vector Search (USearch, HNSW, cosine)

- **엔진**: `usearch.index.Index` (C++ 구현)
- **메트릭**: `MetricKind.Cos` (코사인 유사도)
- **HNSW 파라미터**: `M=16`, `ef_construction=200`
- **내부 매핑**: 정수 키 <-> chunk_id 문자열 양방향 딕셔너리
- **저장 경로**: `{project_dir}/vectors.usearch`
- **결과**: `VectorResult(chunk_id, score)`
- `chunk_ids_filter` 파라미터로 file_pattern/node_type 필터링 지원

## RRF Fusion (k=60, 순위 기반 합산)

`reciprocal_rank_fusion()` in `fusion.py`:

```
RRF_score(chunk) = bm25_weight / (k + bm25_rank) + vector_weight / (k + vector_rank)
```

- **k=60**: Cormack et al. 논문 기준 표준값 (config에서 `search.rrf_k`로 조정 가능)
- **bm25_weight**: 쿼리 분류기가 결정 (0.15 ~ 0.80)
- **vector_weight**: `1.0 - bm25_weight`
- 한쪽 채널에만 등장한 청크도 점수를 받음 (누락 채널 기여분은 0)
- 결과: `FusedResult(chunk_id, rrf_score, bm25_rank, vector_rank)`

## 멀티 프로젝트 검색 (CWD 부스트)

`_search_cross_project()` -- 등록된 모든 프로젝트를 병렬 검색 후 병합.

**BM25 병합**: round-robin 인터리브 (`_interleave_round_robin`)
- primary 프로젝트가 있으면 `_weighted_interleave(primary, secondary, ratio=2)` -- 2:1 비율

**Vector 병합**: 전체 cosine similarity로 정렬
- primary 프로젝트 청크에 **+0.05 부스트** 적용

**CWD 감지**: `_detect_primary_project(cwd, project_infos)`
- cwd 경로가 등록 프로젝트 경로에 포함되면 해당 프로젝트를 primary로 지정
- `project` 파라미터가 명시되면 CWD 감지를 건너뜀

**타임아웃**: 프로젝트당 `PROJECT_TIMEOUT_S = 2.0`초, 초과 시 skipped 목록에 추가.

**필터링**: `_build_filter()` -- file_pattern(fnmatch), node_types로 chunk ID 집합 생성.
파일 경로 캐시로 N+1 쿼리를 방지한다.
