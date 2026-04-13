# Indexing Pipeline
> 마지막 업데이트: 2026-04-14 | 상태: fresh

## 개요

프로젝트 디렉토리를 스캔하여 코드/문서 파일을 청킹하고, OpenAI 임베딩을 생성한 뒤
SQLite + Tantivy(BM25) + USearch(벡터) 3개 스토어에 동기화하는 파이프라인이다.

## 2-pass 아키텍처

**Pass 1 -- 청킹 (CPU only, no API call)**
- 파일별로 `_chunk_file()` 호출 -> `CodeChunk` 리스트 생성
- 코드 파일: `chunk_code_file()` (tree-sitter AST 기반)
- 문서 파일(md/json/yaml/toml): `chunk_doc_file()`
- 결과를 `_FileChunkResult`에 담아 `pending` 버퍼에 누적

**Pass 2 -- 배치 임베딩 + 저장 (API call)**
- `pending_chunk_count >= EMBED_FLUSH_THRESHOLD(128)` 도달 시 `_flush_pending()` 실행
- 모든 pending 파일의 `embedding_input`을 하나의 리스트로 모아 `Embedder.embed_texts()` 호출
- 반환된 벡터를 파일별로 슬라이싱하여 `_store_file()`로 3개 스토어에 기록

이 구조 덕분에 파일 경계를 넘어서 임베딩을 배치 처리할 수 있어 API 호출 횟수를 최소화한다.

## 핵심 클래스/함수

### `IndexingPipeline.__init__(config, registry, embedder)`
오케스트레이터. Config, ProjectRegistry, Embedder를 주입받는다.

### `IndexingPipeline.index_project(project_path, project_name?, force?, on_progress?)`
-> `IndexingResult`

메인 진입점. 프로젝트 등록 -> 스캔 -> 삭제 처리 -> 2-pass 인덱싱 -> call graph 해석 -> 일관성 검증 순서로 실행.

### `_chunk_file(db, file_path, project_root, project_id)`
-> `_FileChunkResult | None`

Pass 1 단위 작업. 언어 감지 -> 소스 읽기 -> 해시 계산 -> 청커 호출 -> 기존 chunk_id 조회.

### `_flush_pending(pending, db, vector_engine, bm25_engine, project_id, result)`

Pass 2 배치 처리. pending 전체의 텍스트를 모아 `embed_texts()` 한 번 호출 후 `_store_file()`로 분배.

### `_store_file(db, vector_engine, bm25_engine, fcr, embeddings, project_id)`

Multi-store 원자적 기록:
1. SQLite 트랜잭션 내: file record upsert -> old call_edges/chunks 삭제 -> new chunks/edges 삽입 -> file record 최종 갱신
2. 트랜잭션 외: BM25 old 삭제 + new 추가, Vector old 삭제 + new 추가

file_hash를 **마지막**에 기록하여 crash recovery 마커로 활용 (hash="" = 미완료).

### `_process_deletions(db, vector_engine, bm25_engine, project_id, deleted_paths)`

삭제된 파일의 chunks, call_edges, vectors, BM25 문서를 모두 제거.

## Delta 인덱싱

`scanner.scan_project()` 가 디스크 vs DB 상태를 비교하여 `ScanResult(added, changed, deleted)`를 반환한다.

**변경 감지 전략 (`_is_changed`)**:
1. `file_hash == ""` -> 크래시 복구 대상, 무조건 재인덱싱
2. `file_size` 불일치 -> SHA256 해시 비교로 확인
3. `file_mtime` 불일치 -> SHA256 해시 비교로 확인
4. size + mtime 모두 동일 -> 스킵

(size, mtime) prefilter로 대부분의 파일을 해시 계산 없이 빠르게 스킵한다.

**삭제 감지**: DB에 있지만 디스크에 없는 파일을 `deleted`로 분류.

## OpenAI 임베딩

`Embedder` 클래스 (`src/hybrid_search/index/embedder.py`)

- **모델**: `text-embedding-3-small` (1536차원)
- **API**: `urllib.request`로 직접 호출 (외부 SDK 의존 없음)
- **배치**: `batch_size=100` (config에서 설정 가능, OpenAI 최대 2048)
- **토큰 제한**: `tiktoken`으로 8000 토큰에서 truncate (OpenAI 한도 8192)
- **정규화**: 반환 벡터를 L2 정규화 (OpenAI가 이미 정규화하지만 검증 차원)

```python
Embedder.embed_texts(texts: list[str]) -> np.ndarray   # (N, 1536) float32
Embedder.embed_query(query: str) -> np.ndarray          # (1536,) float32
Embedder._truncate(text: str, max_tokens=8000) -> str
```

API 키는 환경변수 `OPENAI_API_KEY` 또는 프로젝트 루트의 `.env.local`에서 로드.

## 체크포인트

`EMBED_FLUSH_THRESHOLD = 128` 청크마다 중간 저장:

```
파일 순회 -> pending 버퍼 누적
   |
   +-- pending_chunk_count >= 128 ?
         -> _flush_pending() 실행
         -> bm25_engine.commit() + vector_engine.save()  # 체크포인트
         -> pending 클리어
```

루프 종료 후 남은 pending도 flush. 최종적으로 한 번 더 `commit()` + `save()`.

이 방식으로 대규모 프로젝트에서도 메모리 사용량을 일정하게 유지하면서, 크래시 시 마지막 체크포인트까지의 작업이 보존된다.

## 에러 처리

| 상황 | 처리 방식 |
|------|----------|
| 파일 청킹 실패 | `result.errors`에 기록, 해당 파일 스킵, 나머지 계속 |
| 파일 저장 실패 | `result.errors`에 기록, 해당 파일 스킵 |
| 일관성 불일치 (SQLite != Tantivy != USearch) | 경고 로그 + `force=True`로 자동 전체 재빌드 |
| Call graph 해석 실패 | 경고 로그, non-fatal (검색은 작동) |
| 크래시 복구 (file_hash="") | 다음 인덱싱 시 해당 파일 자동 재처리 |

일관성 검증은 `index_project()` 끝에서 3개 스토어의 카운트를 비교하며, 불일치 시 재귀적으로 `force=True` 재인덱싱을 실행한다. 이미 `force=True`인 경우에는 재귀하지 않는다.
