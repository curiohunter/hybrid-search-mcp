# AST Chunker
> 마지막 업데이트: 2026-04-14 | 상태: fresh | synthesized: 2026-04-14

## Overview

AST Chunker는 소스 코드를 의미 단위(함수, 클래스, 메서드)로 분할하는 모듈이다. tree-sitter C 파서를 사용하여 14개 언어의 AST를 파싱하고, 각 노드를 검색과 임베딩에 적합한 `CodeChunk`로 변환한다. AST 파싱이 불가능한 언어는 빈 줄 기반 폴백으로 처리하며, Markdown/JSON/YAML은 별도의 `doc_chunker.py`가 헤딩/크기 기반으로 분할한다. 두 모듈 모두 동일한 `CodeChunk` 데이터 구조를 반환하여 다운스트림(임베딩, BM25 인덱싱)에서 통일된 처리가 가능하다.

## Key Design Decisions

- **tree-sitter C 바인딩 사용**: 순수 Python AST 파서 대신 tree-sitter C 라이브러리를 사용하여 14개 언어를 단일 인터페이스로 지원. 언어별 파서 구현 없이 `LANGUAGE_CONFIG` 딕셔너리에 노드 타입만 등록하면 확장 가능 (`src/hybrid_search/index/ast_chunker.py:L34`)
- **클래스 헤더 분리**: 클래스 전체를 하나의 청크로 만들면 너무 커지므로, 헤더(처음 5줄)만 별도 청크로 추출하고 내부 메서드는 재귀 탐색하여 개별 청크로 분리 (`src/hybrid_search/index/ast_chunker.py:L175`)
- **대형/소형 청크 정규화**: 4000자 초과 청크는 오버랩 500자로 분할하고, 500자 미만 인접 청크는 병합. 검색 정밀도(너무 큰 청크는 노이즈)와 맥락 보존(너무 작은 청크는 의미 부족) 사이의 균형 (`src/hybrid_search/index/ast_chunker.py:L301`)
- **contextualized embedding_input**: 코드 자체만이 아니라 `"passage: [타입] 이름 in 파일경로"` 접두사 + import 목록 + docstring을 포함. E5/GTE 임베딩 모델의 passage prefix 컨벤션을 따르며, 검색 시 파일 경로와 함수명이 벡터에 인코딩됨 (`src/hybrid_search/index/ast_chunker.py:L247`)
- **import_map → call 모듈 바인딩**: 파일의 import문에서 `{로컬이름: 모듈경로}` 맵을 구축하고, 각 청크의 함수 호출에서 이 맵을 조회하여 모듈 경로를 바인딩. 이 정보가 [[Call Graph & Module Tree]]의 call edge resolution 입력이 됨 (`src/hybrid_search/index/ast_chunker.py:L206`)

## Data Flow

```
소스 파일 (bytes)
  │
  ├─ 코드 파일 (.py, .ts, .rs, ...)
  │    │
  │    ▼
  │  ts.Parser.parse(source)
  │    │
  │    ▼
  │  _walk_node(root) → 재귀 순회
  │    │
  │    ├─ CLASS_NODE_TYPES → 헤더 5줄 청크 + 메서드 재귀
  │    ├─ export_statement → 내부 선언 추출
  │    └─ CHUNK_NODE_TYPES → CodeChunk 생성
  │         │
  │         ▼
  │    _split_large_chunks (>4000자 분할)
  │         │
  │         ▼
  │    _merge_small_chunks (<500자 병합)
  │         │
  │         ▼
  │    _build_embedding_input (contextualized text)
  │
  ├─ 문서 파일 (.md, .json, .yaml)
  │    │
  │    ▼
  │  doc_chunker.chunk_doc_file()
  │    ├─ Markdown: ## 헤딩 분할
  │    └─ JSON/YAML: 크기 기반 분할
  │
  └─ 미지원 언어
       │
       ▼
     빈 줄 기반 폴백 (_fallback_chunk)

모든 경로 → list[CodeChunk] → Scanner → Embedder → Store
```

## Caveats

- **export 래핑 깊이 제한**: `export_statement` 내부의 선언만 한 단계 추출. `export default (function() { ... })()` 같은 중첩 패턴은 빈 export 청크가 될 수 있음 (`src/hybrid_search/index/ast_chunker.py:L182`)
- **소형 청크 병합 시 이름 충돌**: 병합된 청크의 이름이 `"name1+name2+Nmore"` 형식이라 qualified_name 검색에서 개별 함수를 찾기 어려울 수 있음. 하지만 병합 전 개별 이름이 사라지므로 call graph에서 참조 해제 가능성 있음 (`src/hybrid_search/index/ast_chunker.py:L330`)
- **tree-sitter 파싱 실패 시 무음 폴백**: AST 파싱 에러가 발생하면 `_fallback_chunk`로 전환되지만, 에러 로그가 debug 레벨이라 대규모 인덱싱 시 파싱 실패를 놓칠 수 있음 (`src/hybrid_search/index/ast_chunker.py:L155`)
- **SCSS와 CSS가 동일 파서 공유**: 두 언어가 `tree_sitter_css`를 공유하므로 SCSS 고유 문법(nesting, mixin 등)의 AST 노드가 누락될 수 있음 (`src/hybrid_search/index/ast_chunker.py:L75`)

## Related Modules

- [[Call Graph & Module Tree]] -- AST Chunker가 추출한 `calls` 리스트와 `import_map`이 call edge의 입력 데이터
- [[Embedder -- OpenAI API Backend]] -- `embedding_input` 필드를 벡터로 변환
- [[Indexing Pipeline]] -- `Scanner`가 AST Chunker를 호출하여 파일별 청크 생성을 orchestrate
- [[Search Engine]] -- BM25 인덱스가 `content`, `name`, `qualified_name`, `docstring` 필드를 사용

<details>
<summary>Structure (auto-generated)</summary>

## 개요

`src/hybrid_search/index/ast_chunker.py` — tree-sitter 기반 AST 파싱으로 코드 파일을 함수/클래스/메서드 단위의 의미 있는 청크로 분할한다. AST 파싱이 불가능한 언어는 빈 줄 기반 폴백 청킹을 사용한다.

`src/hybrid_search/index/doc_chunker.py` — Markdown은 `##` 헤딩 기준, JSON/YAML/TOML은 크기 기반으로 분할한다.

두 모듈 모두 동일한 `CodeChunk` 데이터 구조를 반환하여 다운스트림(임베딩, 인덱스)에서 통일된 처리가 가능하다.

## 지원 언어 (14개)

| 언어 | tree-sitter 패키지 | 주요 노드 타입 |
|------|-------------------|---------------|
| TypeScript | `tree_sitter_typescript` | function_declaration, method_definition, arrow_function, class_declaration, interface_declaration, type_alias_declaration, enum_declaration, export_statement, lexical_declaration |
| JavaScript | `tree_sitter_javascript` | function_declaration, method_definition, arrow_function, class_declaration, export_statement, lexical_declaration |
| Python | `tree_sitter_python` | function_definition, class_definition, decorated_definition |
| Rust | `tree_sitter_rust` | function_item, struct_item, enum_item, trait_item, impl_item, mod_item, type_item |
| Go | `tree_sitter_go` | function_declaration, method_declaration, type_declaration |
| Ruby | `tree_sitter_ruby` | method, class, module, singleton_method |
| Java | `tree_sitter_java` | class_declaration, interface_declaration, enum_declaration, method_declaration, constructor_declaration |
| C | `tree_sitter_c` | function_definition, struct_specifier, type_definition |
| C++ | `tree_sitter_cpp` | function_definition, class_specifier, struct_specifier, namespace_definition, template_declaration |
| Swift | `tree_sitter_swift` | function_declaration, class_declaration, protocol_declaration |
| Kotlin | `tree_sitter_kotlin` | function_declaration, class_declaration |
| CSS | `tree_sitter_css` | rule_set, media_statement, keyframes_statement |
| SCSS | `tree_sitter_css` (공유) | rule_set, media_statement, keyframes_statement |
| SQL | `tree_sitter_sql` | statement |

HTML 등 미지원 언어는 빈 줄(`\n{2,}`) 기반 폴백 청킹으로 처리된다.

## 청킹 전략

1. **AST 파싱**: `ts.Parser`로 소스를 파싱하고 `CHUNK_NODE_TYPES`에 해당하는 노드를 재귀 순회(`_walk_node`)하여 추출
2. **클래스 처리**: `CLASS_NODE_TYPES`에 해당하면 헤더(처음 5줄)만 별도 청크로 추출 후, 내부 메서드를 재귀 탐색
3. **export 처리**: `export_statement` 내부에 실제 선언이 있으면 해당 선언을 추출
4. **대형 청크 분할**: 비공백 문자 4000자 초과 시 `_split_large_chunks`로 분할
5. **소형 청크 병합**: 비공백 500자 미만인 인접 청크를 `_merge_small_chunks`로 병합 (4000자 한도)

## CodeChunk 데이터 구조

```python
@dataclass
class CodeChunk:
    id: str                  # SHA-256(project:path:start_byte:end_byte)[:16]
    project_id: str
    file_path: str           # 프로젝트 루트 기준 상대 경로
    language: str
    node_type: str           # function, class, method, struct, merged, block 등
    name: str                # 함수/클래스 이름 또는 anonymous_L{n}
    qualified_name: str      # "path::name" 또는 "ClassName.methodName"
    content: str             # 원본 소스 텍스트
    embedding_input: str     # 임베딩용 contextualized 텍스트
    imports: list[str]       # 파일 전체의 import 목록
    docstring: str | None    # JSDoc, Python docstring, Rust /// 등
    start_line / end_line: int
    start_byte / end_byte: int
    parent_name: str | None  # 소속 클래스/모듈 이름
    calls: list[tuple[str, str | None]]  # (함수명, 모듈경로) 쌍
```

## embedding_input 생성

`_build_embedding_input`이 contextualizedText 패턴으로 조합:

```
passage: [node_type] ParentClass.methodName in src/foo.py
imports: os, sys, pathlib
docstring text here
actual source code content
```

- `"passage: "` 접두사 (E5/GTE 계열 임베딩 모델 컨벤션)
- 헤더: `[타입] 이름 in 파일경로`
- import 목록 (최대 10개)
- docstring (있으면)
- 원본 코드

Doc Chunker의 embedding_input은 더 단순: `"passage: [section] heading in path\ncontent"`

## import 추출 + call name 추출

**import 추출** (`_extract_imports` + `_extract_import_map`):
- 파일 최상위 import문에서 경로와 이름을 추출
- `import_map`: `{로컬이름: 모듈경로}` 딕셔너리 생성 (예: `{"login": "./auth"}`)
- 14개 언어별 AST 노드 패턴을 개별 처리

**call name 추출** (`_extract_calls`):
- 각 청크 내부의 `call_expression`/`call` 노드에서 함수 호출명 추출
- `_BUILTIN_CALLS` (len, print, useState 등 70+개)와 `_BUILTIN_METHOD_CALLS` (console.log 등)를 필터링
- `this.method()` / `self.method()` 호출은 `__self__::ClassName`으로 모듈 매핑
- 일반 호출은 `import_map`에서 모듈 경로 조회
- 반환: `list[tuple[str, str | None]]` — `(함수명, 모듈경로 or None)`

## Doc Chunker (MD/JSON/YAML 처리)

`doc_chunker.py`는 `CodeChunk`를 재사용하며 `chunk_doc_file()` 진입점 제공:

- **Markdown**: `#{1,6}` 헤딩으로 섹션 분할. 헤딩이 없으면 전체를 단일 청크로
- **JSON/YAML/TOML**: 비공백 4000자 이하면 단일 청크, 초과 시 ~2000자 단위로 줄 기반 분할
- `node_type`은 markdown=`"section"`, 구조화 데이터=`"block"` 또는 `"document"`

## 대형 청크 분할 (_split_large_chunks)

상수:
- `LARGE_CHUNK_THRESHOLD = 4000` (비공백 문자 기준)
- `LARGE_CHUNK_SPLIT_SIZE = 2000`
- `LARGE_CHUNK_OVERLAP = 500`

동작:
1. 비공백 4000자 초과 청크를 감지
2. 문자 단위로 `SPLIT_SIZE * 2` 간격으로 슬라이싱, `OVERLAP * 2` 만큼 겹침
3. 각 파트에 `_part{n}` 접미사 부여 (예: `myFunction_part1`, `myFunction_part2`)
4. docstring과 calls는 첫 번째 파트에만 유지

소형 청크 병합 (`_merge_small_chunks`):
- 비공백 500자 미만인 인접 청크를 `"\n\n"` 구분자로 결합
- 병합 이름: `"name1+name2+name3+Nmore"` 형식
- `node_type = "merged"`, calls는 전체 합산

</details>
