# Hybrid Search MCP Server — Design Document

> **Status**: Draft v5 | **Date**: 2026-04-13 | **Author**: Ian + Claude
> **Review**: Codex review x3 + Brightdata 웹 리서치 반영 (v1 → v2 → v3 → v4 → v5)

## 1. Problem Statement

MindVault(BM25 기반)로 코드베이스 컨텍스트를 자동 제공하고 있으나, **한국어 자연어 ↔ 영어 코드** 매칭이 불가능한 근본적 한계가 있다.

| 쿼리 | 기대 결과 | BM25 결과 |
|------|----------|----------|
| "로그인" | `signIn()`, `auth/login.ts` | 매칭 실패 |
| "학원비" | `tuition_fees`, `billing/` | 매칭 실패 |
| "사용자 인증" | `middleware.ts:createSupabaseMiddlewareClient` | 매칭 실패 |

**Vector Embedding**은 의미 공간에서 매칭하므로 크로스 언어 검색이 가능하다. BM25(정확한 키워드)와 Vector(의미적 유사도)를 **RRF로 합산**하면 두 방식의 장점을 모두 취할 수 있다.

## 2. Goals & Non-Goals

### Goals
- **G1**: BM25 + Vector Hybrid Search (RRF fusion)
- **G2**: 100% 로컬 실행 (API 키 불필요)
- **G3**: 한국어 ↔ 영어 크로스 언어 코드 검색
- **G4**: Claude Code MCP 서버로 도구 노출
- **G5**: AST 기반 코드 청킹 (함수/클래스 단위)
- **G6**: Delta 인덱싱 (변경 파일만 재인덱싱)
- **G7**: 문서(MD, JSON, YAML) + 코드(TS/JS/Python 등) 모두 지원
- **G8**: 8개 프로젝트 글로벌 관리 및 크로스 프로젝트 검색

### Non-Goals
- 클라우드 서비스 의존 (OpenAI, Zilliz 등)
- GUI / Web UI
- MindVault 대체 (병행 사용)
- IDE 플러그인 (Claude Code MCP로 충분)
- 실시간 파일 감시 (watch mode) — v1에서는 명시적 인덱싱

## 3. 경쟁 분석 & 차별점

| 기능 | ck | grepai | claude-context | MindVault | **Ours** |
|------|:--:|:------:|:--------------:|:---------:|:--------:|
| Hybrid (BM25+Vector) | O | X | O | X | **O** |
| 100% 로컬 | O | O | X | O | **O** |
| 한국어↔영어 | X | X | △ | X | **O** |
| MCP 네이티브 | X | O | X | O | **O** |
| AST 청킹 | O | X | X | X | **O** |
| Delta 인덱싱 | O | X | X | X | **O** |
| 멀티 프로젝트 | X | X | X | O | **O** |
| 유지보수 | 2개월 정체 | 활발 | 활발 | 저활성 | 자체 |

**핵심 차별점**: 다국어 임베딩 + AST 청킹 + MCP 네이티브 + 멀티 프로젝트

## 4. Tech Stack Trade-off: Rust vs Python

### Option A: Rust

| 장점 | 단점 |
|------|------|
| 메모리 효율 (데몬 상주 시 유리) | 개발 속도 느림 |
| TreeSitter 네이티브 바인딩 | ML 생태계 약함 (ONNX Runtime은 가능) |
| ck가 증명한 실현 가능성 | MCP SDK 미성숙 (직접 JSON-RPC 구현) |
| 싱글 바이너리 배포 | 임베딩 모델 로딩 복잡 |

### Option B: Python

| 장점 | 단점 |
|------|------|
| sentence-transformers 직접 사용 | 메모리 사용량 높음 |
| MCP SDK 성숙 (mcp[cli]) | 속도 (인덱싱 시) |
| tree-sitter 바인딩 존재 | 패키징/배포 복잡 (venv) |
| 빠른 프로토타이핑 | GIL 제약 (멀티스레딩) |

### 결론: Python with Native Extensions — **권장**

Python 단일 스택이지만, 성능 핵심부는 이미 네이티브 바이너리(Rust/C++)로 동작한다.

| 컴포넌트 | Python 패키지 | 실제 백엔드 | 이유 |
|----------|-------------|-----------|------|
| MCP 서버 & 오케스트레이션 | `mcp[cli]` | Python | MCP SDK 성숙, 빠른 개발 |
| 임베딩 추론 | `onnxruntime` | C++ (ONNX) | GPU/MPS 가속 지원 |
| BM25 인덱스 | `tantivy-py` | **Rust** (tantivy) | Lucene급 성능 |
| AST 파싱 | `tree-sitter` | **C** (tree-sitter) | 네이티브 파서 |
| Vector DB | `usearch` | **C++** (USearch) | HNSW, SIMD 최적화 |

**성능 병목 시 Rust 교체 기준**: 프로파일링으로 Python 오케스트레이션 레이어가 전체 시간의 >30%를 차지하면 해당 컴포넌트를 Rust로 교체. 현재 핵심 연산은 이미 네이티브이므로 가능성 낮음.

**패키징 전략**: `uv`로 가상환경 관리. tree-sitter grammar은 `pip install tree-sitter-languages`로 일괄 설치.

**모델 다운로드 보안**:
- ONNX 모델은 최초 실행 시 HuggingFace에서 다운로드 후 `~/.hybrid-search/models/`에 캐싱
- **Pinned revision**: config.toml에 `model_revision = "abc123..."` (commit hash)을 명시. 기본값은 릴리즈 시 검증된 revision으로 하드코딩
- **Checksum 검증**: 다운로드 후 SHA256 checksum을 config의 `model_sha256`과 비교. 불일치 시 다운로드 실패 처리
- **오프라인 설치**: `~/.hybrid-search/models/`에 수동으로 ONNX 파일을 배치하면 다운로드 skip. `model_path` config로 로컬 경로 직접 지정 가능

## 5. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Claude Code (Client)                  │
│                                                         │
│  MCP Tools: hybrid_search, semantic_search,             │
│             trace_callers, trace_callees,               │
│             index_project, search_symbols,              │
│             index_status, list_projects, remove_project │
└──────────────────────┬──────────────────────────────────┘
                       │ stdio (JSON-RPC)
                       ▼
┌─────────────────────────────────────────────────────────┐
│              MCP Server Layer (Python)                   │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Tool Router  │  │ Project Mgr  │  │ Config Loader │  │
│  └──────┬──────┘  └──────┬───────┘  └───────────────┘  │
│         │                │                               │
│         ▼                ▼                               │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Search Orchestrator                 │    │
│  │                                                  │    │
│  │  ┌──────────┐   ┌──────────┐   ┌─────────────┐ │    │
│  │  │  BM25    │   │  Vector  │   │  RRF Fusion │ │    │
│  │  │  Engine  │   │  Engine  │   │  (k=60)     │ │    │
│  │  └────┬─────┘   └────┬─────┘   └──────┬──────┘ │    │
│  │       │              │                │         │    │
│  │       ▼              ▼                ▼         │    │
│  │  ┌──────────┐   ┌──────────┐   ┌──────────┐   │    │
│  │  │ Tantivy  │   │ USearch  │   │ Result   │   │    │
│  │  │ Index    │   │ HNSW     │   │ Ranker   │   │    │
│  │  └──────────┘   └──────────┘   └──────────┘   │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Indexing Pipeline                    │    │
│  │                                                  │    │
│  │  ┌──────────┐   ┌──────────┐   ┌─────────────┐ │    │
│  │  │ File     │   │ AST      │   │ Embedding   │ │    │
│  │  │ Scanner  │   │ Chunker  │   │ Generator   │ │    │
│  │  │ (delta)  │   │ (TS)     │   │ (multilang) │ │    │
│  │  └──────────┘   └──────────┘   └─────────────┘ │    │
│  │                                                  │    │
│  │  ┌──────────┐   ┌──────────┐                    │    │
│  │  │ Doc      │   │ Call     │                    │    │
│  │  │ Chunker  │   │ Graph   │                    │    │
│  │  │ (MD/etc) │   │ Builder │                    │    │
│  │  └──────────┘   └──────────┘                    │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │              Storage Layer                       │    │
│  │                                                  │    │
│  │  ~/.hybrid-search/                               │    │
│  │    ├── config.toml                               │    │
│  │    ├── projects/                                  │    │
│  │    │   ├── {project_hash}/                        │    │
│  │    │   │   ├── tantivy/    (BM25 index)          │    │
│  │    │   │   ├── vectors/    (HNSW index)          │    │
│  │    │   │   └── store.db    (SQLite: metadata,    │    │
│  │    │   │                    chunks, call_edges)   │    │
│  │    │   └── ...                                   │    │
│  │    └── global/             (cross-project alias   │    │
│  │         └── project_registry.db  index)          │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## 6. Module Structure

```
hybrid-search-mcp/
├── pyproject.toml                 # Project config (uv/pip)
├── src/
│   └── hybrid_search/
│       ├── __init__.py
│       ├── server.py              # MCP server entry point
│       ├── config.py              # Configuration loading
│       ├── project.py             # Project registry & management
│       │
│       ├── search/
│       │   ├── __init__.py
│       │   ├── orchestrator.py    # Hybrid search coordination
│       │   ├── bm25.py            # Tantivy-based BM25 engine
│       │   ├── vector.py          # USearch-based vector engine
│       │   └── fusion.py          # RRF fusion algorithm
│       │
│       ├── index/
│       │   ├── __init__.py
│       │   ├── pipeline.py        # Indexing orchestration
│       │   ├── scanner.py         # File discovery & delta detection
│       │   ├── ast_chunker.py     # TreeSitter AST-based chunking
│       │   ├── doc_chunker.py     # Markdown/text chunking
│       │   ├── embedder.py        # Embedding generation
│       │   └── callgraph.py       # Call graph extraction
│       │
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── db.py              # SQLite store.db (files, chunks, call_edges)
│       │   └── indexes.py         # Tantivy + USearch index management
│       │
│       └── tools/
│           ├── __init__.py
│           ├── hybrid_search.py   # hybrid_search tool
│           ├── semantic_search.py # semantic_search tool
│           ├── trace.py           # trace_callers / trace_callees
│           ├── index.py           # index_project / index_status tools
│           ├── symbols.py         # search_symbols tool
│           └── projects.py        # list_projects / remove_project tools
│
├── tests/
│   ├── test_bm25.py
│   ├── test_vector.py
│   ├── test_fusion.py
│   ├── test_ast_chunker.py
│   └── test_integration.py
│
└── docs/
    └── design.md                  # This file
```

## 7. Embedding Model Selection

### 요구사항
- 다국어 (한국어 + 영어 필수)
- 코드 이해 가능
- 로컬 실행 (ONNX 또는 GGUF)
- 적당한 차원수 (384~1024, 속도-품질 균형)

### 후보 비교

| 모델 | 차원 | 크기 | 한국어 | 코드 | 비고 |
|------|------|------|:------:|:----:|------|
| `intfloat/multilingual-e5-small` | 384 | 471MB | O | △ | 가볍고 다국어 |
| `intfloat/multilingual-e5-base` | 768 | 1.1GB | O | △ | 한국어 강함, 코드 약함 |
| `Alibaba-NLP/gte-multilingual-base` | 768 | 1.2GB | O | O | 코드+다국어 균형 |
| `BAAI/bge-m3` | 1024 | 2.2GB | O | O | Dense+Sparse+ColBERT |
| `Qwen/Qwen3-Embedding` | 1024 | ~2.5GB | O | O | bge-m3 능가 보고, 최신 |
| `nomic-embed-text-v1.5` | 768 | 548MB | △ | O | Ollama 지원, 한국어 약함 |

### 권장: Phase 1에서 벤치마크 후 결정

비교표에서 `gte-multilingual-base`와 `bge-m3`가 한국어+코드 모두 O로 평가되는 반면, `multilingual-e5-base`는 코드가 △다. **구현 전에 실제 벤치마크가 필수**.

**벤치마크 계획** (Phase 1 첫 번째 작업):
1. 평가 쿼리셋 30개 구성 (한국어 자연어 → 영어 코드 10개, 영어→영어 10개, 혼합 10개)
2. 실제 프로젝트(breeze) 코드를 대상으로 retrieval 평가
3. 메트릭: Recall@10, MRR, 인덱싱 속도, 메모리 사용량
4. 후보 3개 비교 (우선순위 순):

| 후보 | 장점 | 단점 | 예상 시나리오 |
|------|------|------|-------------|
| `gte-multilingual-base` | 코드+다국어 균형, 768차원, encoder-only(추론 빠름), sparse+dense 지원 | 비교적 신규 | **1순위 기본 후보** |
| `bge-m3` | 최고 품질, 자체 Sparse+ColBERT, 100+언어, 8192 토큰 | 2.2GB, 느림 | 품질 최우선 시 |
| `Qwen3-Embedding` | 최신, bge-m3 능가 보고 | 2.5GB, 검증 부족 | 벤치마크에서 확인 후 |

> `multilingual-e5-base`는 코드 이해 약함(△)으로 3순위에서 제외. 최근 벤치마크/가이드에서도 언급 빈도 낮음.

**Ollama 폴백 조건**: Ollama의 다국어 모델이 벤치마크에서 Recall@10 > 0.7을 달성하는 경우에만 폴백으로 허용. `nomic-embed-text`는 한국어가 △이므로 기본 폴백에서 제외.

### 임베딩 입력 형식

코드 청크의 임베딩 입력은 **raw content가 아니라 구조화된 텍스트**로 변환:

```
Query: "로그인 함수 찾아줘"
  ↓ prefix: "query: 로그인 함수 찾아줘"

Code Chunk:
  name: "signIn"
  node_type: "function"
  file_path: "src/auth/signIn.ts"
  parent: "AuthService"
  imports: ["supabase/auth", "utils/validation"]
  docstring: "Authenticates user with email and password"
  content: "async function signIn(email: string, password: string) { ... }"

  ↓ embedding input (contextualizedText 패턴, code-chunk 라이브러리 참고):
  "passage: [function] AuthService.signIn in src/auth/signIn.ts
   imports: supabase/auth, utils/validation
   Authenticates user with email and password
   async function signIn(email: string, password: string) { ... }"
```

구조화된 입력이 raw code보다 시맨틱 매칭에 유리하다 (code-chunk 라이브러리의 `contextualizedText` 패턴). scope chain(부모 클래스/모듈)과 import 경로를 포함하면 동일 이름 함수의 disambiguation에도 도움된다. E5/GTE 모델은 query/passage prefix로 비대칭 검색에 최적화되어 있다.

### 토큰 예산 & Truncation 정책

모델별 max_tokens는 **자동 감지** (config에서 override 가능):

| 모델 | max_tokens (auto) | 대략 문자수 | 비고 |
|------|:-----------------:|:----------:|------|
| multilingual-e5-base | 512 | ~2,000자 | 청킹 크기에 주의 필요 |
| gte-multilingual-base | 8,192 | ~32,000자 | 대부분의 청크가 들어감 |
| bge-m3 | 8,192 | ~32,000자 | 대부분의 청크가 들어감 |

**Truncation 규칙** (max_tokens 초과 시):
1. `content` 필드를 뒤에서부터 잘라냄 (함수 시그니처 + 상단이 가장 중요)
2. `docstring`은 항상 보존 (시맨틱 매칭의 핵심)
3. prefix (`passage: [function] signIn in ...`)는 항상 보존
4. 잘린 경우 `[truncated]` 마커 추가

**대형 청크 (>100줄) 분할과의 관계**: AST 청킹에서 이미 100줄 단위로 분할하므로, 대부분의 청크는 512 토큰 이내. 분할 후에도 초과하는 경우(예: 긴 한줄 코드)만 truncation 적용.

## 8. AST-Based Code Chunking

### TreeSitter 지원 언어

#### Phase 1 (MVP): 핵심 3개 언어

| 언어 | 청크 단위 | tree-sitter grammar |
|------|----------|-------------------|
| TypeScript/TSX | function, class, interface, type, export | tree-sitter-typescript |
| JavaScript/JSX | function, class, export | tree-sitter-javascript |
| Python | function, class, decorator | tree-sitter-python |

#### Phase 3: 확장 (10개 추가)

| 언어 | 청크 단위 | tree-sitter grammar |
|------|----------|-------------------|
| Rust | fn, impl, struct, enum, trait | tree-sitter-rust |
| Go | func, type, interface | tree-sitter-go |
| Ruby | def, class, module | tree-sitter-ruby |
| Java | method, class, interface | tree-sitter-java |
| C/C++ | function, class, struct | tree-sitter-c/cpp |
| Swift | func, class, struct, enum | tree-sitter-swift |
| Kotlin | fun, class, data class | tree-sitter-kotlin |
| SQL | CREATE, ALTER, SELECT (top-level) | tree-sitter-sql |
| CSS/SCSS | rule, mixin, keyframes | tree-sitter-css |
| HTML | (전체 파일, 소규모) | tree-sitter-html |

#### AST 파싱 실패 시 폴백

TreeSitter 파싱이 실패하거나, 지원하지 않는 언어인 경우:
1. **빈줄 기반 청킹**: 빈 줄 2개 이상으로 구분된 블록 단위로 분할
2. **크기 제한**: 블록이 4000 비공백 문자 초과 시 2000자 단위로 분할 (500자 overlap)
3. **이름 추출**: 파일명 + 줄 번호로 대체 (e.g., `utils.go:L45-L89`)

### 청크 구조

```python
@dataclass
class CodeChunk:
    id: str                    # SHA256(project_id + file_path + start_byte + end_byte)
                               # byte range 기반 → 오버로드, 익명 함수에도 안정적
    project_id: str            # Project identifier
    file_path: str             # Relative path from project root
    language: str              # "typescript", "python", etc.
    node_type: str             # "function", "class", "interface"
    name: str                  # Symbol name (e.g., "signIn")
    qualified_name: str        # "AuthService.signIn" or "auth/signIn.ts::signIn"
    content: str               # Raw source code
    embedding_input: str       # contextualizedText: scope chain + imports + docstring + content (see §7)
    imports: list[str]         # Import paths relevant to this chunk
    docstring: str | None      # Extracted docstring/JSDoc
    start_line: int            # Start line number
    end_line: int              # End line number
    start_byte: int            # Start byte offset (for stable ID)
    end_byte: int              # End byte offset
    parent_name: str | None    # Enclosing class/module name
    calls: list[str]           # Outgoing function calls (for call graph)
```

**ID 안정성**: `name` 대신 `start_byte + end_byte`를 사용하면 같은 이름의 오버로드, 익명 함수, default export에도 고유 ID가 보장된다. 파일 내용이 변경되면 해당 파일의 모든 청크를 삭제 후 재생성하므로 byte offset 변동은 문제없다.

### 청킹 규칙

1. **함수/메서드**: 개별 청크 (docstring + signature + body)
2. **클래스**: 클래스 헤더 + 각 메서드는 별도 청크
3. **Import 블록**: 파일당 하나의 청크
4. **Top-level 상수/변수**: 연속된 것들을 하나의 청크로 묶음
5. **대형 함수 (>4000 비공백 문자)**: AST 자식 노드로 재귀 분할 (cAST 알고리즘). 자식 노드가 없으면 4000자 단위로 분할 (1000자 overlap). 크기 기준은 줄 수가 아닌 **비공백 문자 수** — 줄 수는 빈 줄/주석에 의해 왜곡됨 (cAST 논문, Recall@5 +1.2~4.3 포인트)
6. **인접 소규모 청크 병합**: 비공백 문자 500자 미만의 인접 청크는 4000자를 초과하지 않는 범위에서 병합 (fragmentation 방지)
7. **문서 파일**: 헤딩 단위로 분할 (## 기준)

## 9. Indexing Pipeline

### Delta Indexing Flow

```
index_project(project_path) 호출
    │
    ▼
┌─────────────────────────┐
│ 1. File Scanner           │
│   - Walk directory        │
│   - Apply .gitignore      │
│   - Fast prefilter:       │
│     (size, mtime) 비교    │
│     → 변경 없으면 SKIP    │
│   - 변경 의심 파일만      │
│     SHA256 계산            │
│   - Compare with DB       │
│   - Result: changed,      │
│     added, deleted         │
│   - Symlink: resolve 후   │
│     project root 밖이면   │
│     SKIP (경로 탈출 방지) │
│   - Path normalization:   │
│     realpath() 적용       │
└──────────┬────────────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
┌────────┐  ┌─────────┐
│ Code   │  │ Doc     │
│ Files  │  │ Files   │
└───┬────┘  └────┬────┘
    │            │
    ▼            ▼
┌────────┐  ┌─────────┐
│ AST    │  │ Heading  │
│ Chunker│  │ Chunker  │
└───┬────┘  └────┬────┘
    │            │
    └──────┬─────┘
           │
           ▼
┌─────────────────────┐
│ 3. Embedding Gen     │
│   - Batch processing │
│   - "passage: ..."   │
│   - selected model   │
└──────────┬──────────┘
           │
           ▼
┌──────────────────────────┐
│ 4. Index Update           │
│   (per changed file)      │
│   a. DELETE all old chunks│
│      for this file from:  │
│      SQLite, Tantivy,     │
│      USearch, call_edges  │
│   b. INSERT new chunks    │
│   c. Update file_hash     │
│      (last, for crash     │
│       recovery — see §13) │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│ 5. Cleanup                │
│   - Remove deleted file   │
│     chunks (CASCADE)      │
│   - Compact indices       │
│   - Verify consistency:   │
│     chunk count in SQLite │
│     == entries in Tantivy │
│     == vectors in USearch │
│     (mismatch → log warn  │
│      + schedule rebuild)  │
└──────────────────────────┘
```

### 성능 목표

| 메트릭 | 목표 |
|--------|------|
| 초기 인덱싱 (10K 파일) | < 5분 |
| Delta 인덱싱 (10 파일 변경) | < 5초 |
| 검색 응답 시간 | < 500ms |
| 메모리 사용량 (데몬 상주) | < 500MB |
| 디스크 사용량 (프로젝트당) | < 200MB |

## 10. MCP Tool Specifications

### 10.1 `hybrid_search`

**설명**: BM25 + Vector를 결합한 하이브리드 검색

```json
{
  "name": "hybrid_search",
  "description": "Search code and documentation using hybrid BM25 + semantic vector search with cross-language support (Korean ↔ English)",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Search query (Korean or English)"
      },
      "project": {
        "type": "string",
        "description": "Project name or path. Omit for all projects."
      },
      "limit": {
        "type": "integer",
        "default": 10,
        "minimum": 1,
        "maximum": 50,
        "description": "Max results to return"
      },
      "file_pattern": {
        "type": "string",
        "description": "Glob pattern to filter files (e.g., '*.ts')"
      },
      "node_types": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Filter by chunk types: function, class, interface, etc."
      },
      "bm25_weight": {
        "type": "number",
        "default": 0.5,
        "minimum": 0.0,
        "maximum": 1.0,
        "description": "Weight for BM25 in RRF (0-1). Vector weight = 1 - bm25_weight. If explicitly set, overrides auto query classification."
      }
    },
    "required": ["query"]
  }
}
```

**응답 형식**:
```json
{
  "results": [
    {
      "chunk_id": "a3f8c2...",
      "rrf_score": 0.0142,
      "bm25_rank": 3,
      "vector_rank": 1,
      "file_path": "src/auth/signIn.ts",
      "project": "breeze",
      "name": "signIn",
      "qualified_name": "auth/signIn.ts::signIn",
      "node_type": "function",
      "start_line": 15,
      "end_line": 42,
      "content": "async function signIn(email: string, password: string) { ... }",
      "snippet": "...authenticates user with email and password..."
    }
  ],
  "query_type": "KOREAN_NL",
  "effective_bm25_weight": 0.15,
  "query_time_ms": 45,
  "total_chunks_searched": 12500
}
```

**Score 정의**:
- `rrf_score`: Raw RRF 점수 = Σ(weight / (k + rank)). 정규화하지 않음 — 절대값보다 상대 순위가 의미있음
- `bm25_rank` / `vector_rank`: 해당 엔진에서의 순위. 한쪽에만 등장하면 다른 쪽은 `null`
- `chunk_id`: trace_callers/callees에 전달 가능한 고유 식별자
```

### 10.2 `semantic_search`

**설명**: 순수 벡터 검색 (크로스 언어 매칭에 특화)

```json
{
  "name": "semantic_search",
  "description": "Pure semantic vector search. Best for cross-language queries (e.g., Korean query → English code)",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string" },
      "project": { "type": "string" },
      "limit": { "type": "integer", "default": 10, "minimum": 1, "maximum": 50 },
      "file_pattern": {
        "type": "string",
        "description": "Glob pattern to filter files (e.g., '*.ts')"
      },
      "node_types": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Filter by chunk types: function, class, interface, etc."
      },
      "similarity_threshold": {
        "type": "number",
        "default": 0.5,
        "minimum": 0.0,
        "maximum": 1.0,
        "description": "Minimum cosine similarity (0-1)"
      }
    },
    "required": ["query"]
  }
}
```

### 10.3 `trace_callers`

**설명**: 특정 함수를 호출하는 모든 함수 추적

```json
{
  "name": "trace_callers",
  "description": "Find all functions that call the given function (reverse call graph). Provide chunk_id (precise) or symbol (name-based). If both given, chunk_id takes precedence.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "symbol": {
        "type": "string",
        "description": "Function/method name or qualified name (e.g., 'signIn' or 'AuthService.signIn')"
      },
      "chunk_id": {
        "type": "string",
        "description": "Chunk ID from a prior search result. More precise than symbol name. Takes precedence over symbol."
      },
      "project": { "type": "string" },
      "depth": {
        "type": "integer",
        "default": 2,
        "minimum": 1,
        "maximum": 10,
        "description": "Max depth of call graph traversal"
      },
      "min_confidence": {
        "type": "string",
        "enum": ["low", "medium", "high"],
        "default": "medium",
        "description": "Minimum resolution confidence for call edges"
      }
    }
  }
}
```

**입력 규칙**: `symbol` 또는 `chunk_id` 중 최소 하나 필수. 둘 다 없으면 에러. 둘 다 전달되면 `chunk_id` 우선.

**Traversal 동작**:
- **순환 참조 방지**: visited set으로 이미 방문한 chunk는 skip. A→B→A 순환 시 A에서 중단.
- **결과 상한**: depth와 무관하게 최대 **100개 노드** 반환. 초과 시 truncated 플래그 설정.
- **부분 결과**: depth 3 요청 중 depth 2에서 unresolved edge만 남으면, depth 2까지의 결과를 `partial: true`로 반환.

### 10.4 `trace_callees`

**설명**: 특정 함수가 호출하는 모든 함수 추적

```json
{
  "name": "trace_callees",
  "description": "Find all functions called by the given function (forward call graph). Provide chunk_id (precise) or symbol (name-based). If both given, chunk_id takes precedence.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "symbol": {
        "type": "string",
        "description": "Function/method name or qualified name"
      },
      "chunk_id": {
        "type": "string",
        "description": "Chunk ID from a prior search result"
      },
      "project": { "type": "string" },
      "depth": {
        "type": "integer",
        "default": 2,
        "minimum": 1,
        "maximum": 10
      },
      "min_confidence": {
        "type": "string",
        "enum": ["low", "medium", "high"],
        "default": "medium"
      }
    }
  }
}
```

Traversal 동작은 `trace_callers`와 동일 (visited set, 100노드 상한, partial 결과).

### Trace 응답 형식 (trace_callers / trace_callees 공통)

```json
{
  "root": {
    "chunk_id": "a3f8c2...",
    "name": "signIn",
    "qualified_name": "auth/signIn.ts::signIn",
    "file_path": "src/auth/signIn.ts",
    "start_line": 15
  },
  "edges": [
    {
      "from_chunk_id": "b7d1e4...",
      "from_name": "handleLogin",
      "from_file": "src/pages/login.tsx",
      "to_chunk_id": "a3f8c2...",
      "to_name": "signIn",
      "confidence": "high",
      "depth": 1
    }
  ],
  "total_nodes": 8,
  "max_depth_reached": 2,
  "truncated": false,
  "partial": false
}
```

### 10.5 `index_project`

**설명**: 프로젝트 인덱싱 (초기 또는 delta)

```json
{
  "name": "index_project",
  "description": "Index or re-index a project. Uses delta indexing if index already exists.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project_path": {
        "type": "string",
        "description": "Absolute path to project root"
      },
      "project_name": {
        "type": "string",
        "description": "Human-readable project name"
      },
      "force": {
        "type": "boolean",
        "default": false,
        "description": "Force full re-index (ignore delta)"
      }
    },
    "required": ["project_path"]
  }
}
```

### 10.6 `search_symbols`

**설명**: 심볼 이름으로 빠른 검색 (fuzzy match)

```json
{
  "name": "search_symbols",
  "description": "Search for symbols (functions, classes, types) by name with fuzzy matching",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Symbol name or pattern"
      },
      "project": { "type": "string" },
      "node_types": {
        "type": "array",
        "items": { "type": "string" }
      }
    },
    "required": ["name"]
  }
}
```

### 10.7 `index_status`

**설명**: 인덱스 상태 조회

```json
{
  "name": "index_status",
  "description": "Show indexing status for one or all projects: file count, chunk count, last indexed time, index health",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project": {
        "type": "string",
        "description": "Project name. Omit for all projects."
      }
    }
  }
}
```

**index_status 응답 형식**:
```json
{
  "projects": [
    {
      "name": "breeze",
      "path": "/Users/ian/project/claude_project/breeze",
      "last_indexed_at": "2026-04-13T10:30:00Z",
      "file_count": 342,
      "chunk_count": 2150,
      "index_healthy": true,
      "embedding_model": "gte-multilingual-base",
      "index_version": 1
    }
  ]
}
```

### 10.8 `list_projects`

**설명**: 등록된 프로젝트 목록 조회

```json
{
  "name": "list_projects",
  "description": "List all registered projects with their paths and index status",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

### 10.9 `remove_project`

**설명**: 프로젝트 등록 해제 및 인덱스 삭제

```json
{
  "name": "remove_project",
  "description": "Unregister a project and delete its index data. Does not delete source files.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "project": {
        "type": "string",
        "description": "Project name to remove"
      },
      "keep_index": {
        "type": "boolean",
        "default": false,
        "description": "If true, unregister but keep index data on disk"
      }
    },
    "required": ["project"]
  }
}
```

## 11. RRF (Reciprocal Rank Fusion) Algorithm

```python
def reciprocal_rank_fusion(
    bm25_results: list[SearchResult],
    vector_results: list[SearchResult],
    k: int = 60,
    bm25_weight: float = 0.5,
) -> list[SearchResult]:
    """
    RRF Score = Σ (weight / (k + rank))

    k=60 is standard (from the original Cormack et al. paper).
    Higher k → more uniform distribution across ranks.
    """
    scores: dict[str, float] = {}
    results_by_id: dict[str, SearchResult] = {}
    vector_weight = 1.0 - bm25_weight

    for rank, result in enumerate(bm25_results, start=1):
        scores[result.id] = scores.get(result.id, 0) + bm25_weight / (k + rank)
        results_by_id[result.id] = result

    for rank, result in enumerate(vector_results, start=1):
        scores[result.id] = scores.get(result.id, 0) + vector_weight / (k + rank)
        results_by_id[result.id] = result

    # Sort by fused score descending
    ranked = sorted(results_by_id.values(), key=lambda r: scores[r.id], reverse=True)
    return ranked
```

**Retrieval Depth**: 각 엔진에서 `limit * 3`개를 후보로 가져온 뒤 RRF로 합산. 최종 `limit`개 반환.

### 쿼리 분류 & 가중치 전략

단순 한글 감지가 아닌, **3단계 쿼리 분류기**로 가중치를 결정:

```
classify_query(query) → QueryType
  1. EXACT_SYMBOL: camelCase/snake_case 패턴 매칭 (regex)
     → "signIn", "tuition_fees", "createUser"
  2. KOREAN_NL: 한글 비율 > 50%
     → "로그인 함수 찾아줘", "학원비 계산"
  3. ENGLISH_NL: 나머지
     → "find the login function", "how does auth work"
```

| 쿼리 타입 | bm25_weight | vector_weight | 이유 |
|----------|:-----------:|:-------------:|------|
| EXACT_SYMBOL | 0.8 | 0.2 | BM25가 정확한 심볼 이름에 강함 |
| KOREAN_NL | 0.15 | 0.85 | BM25는 한국어→영어 매칭 불가 |
| ENGLISH_NL | 0.4 | 0.6 | 의미적 매칭이 약간 유리 |

**사용자 override 우선순위**: `bm25_weight` 파라미터가 명시적으로 전달되면 자동 분류를 무시하고 사용자 값을 사용한다.

**혼합 쿼리** ("signIn 함수가 뭐하는지"): 한글이 포함되지만 심볼 이름도 있는 경우 → EXACT_SYMBOL과 KOREAN_NL의 중간값 (bm25=0.4, vector=0.6) 적용.

## 12. Call Graph

### 추출 방법

TreeSitter AST에서 함수 호출 노드를 추출:

```
TypeScript: call_expression → function 이름 추출
Python:     call → function 이름 추출
Rust:       call_expression, method_call_expression
```

### 저장 구조 (SQLite, store.db 내)

```sql
CREATE TABLE call_edges (
    caller_chunk_id TEXT NOT NULL,
    callee_name TEXT NOT NULL,           -- 심볼 이름
    callee_qualified_name TEXT,          -- "AuthService.signIn" (namespace 포함)
    callee_chunk_id TEXT,                -- Resolved chunk ID (nullable)
    callee_module TEXT,                  -- import 경로 (e.g., "./auth/signIn")
    project_id TEXT NOT NULL,
    confidence TEXT DEFAULT 'low',       -- 'high' | 'medium' | 'low'
    FOREIGN KEY (caller_chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);

CREATE INDEX idx_callee_name ON call_edges(callee_name);
CREATE INDEX idx_callee_qualified ON call_edges(callee_qualified_name);
CREATE INDEX idx_caller ON call_edges(caller_chunk_id);
```

### Resolution 전략

1. **High confidence**: import 경로 + 심볼 이름으로 정확한 chunk 매칭
2. **Medium confidence**: 같은 프로젝트 내 qualified_name 매칭 (클래스명.메서드명)
3. **Low confidence**: 이름만으로 매칭 (common names 필터링)

**Common name 필터**: `run`, `init`, `get`, `set`, `render`, `handle`, `process`, `validate`, `update`, `create` 같은 흔한 이름은 low confidence로 태깅하고, trace 결과에서 confidence 표시.

### 현재 한계 (Phase 7에서 해결 예정)

| 한계 | 원인 | Phase 7 해결 |
|------|------|:----------:|
| Resolution rate ~7.5% | `callee_module`이 항상 NULL (import-call 미연결) | Step 1: Import-Call 바인딩 |
| High confidence 0% | module 정보 없이 이름만으로 매칭 | Step 1+2 |
| `this.method()` 미해결 | receiver class 미추적 | Step 3: Receiver 추적 |
| COMMON_NAMES 일괄 차단 | context 없이 이름만으로 판별 불가 | Step 4: 정책 완화 |
| Cross-file resolve best-effort | import path → file 역인덱스 없음 | Step 2: 역인덱스 |

## 13. Storage Design

### 단일 SQLite (store.db per project)

`meta.db`와 `chunks.db`를 하나의 `store.db`로 통합. SQLite의 트랜잭션으로 일관성 보장.

```sql
-- 파일 메타데이터 (delta 인덱싱 기준)
CREATE TABLE files (
    id TEXT PRIMARY KEY,           -- SHA256(project_id + relative_path)
    project_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,        -- SHA256 of content
    file_size INTEGER,
    file_mtime TEXT,                -- mtime for fast prefilter
    language TEXT,
    last_modified TEXT,
    chunk_count INTEGER DEFAULT 0,
    UNIQUE(project_id, relative_path)
);

-- 청크 데이터
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT,
    qualified_name TEXT,            -- "AuthService.signIn"
    node_type TEXT,
    start_line INTEGER,
    end_line INTEGER,
    start_byte INTEGER,
    end_byte INTEGER,
    content TEXT,
    embedding_input TEXT,           -- structured text used for embedding
    docstring TEXT,
    parent_name TEXT,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE INDEX idx_chunks_project ON chunks(project_id);
CREATE INDEX idx_chunks_name ON chunks(name);
CREATE INDEX idx_chunks_qualified ON chunks(qualified_name);
CREATE INDEX idx_chunks_type ON chunks(node_type);
```

### 글로벌 프로젝트 레지스트리 (`~/.hybrid-search/global/project_registry.db`)

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    last_indexed_at TEXT,
    file_count INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    index_version INTEGER DEFAULT 1
);
```

**Cross-project 검색** (`project` 파라미터 생략 시):

2단계 RRF가 아닌, **단일 RRF**로 통합:
1. 각 프로젝트에서 BM25 top-(limit*3)과 Vector top-(limit*3)을 각각 수집
2. 전 프로젝트의 BM25 결과를 하나의 리스트로 merge-sort (**BM25 rank** 기준, score 아님 — 프로젝트별 Tantivy 인덱스의 raw score는 서로 비교 불가)
3. 전 프로젝트의 Vector 결과를 하나의 리스트로 merge-sort (cosine similarity 기준 — 동일 임베딩 모델이므로 프로젝트 간 비교 가능)
4. 통합된 두 리스트에 RRF 적용 → 최종 limit개 반환

**BM25 cross-project merge 전략**: 각 프로젝트에서 rank 1~N을 가져온 뒤, 라운드-로빈으로 interleave한다 (프로젝트 A rank 1, B rank 1, A rank 2, B rank 2, ...). 이 방식은 프로젝트 크기 편향을 방지하고, BM25 score의 프로젝트 간 비교 불가 문제를 우회한다.

**타임아웃**: cross-project 검색 시 프로젝트당 최대 2초. 타임아웃된 프로젝트는 결과에서 제외하고 응답에 `skipped_projects` 목록을 포함한다.

### 인덱스 파일

- **Tantivy Index** (`tantivy/`): 각 청크의 `content + name + qualified_name + docstring`을 인덱싱
- **USearch HNSW** (`vectors/`): 각 청크의 embedding vector 저장
  - Metric: cosine similarity
  - M=16, ef_construction=200

### 동시성 모델

| 상황 | 동작 |
|------|------|
| 검색 중 인덱싱 | SQLite는 WAL 모드로 읽기/쓰기 동시 가능. Tantivy/USearch는 인덱싱 완료 후 reader reload |
| 동시 인덱싱 | 프로젝트별 file lock (`store.db.lock`). 같은 프로젝트의 동시 인덱싱 차단 |
| 크래시 복구 | SQLite 트랜잭션이 metadata 일관성 보장. Tantivy/USearch 인덱스가 SQLite와 불일치하면 해당 프로젝트에 `force` 재인덱싱 트리거 |

### Multi-store 업데이트 순서 (원자성)

```
1. SQLite: BEGIN TRANSACTION
2. SQLite: DELETE stale chunks → INSERT new chunks
3. Tantivy: delete stale docs → add new docs → commit
4. USearch: remove stale vectors → add new vectors → save
5. SQLite: UPDATE files SET file_hash = new_hash
6. SQLite: COMMIT
   ─── 실패 시 ───
   SQLite: ROLLBACK
   Tantivy/USearch: reload from last committed state
```

`store.db`의 `files.file_hash`가 마지막에 업데이트되므로, 중간 실패 시 다음 delta 인덱싱에서 해당 파일을 "변경됨"으로 감지하여 자동 복구.

**실패 시나리오별 복구**:

| 실패 지점 | 상태 | 복구 |
|----------|------|------|
| Step 2 (SQLite INSERT) 실패 | SQLite ROLLBACK, 외부 인덱스 미변경 | 자동: 다음 delta에서 재시도 |
| Step 3 (Tantivy commit) 실패 | SQLite ROLLBACK, Tantivy uncommitted | 자동: Tantivy는 uncommitted 변경 폐기 |
| Step 4 (USearch save) 실패 | SQLite ROLLBACK, Tantivy committed, USearch 미변경 | **불일치 발생**: consistency check(§9 Step 5)에서 감지 → 해당 프로젝트 force rebuild 스케줄 |
| Step 6 (SQLite COMMIT) 실패 | SQLite ROLLBACK, Tantivy+USearch committed | **불일치 발생**: file_hash 미갱신 → 다음 delta에서 해당 파일 재처리. 중복 chunk는 ID 기반 upsert로 무해 |
| 프로세스 크래시 | 불확정 | 서버 재시작 시 consistency check 실행 → 불일치 감지 시 force rebuild |

### 인덱스 버전 관리 & 마이그레이션

`index_meta`는 **per-project `store.db`** 내에 저장한다 (프로젝트별 독립적 마이그레이션 지원):

```sql
-- store.db (per project)
CREATE TABLE index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 저장 항목: schema_version, embedding_model, embedding_dim, chunk_id_format
```

**자동 rebuild 트리거**: 서버 시작 시 각 프로젝트의 `store.db → index_meta`를 확인하고, 아래 조건 중 하나라도 해당하면 `force` 재인덱싱을 스케줄:

| 변경 감지 | 동작 |
|----------|------|
| `embedding_model` 변경 | 전체 rebuild (벡터 호환 불가) |
| `embedding_dim` 변경 | 전체 rebuild |
| `schema_version` 변경 | SQLite migration 시도 → 실패 시 rebuild |
| `chunk_id_format` 변경 | 전체 rebuild |

**마이그레이션 순서**: 새 인덱스를 `{project_hash}.new/`에 빌드 → 완료 후 atomic rename → 구 인덱스 삭제. 마이그레이션 중에도 구 인덱스로 검색 가능 (무중단).

## 14. Configuration

### `~/.hybrid-search/config.toml`

```toml
[general]
data_dir = "~/.hybrid-search"
log_level = "info"

[embedding]
model = "Qwen/Qwen3-Embedding-0.6B"         # Benchmark winner: KR→EN R@1=0.70, MRR=0.902
                                            # e5-base: R@1=0.60, gte: custom code bug, bge-m3: too heavy
model_revision = ""                         # REQUIRED for download. Pinned HuggingFace commit hash.
                                            # Server refuses to download without pinned revision.
model_sha256 = ""                           # REQUIRED for download. SHA256 of ONNX file.
                                            # Mismatch → download rejected, server won't start.
model_path = ""                             # Optional: local ONNX path (skip download + skip verification)
backend = "onnx"                            # "onnx" | "ollama"
ollama_model = ""                           # Set only if Ollama model passes benchmark
batch_size = 32
max_tokens = 0                              # 0 = auto-detect from model (e5: 512, gte/bge-m3: 8192)
                                            # Override only if needed for memory constraints
device = "cpu"                              # "cpu" | "mps" (Apple Silicon)

[search]
default_limit = 10
rrf_k = 60
query_classifier = true                     # 3-stage: EXACT_SYMBOL / KOREAN_NL / ENGLISH_NL
default_bm25_weight = 0.5

[indexing]
exclude_patterns = [
    "node_modules", ".git", "__pycache__", ".next",
    "dist", "build", ".venv", "*.lock"
]
max_file_size_kb = 512                      # Skip files larger than this
supported_extensions = [
    ".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go",
    ".rb", ".java", ".c", ".cpp", ".h", ".hpp",
    ".swift", ".kt", ".sql", ".css", ".scss",
    ".md", ".json", ".yaml", ".yml", ".toml"
]

[[projects]]
name = "breeze"
path = "/Users/ian/project/claude_project/breeze"

[[projects]]
name = "valuein-homepage"
path = "/Users/ian/project/claude_project/valuein_homepage"

# ... more projects
```

## 15. MindVault 공존 전략

### 역할 분리

| 구분 | MindVault | Hybrid Search MCP |
|------|----------|-------------------|
| 역할 | 자동 컨텍스트 (conversation hook) | 명시적 검색 (MCP tool) |
| 트리거 | 매 프롬프트마다 자동 실행 | 사용자/Claude가 tool 호출 시 |
| 검색 방식 | BM25 + Wiki + Graph | BM25 + Vector (RRF) |
| 강점 | 그래프 관계, 위키 구조 | 의미적 검색, 크로스 언어 |
| 포트/통신 | 데몬 (별도 포트) | stdio (MCP) |

### 충돌 분석

| 충돌 유형 | 심각도 | 대응 |
|----------|:------:|------|
| 통신 채널 | 없음 | 서로 다른 채널 (데몬 vs stdio) |
| 인덱싱 I/O | 낮음 | 같은 파일을 동시에 읽을 수 있으나 쓰기 경합 없음 (각자 별도 인덱스 디렉토리) |
| CPU/메모리 | 중간 | 동시 인덱싱 시 CPU 경합 가능. Hybrid Search 인덱싱은 MindVault 데몬과 다른 시점에 실행 |
| 검색 결과 중복 | 중간 | Claude가 MindVault 자동 컨텍스트로 충분하면 hybrid_search를 호출하지 않음 |

### Claude의 도구 선택 가이드

MCP 서버의 `hybrid_search` tool description에 아래 가이드를 포함:

> "Use this tool when: (1) the automatic MindVault context is insufficient, (2) you need cross-language search (Korean ↔ English), (3) you need semantic/conceptual matching beyond exact keywords, or (4) you need call graph tracing."

### 사용 시나리오

- MindVault가 자동으로 관련 컨텍스트 제공 → 충분하면 추가 검색 불필요
- "로그인 관련 코드 찾아줘" → `hybrid_search("로그인")` — MindVault BM25로는 불가
- "signIn 함수를 누가 호출하는지" → `trace_callers("signIn")` — MindVault에 없는 기능
- 영어 정확 키워드 검색 → MindVault BM25로 충분, hybrid_search 불필요

## 16. Implementation Phases

### Phase 1~4: 핵심 검색 파이프라인 ✅ 완료

> 상세: HANDOFF.md 참조

- Phase 1: MVP (semantic search, AST chunking, 13개 MCP 도구)
- Phase 2: Hybrid BM25+Vector (RRF fusion, 쿼리 분류)
- Phase 3a: Call Graph (3단계 confidence, trace 도구)
- Phase 3b: 14개 AST 언어 지원
- Phase 4: Polish (ONNX/Ollama/MPS, 크래시 복구, 핫 리로드)

### Phase 5: Reactive Wiki Layer ✅ 완료

- `compile_to_wiki` / `lookup_wiki` / `check_wiki_staleness` / `refresh_wiki_page` 4개 MCP 도구
- WikiStore (LRU eviction, 파일 해시 스냅샷 기반 staleness)
- DB: `wiki_pages` + `wiki_dependencies` 테이블

### Phase 6: Background Indexing + Wiki Infra ✅ 완료

- CLI (`hybrid_search.cli`): reindex, status, stale, install-hook, sync-wiki
- post-commit hook → delta reindex → wiki auto-sync (확정적 파이프라인)
- 3개 스킬: `/bootstrap-wiki`, `/save-wiki`, `/search`
- CWD 프로젝트 부스트 (BM25 2:1 interleave + cosine +0.05)

### Phase 7: Call Graph Resolution 90%+ (미구현)

> **목표**: resolution rate를 7.5% → 90%+ 로 올려서 CodeWiki식 자동 wiki 생성의 전제 조건을 충족

#### 현재 상태와 근본 원인

| Confidence | 조건 | 현재 발동률 |
|:----------:|------|:----------:|
| High | callee_module + callee_name 모두 매칭 | **0%** (callee_module이 항상 NULL) |
| Medium | 이름이 유일하거나 같은 파일 내 매칭 | ~10% |
| Low | 이름만 매칭 + COMMON_NAMES 아닌 경우 | ~5% |
| 미해결 | 매칭 실패 또는 COMMON_NAMES | ~85% |

**근본 원인**: `_extract_imports()`가 import를 추출하고 `_extract_call_name()`이 call을 추출하지만, **두 정보를 연결하지 않는다**. call edge에 `callee_module=NULL`이 들어가서 High confidence가 절대 발동하지 않음.

#### Step 1: Import-Call 바인딩 (예상 7.5% → ~55%)

**가장 큰 ROI. import 정보는 이미 추출하고 있는데 call과 연결만 안 하고 있을 뿐.**

```python
# 현재: call만 추출
calls = ["login", "charge"]

# 개선: import와 연결
calls = [
    {"name": "login", "module": "src/auth"},      # from src.auth import login
    {"name": "charge", "module": "src/billing"},   # from src.billing import charge
]
```

구현:
- `ast_chunker.py`: `_extract_imports()`의 반환값을 `dict[str, str]` (name → module) 으로 변경
- `ast_chunker.py`: chunk 생성 시 `imports_map`을 chunk metadata에 포함
- `callgraph.py`: resolve 시 `callee_module`과 `imports_map`을 매칭하여 High confidence 부여
- 코드 변경량: ast_chunker.py ~50줄, callgraph.py ~20줄

#### Step 2: Module Path → File 역인덱스 (누적 ~70-75%)

```python
# 현재: qualified_name으로만 검색
"src/auth.py::login" → chunk_id_123

# 개선: import path로도 검색
"src/auth" → ["src/auth.py::login", "src/auth.py::logout", ...]
"./auth"   → (같은 결과, 상대경로 해석)
```

구현:
- `db.py`: `files` 테이블에서 import path → file_id 매핑 쿼리 추가
- `callgraph.py`: module path로 파일을 찾고, 해당 파일의 chunk 중 이름 매칭
- 코드 변경량: db.py ~30줄, callgraph.py ~20줄

#### Step 3: 메서드 Receiver 추적 (누적 ~85-90%)

```python
# 현재: this.validate() → callee_name="validate" (COMMON_NAME, 해결 불가)
# 개선: this.validate() → callee_name="validate", parent_class="AuthService"
#       → AuthService.validate()로 매칭
```

구현:
- `ast_chunker.py`: `this`/`self`의 containing class를 추적하여 call에 parent_class 추가
- `callgraph.py`: parent_class + callee_name으로 qualified_name 매칭 (`parent_name` 필드 활용)
- 코드 변경량: ast_chunker.py ~30줄, callgraph.py ~15줄

#### Step 4: COMMON_NAMES 정책 완화 (누적 ~90-95%)

Step 1-3이 되면 `validate`, `render` 같은 이름도 module+class 정보로 구별 가능.

- `callgraph.py`: COMMON_NAMES를 "무조건 low"에서 "context 없을 때만 low"로 변경
- 코드 변경량: callgraph.py ~10줄

#### 검증 계획

```bash
# Phase 7 구현 전후 비교
python -m hybrid_search.cli reindex --cwd /path/to/project --force
python -m hybrid_search.cli call-graph-stats --cwd /path/to/project
# 출력: total_edges, resolved, high, medium, low, unresolved, resolution_rate
```

valuein-homepage(1,757파일)에서 resolution rate 90%+ 달성 시 Phase 8 진행.

### Phase 8: CodeWiki 자동 Wiki 생성 (미구현, Phase 7 전제)

> **목표**: Call graph 위상정렬로 프로젝트의 모든 도메인 기능을 빠짐없이 자동 식별하여 wiki를 생성. 디렉토리 스캔이 아닌 의존성 그래프 기반.
> **근거**: CodeWiki (ACL 2026) — AST + 의존성 그래프 → 위상정렬 → 계층적 모듈 분해 → 리프부터 상향식 문서 생성

#### 왜 Call Graph 기반이 디렉토리 스캔보다 나은가

| 문제 | 디렉토리 스캔 | Call Graph 위상정렬 |
|------|:------------:|:------------------:|
| `services/makeup-service.ts` (보강) | services/ 폴더 1개로 뭉뚱그려짐 | 독립 모듈로 식별 |
| `services/textbook-grading-service.ts` (교재채점) | 같은 services/ | 독립 모듈로 식별 |
| "상담관리"가 여러 파일에 걸쳐있음 | 빠뜨리기 쉬움 | call graph에서 연결된 파일 클러스터로 묶임 |
| "입학테스트"가 services/ + app/ + hooks/에 분산 | 각각 별도 페이지 | 하나의 기능 모듈로 인식 |

#### 8a: 모듈 트리 자동 생성 (`generate-wiki-plan`)

CodeWiki 논문의 3단계 파이프라인을 CLI에 구현:

```
Step 1: 의존성 그래프 구성
  - call_edges + imports로 방향성 그래프 G=(V,E) 구축
  - V = chunks (함수/클래스), E = calls/imports

Step 2: 엔트리 포인트 식별 + 위상정렬
  - zero-in-degree 노드 = 최상위 엔트리 (페이지 라우트, API 핸들러 등)
  - 위상정렬로 처리 순서 결정
  - 연결된 컴포넌트(connected component) = 1개 기능 모듈

Step 3: 모듈 트리 → 페이지 목록
  - 각 연결 컴포넌트를 1개 wiki 페이지로 매핑
  - 복잡도(chunk 수)가 임계값 초과 시 하위 분해
  - 고립 노드(call edge 없음)는 디렉토리 기반 폴백
```

```bash
python -m hybrid_search.cli generate-wiki-plan --cwd /path/to/project
# 출력:
# Module Tree (21 modules):
# 1. auth-system (5 files, 12 chunks) — app/(auth)/, lib/auth/, proxy.ts
# 2. tuition-billing (8 files, 23 chunks) — services/tuition-*, hooks/use-tuition*
# 3. makeup-attendance (3 files, 8 chunks) — services/makeup-service.ts, hooks/use-makeup*
# 4. textbook-grading (4 files, 11 chunks) — services/textbook-grading-*, hooks/use-textbook*
# 5. consultation (6 files, 15 chunks) — services/consultation-*, hooks/use-consultation*
# ...
```

#### 8b: 리프 → 부모 상향식 Wiki 생성

CodeWiki의 Hierarchical Synthesis:

```
1. 리프 모듈: 병렬 Agent가 직접 코드 읽고 wiki 페이지 작성
2. 부모 모듈: 자식 wiki를 합성하여 아키텍처 개요 생성
3. architecture.md: 모든 모듈 wiki를 합성하여 최상위 개요
```

#### 8c: 검수 자동화

```bash
python -m hybrid_search.cli verify-wiki --cwd /path/to/project
# 출력:
# Module Tree: 21 modules
# Wiki pages: 21/21 (100%)
# Coverage: 1,423/1,757 files covered (81%)
# Uncovered: 334 files (node_modules 제외 후 실질 12 files)
# Dependencies tracked: 156 deps
```

#### 8d: 전체 파이프라인 (완전 자동)

```
git commit
  └→ post-commit hook
     └→ delta reindex
        └→ call graph re-resolve
           └→ module tree 변경 감지
              └→ 변경된 모듈의 wiki만 stale 마킹
                 └→ 다음 대화에서 lazy recompile

/bootstrap-wiki (최초 1회)
  └→ generate-wiki-plan (call graph 기반)
     └→ 사용자 확인
        └→ 병렬 Agent 생성
           └→ sync-wiki (DB 동기화)
              └→ verify-wiki (검수)
```

## 17. Dependencies (Python)

```toml
[project]
name = "hybrid-search-mcp"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.0",              # MCP SDK
    "tantivy>=0.22",               # BM25 (Rust-backed)
    "usearch>=2.0",                # HNSW vector index
    "onnxruntime>=1.17",           # ONNX model inference (optional backend)
    "sentence-transformers>=5.0",  # Primary embedding backend
    "transformers>=4.40",          # Tokenizer
    "tree-sitter>=0.23",           # AST parsing
    "tree-sitter-python>=0.23",    # Python grammar
    "tree-sitter-javascript>=0.23",# JavaScript grammar
    "tree-sitter-typescript>=0.23",# TypeScript grammar
    # Phase 3b: 10 additional languages
    "tree-sitter-rust>=0.24",
    "tree-sitter-go>=0.25",
    "tree-sitter-ruby>=0.23",
    "tree-sitter-java>=0.23",
    "tree-sitter-c>=0.24",
    "tree-sitter-cpp>=0.23",
    "tree-sitter-swift>=0.0.1",
    "tree-sitter-kotlin>=1.0",
    "tree-sitter-css>=0.25",
    "tree-sitter-html>=0.23",
    "tree-sitter-sql>=0.3",
    "pathspec>=0.12",              # .gitignore parsing
]
# Note: tree-sitter-languages는 Python 3.13 미지원으로 개별 grammar 패키지 사용

[project.optional-dependencies]
gpu = ["onnxruntime-silicon>=1.17"]  # Apple MPS support
dev = ["pytest>=8.0", "ruff>=0.4"]
```

## 18. Open Questions

### Resolved
- ~~bge-m3 vs multilingual-e5~~ → 벤치마크 완료: Qwen3-0.6B(R@1=0.83) > e5-base(0.77) > e5-small(0.77). 운영은 e5-small(속도), 품질 필요시 Qwen3 전환 (§7)
- ~~Call graph 정확도~~ → confidence 레벨 + common name 필터로 noise 관리 (§12)
- ~~meta.db/chunks.db 불일치~~ → store.db 단일 통합 (§13)
- ~~callgraph/ 디렉토리 vs store.db~~ → callgraph/ 제거, call_edges는 store.db 내 (§5, §12)
- ~~인덱스 마이그레이션~~ → per-project index_meta + 자동 rebuild 트리거 (§13)
- ~~모델 다운로드 보안~~ → pinned revision + SHA256 필수, 빈칸 시 서버 거부 (§4, §14)
- ~~Traversal 순환 참조~~ → visited set + 100노드 상한 + partial 결과 (§10.3)
- ~~토큰 예산~~ → 모델별 auto-detect + truncation 정책 (§7, §14)
- ~~Cross-project BM25 score 비교 불가~~ → rank 기반 interleave + 프로젝트별 타임아웃 (§13)
- ~~Multi-store 실패 복구~~ → 시나리오별 복구 전략 테이블 (§13)
- ~~trace 도구 스키마 모순~~ → oneOf 제거, 우선순위 기반 + description에 명시 (§10.3)
- ~~청크 크기 기준~~ → 줄 수 → 비공백 문자 4000자 (cAST 논문 근거) (§8)
- ~~도구 응답 스키마 부재~~ → trace, index_status 응답 형식 추가 (§10)

### Open
1. ~~**bge-m3 Sparse Vector**~~ → bge-m3는 CPU에서 너무 느려서 실사용 불가. e5-small + Tantivy BM25 조합으로 확정
2. ~~**인덱스 크기**~~ → valuein-homepage 1,757파일 = 9,559 chunks (229초 CPU). breeze 대비 11.3x 파일, 29.3x 청크. 대규모 프로젝트에서도 에러 0, 5분 이내 인덱싱 완료
3. ~~**MPS 가속 효과**~~ → M3 Pro에서 sentence-transformers + MPS: 192.9초 vs CPU 229.3초 = **19% 가속**. e5-small(33M)은 경량 모델이라 GPU 전송 오버헤드 대비 폭 제한적. Qwen3-0.6B에서는 더 큰 차이 예상
4. **MindVault 장기 통합**: Hybrid Search가 안정화되면 MindVault의 BM25를 대체하고, MindVault는 Graph/Wiki에 집중하는 구조로 갈 수 있는가?
5. **임베딩 입력 최적화**: 구조화된 입력(§7) vs raw code의 실제 retrieval 품질 차이는? → 벤치마크에서 A/B 비교
6. **INSERT OR REPLACE + FK CASCADE**: SQLite에서 REPLACE는 DELETE+INSERT로 구현되어 FK CASCADE 발동. `ON CONFLICT DO UPDATE` 패턴 필수. (해결됨, 교훈으로 기록)
7. **Python 3.13 sqlite3 호환성**: `isolation_level` 기본값과 `executescript` 동작이 3.12와 다름. `isolation_level=None` + 명시적 commit 필요. (해결됨, 교훈으로 기록)
8. **tree-sitter byte offset vs Python str index**: tree-sitter는 UTF-8 byte offset을 반환하지만 Python str은 문자 단위 인덱싱. 멀티바이트 문자(한국어 주석, em-dash 등)가 소스에 있으면 `source[node.start_byte:node.end_byte]`가 잘못된 텍스트를 반환. 해결: `source_bytes = source.encode("utf-8")` 후 `source_bytes[start:end].decode()` 사용. (해결됨, Phase 3a에서 수정)
9. **Reactive Wiki Layer (Phase 5 후보)**: Hybrid Search 결과를 wiki 페이지로 "컴파일"하여 반복 질문 시 검색 없이 즉시 답변. Makefile 의존성 그래프처럼 소스 파일 해시를 추적하고, 변경 시 stale 마킹 → diff 기반 부분 재컴파일. 핵심 도구 3개: `compile_to_wiki`, `check_wiki_staleness`, `refresh_wiki_page`. 전제 조건: Hybrid Search Phase 2 완성 + 실사용 패턴 데이터 축적. 위험: LLM 추론 비용, "반복 질문" 판단 정확도, wiki 폭증 관리.
