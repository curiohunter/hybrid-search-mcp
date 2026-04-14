# Architecture
> 마지막 업데이트: 2026-04-14 | 상태: fresh | synthesized: 2026-04-14

## Overview

Hybrid-search-mcp is a 100% local BM25+Vector hybrid search MCP server purpose-built for cross-language code search — Korean natural language queries finding English code. It combines a Rust BM25 engine (Tantivy), a C++ HNSW vector index (USearch), and OpenAI embeddings with RRF fusion, exposed as 3 MCP tools for Claude Code and 12 CLI commands for management. On top of search, it auto-generates wiki documentation from call graph analysis (DAG → connected components → topological sort → module wiki pages), with LLM synthesis adding explanatory content grounded in deterministic structural data.

## Key Design Decisions

- **Native extensions for hot paths**: Python orchestrates, but search-critical operations use C/C++/Rust backends — tree-sitter (C) for AST parsing, Tantivy (Rust) for BM25, USearch (C++ SIMD) for vector search — keeping query latency under 500ms
- **3-stage query classification**: Queries are classified as EXACT_SYMBOL (bm25_weight=0.8), KOREAN_NL (0.15), or ENGLISH_NL (0.4), dynamically adjusting the BM25/vector balance because Korean text tokenizes poorly for BM25 but excels in semantic embedding space
- **OpenAI embeddings over local models**: After iterating through sentence-transformers → ONNX INT8 → Ollama GPU, the project settled on OpenAI `text-embedding-3-small` for zero local resource usage on an 18GB MacBook Air at ~$0.04/project indexing cost
- **MCP tool minimalism**: Only 3 tools exposed via MCP (hybrid_search, trace_callers, trace_callees) out of 13 original — tool schemas load into every Claude Code system prompt, so fewer tools = less per-conversation token overhead
- **Deterministic wiki as ground truth**: Wiki pages are first auto-generated from code structure (no LLM), then LLM synthesis is layered on top with mandatory `file:line` citations — hallucination is constrained by structural data and reference verification

## Data Flow

```
Indexing:
  Source files → Scanner (delta hash) → AST/Doc Chunker → Embedder (OpenAI API)
    → Triple-store write: SQLite + Tantivy + USearch
      → Call Graph Resolution (import-call binding)
        → Wiki staleness marking

Search:
  Query → Query Classifier → BM25 + Vector parallel
    → RRF Fusion (k=60) → Ranked Results

Wiki Generation:
  Call edges → DAG → Connected Components (BFS) → Kahn's Topological Sort
    → Deterministic wiki → LLM Synthesis (prepare/finalize) → Merged wiki
```

## Caveats

- The triple-store architecture requires manual consistency management — SQLite has transactional rollback but USearch/Tantivy do not, so partial failures during indexing can leave stores out of sync until the next reindex
- Cross-project search opens multiple project stores simultaneously, each with its own SQLite connection, BM25 index, and vector index — memory usage scales linearly with registered project count
- The MCP server runs as a single-process stdio server; there is no built-in concurrency control for multiple Claude Code sessions connecting simultaneously

## Related Modules

- [[search]] -- core search orchestration (query classification, BM25+vector, RRF fusion)
- [[storage-layer]] -- triple-store persistence (SQLite, USearch, Tantivy)
- [[call-graph-&-module-tree]] -- DAG construction and topological sort for wiki generation
- [[wiki-system]] -- wiki CRUD, staleness tracking, wikilink graph
- [[mcp-server-&-cli]] -- external interfaces (MCP server + CLI commands)
- [[embedder----openai-api-backend]] -- OpenAI embedding generation

<details>
<summary>Structure (auto-generated)</summary>

## 개요

100% 로컬 BM25+Vector 하이브리드 검색 MCP 서버. 한국어 자연어 쿼리로 영어 코드를 찾는 크로스 언어 코드 검색과, call graph 기반 자동 wiki 생성을 제공한다.

## 기술 스택

| 컴포넌트 | 패키지 | 실제 백엔드 |
|----------|--------|-----------|
| MCP 서버 | `mcp[cli]` | Python (stdio JSON-RPC) |
| 임베딩 | `openai` / `onnxruntime` | OpenAI API 또는 로컬 ONNX |
| BM25 인덱스 | `tantivy-py` | Rust (tantivy) |
| AST 파싱 | `tree-sitter` | C (tree-sitter) |
| Vector DB | `usearch` | C++ (HNSW, SIMD) |
| 메타 저장소 | `sqlite3` | SQLite |
| 패키지 관리 | `uv` | - |

## 모듈 구조

```
src/hybrid_search/
  server.py          # MCP 서버 진입점, 3개 MCP 도구 노출
  cli.py             # CLI 진입점 (reindex, status, wiki 등)
  config.py          # ~/.hybrid-search/config.toml 로딩
  project.py         # ProjectRegistry — 멀티 프로젝트 등록/조회

  index/             # 인덱싱 파이프라인
    pipeline.py      #   scanner -> chunker -> embedder -> store 오케스트레이션
    scanner.py       #   파일 스캔 + delta 감지 (hash 비교)
    ast_chunker.py   #   tree-sitter AST 기반 코드 청킹
    doc_chunker.py   #   MD/JSON/YAML 문서 청킹
    embedder.py      #   임베딩 생성 (OpenAI / ONNX)
    callgraph.py     #   import-call 바인딩 + call edge 해석
    dag.py           #   모듈 트리 DAG + 위상정렬 + wiki plan 생성

  search/            # 검색 엔진
    orchestrator.py  #   쿼리 분류 + BM25/Vector 병렬 실행 + RRF 합산
    bm25.py          #   tantivy 래퍼
    vector.py        #   USearch HNSW 래퍼
    fusion.py        #   RRF(Reciprocal Rank Fusion) 알고리즘

  storage/           # 저장소
    db.py            #   SQLite 메타 스토어 (files, chunks, call_edges, wiki)
    indexes.py       #   프로젝트별 디렉터리 경로 관리
    wiki.py          #   wiki 페이지 CRUD + staleness 추적

  tools/             # MCP 도구 핸들러
    hybrid_search.py #   hybrid_search 도구
    trace.py         #   trace_callers / trace_callees 도구
    symbols.py       #   search_symbols 도구
    index.py         #   index_project / index_status 도구
    projects.py      #   list_projects / remove_project 도구
    wiki.py          #   lookup_wiki / compile_to_wiki 등
    semantic_search.py # semantic_search 도구
```

## 데이터 흐름

### 인덱싱 (reindex)

```
소스 파일
  │
  ▼
Scanner (delta: hash 비교)
  │ 변경된 파일만
  ▼
AST Chunker ──── Doc Chunker
  │ (코드)          │ (MD 등)
  ▼                 ▼
  ┌─────────────────┐
  │ Embedder (batch) │
  └────────┬────────┘
           ▼
  ┌──────────────────────────┐
  │ Store 동시 업데이트        │
  │  SQLite (메타/chunks)     │
  │  tantivy (BM25 토큰)      │
  │  USearch (벡터)            │
  └──────────────────────────┘
           ▼
  Call Graph Resolution
           ▼
  Wiki Staleness 마킹 + Auto-sync
```

### 검색 (hybrid_search)

```
쿼리 ("로그인 처리")
  │
  ▼
Query Classifier
  │  KOREAN_NL → bm25_weight=0.15
  │  EXACT_SYMBOL → bm25_weight=0.8
  │  ENGLISH_NL → bm25_weight=0.4
  ▼
  ┌────────┐     ┌────────┐
  │ BM25   │     │ Vector │
  │ Engine │     │ Engine │
  └───┬────┘     └───┬────┘
      │              │
      ▼              ▼
   RRF Fusion (k=60)
      │
      ▼
  Ranked Results (JSON)
```

## MCP 도구 / CLI 명령 목록

### MCP 도구 (server.py, 3개)

| 도구 | 설명 |
|------|------|
| `hybrid_search` | BM25+Vector 하이브리드 검색 (크로스 언어) |
| `trace_callers` | 역방향 call graph 탐색 |
| `trace_callees` | 순방향 call graph 탐색 |

> 나머지 도구(index_project, search_symbols, list_projects 등)는 MCP tools/ 핸들러에 존재하며, 추가 MCP 서버 설정으로 노출 가능.

### CLI 명령 (`python -m hybrid_search.cli`)

| 명령 | 설명 |
|------|------|
| `reindex` | Delta 인덱싱 (--force, --wiki) |
| `status` | 전체 프로젝트 인덱스 상태 |
| `stale` | wiki staleness 체크 |
| `install-hook` | git post-commit hook 설치 |
| `sync-wiki` | 디스크 wiki -> DB 동기화 |
| `call-graph-stats` | call graph 해석 통계 |
| `generate-wiki-plan` | 모듈 트리 DAG 생성 |
| `verify-wiki` | wiki 커버리지 검증 |
| `generate-wiki` | 모듈 트리 기반 wiki 자동 생성 |
| `search-symbols` | 심볼 이름 검색 |
| `remove-project` | 프로젝트 등록 해제 |
| `lookup-wiki` | wiki 페이지 조회 |

## 설정 (config.toml 핵심 필드)

파일 위치: `~/.hybrid-search/config.toml`

```toml
[general]
data_dir = "~/.hybrid-search"    # 글로벌 데이터 디렉터리

[embedding]
backend = "openai"               # "openai" | "onnx" | "ollama"
openai_model = "text-embedding-3-small"
batch_size = 100

[search]
default_limit = 10
rrf_k = 60                       # RRF 파라미터
query_classifier = true          # 쿼리 타입 자동 분류
default_bm25_weight = 0.5

[indexing]
max_file_size_kb = 512
exclude_patterns = ["node_modules", ".git", "__pycache__", ...]

[wiki]
max_pages_per_project = 100
eviction_policy = "lru"

[[projects]]
name = "my-project"
path = "/path/to/project"
```

데이터 저장 구조: `~/.hybrid-search/projects/<id>/` 하위에 `store.db`(SQLite), `tantivy/`(BM25), `vectors.usearch`(HNSW) 생성.

</details>