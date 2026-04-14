# AST Chunker
> 마지막 업데이트: 2026-04-14 | 상태: fresh

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
