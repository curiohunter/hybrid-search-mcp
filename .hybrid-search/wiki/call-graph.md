# Call Graph & Module Tree

> 마지막 업데이트: 2026-04-14 | 상태: fresh

## 개요

`callgraph.py`와 `dag.py`는 CodeWiki 자동 생성 파이프라인의 핵심이다.
AST에서 추출된 raw call edge를 chunk ID로 해석(resolve)하고,
방향성 의존성 그래프(DAG)를 구축한 뒤 connected component와 위상정렬로
기능 모듈 트리를 만들어 wiki 페이지를 자동 생성한다.

**소스 파일**:
- `src/hybrid_search/index/callgraph.py` — call edge resolution (236 줄)
- `src/hybrid_search/index/dag.py` — DAG 구축 + 모듈 트리 + wiki 생성 (673 줄)

## Call Edge 수집 (AST에서 호출 이름 + import 모듈 추출)

인덱싱 단계에서 AST 파서가 각 chunk마다 raw call edge를 DB에 저장한다.
각 edge는 다음 필드를 가진다:

| 필드 | 설명 |
|---|---|
| `caller_chunk_id` | 호출하는 쪽 chunk ID |
| `callee_name` | 호출 대상 함수/메서드 이름 |
| `callee_module` | import 경로 또는 `__self__::ClassName` |
| `callee_chunk_id` | (resolve 후 채워짐) |
| `confidence` | high / medium / low |

`callee_module`이 `__self__::ClassName` 형태이면 self/this 메서드 호출을 의미한다.

## 4단계 Resolution (High -> Medium -> Low)

`resolve_call_edges()` 함수가 모든 미해석 edge를 순회하며 4단계 전략을 적용한다.

### Strategy 0 — self/this 호출 (High)
`callee_module`이 `__self__::ClassName`이면 `class_members` 인덱스에서
해당 클래스의 멤버를 조회한다. 단일 매치 -> High, 복수 매치 -> Medium.

### Strategy 1 — import 경로 매칭 (High)
`_build_module_index()`가 파일 경로에서 다양한 import 형태를 생성한다:
- stem: `src/auth/login`
- `./` 접두사: `./src/auth/login`
- index 파일 관례: `src/auth/index.ts` -> `src/auth`
- Python dotted path: `src.auth.login`

모듈 인덱스에서 `callee_module`을 찾고 `callee_name`과 일치하는 chunk를 반환한다.
실패 시 `qname_index`를 fallback 스캔한다 (Strategy 1b).

### Strategy 2 — qualified name 매칭 (Medium)
`callee_name`에 `.`이 포함되면 `qname_index`에서 직접 조회하거나
접미사 매칭 (`endswith(.name)`)을 시도한다.
단일 후보가 있으면 Medium (단, `COMMON_NAMES`에 속하면 Low로 강등).

### Strategy 3 — name-only 매칭 (Low)
복수 후보 중 같은 파일에 있는 것을 우선 선택 (-> Medium 승격).
그 외에는 첫 번째 후보를 Low 신뢰도로 반환한다.
`COMMON_NAMES`(run, init, get, set 등 60여 개)는 컨텍스트 없이 매칭하지 않는다.

## DAG 구축 (방향성 의존성 그래프)

`build_dependency_graph()`는 resolve된 edge 중 **High + Medium만** 사용하여
방향성 인접 리스트 `(forward, reverse)`를 생성한다.
- `forward[caller] = {callee1, callee2, ...}`
- `reverse[callee] = {caller1, caller2, ...}`
- self-loop는 제외한다.

## Connected Components (BFS -> 기능 모듈 식별)

`find_connected_components()`는 forward + reverse 그래프를 **무방향**으로 합쳐
BFS로 연결 요소(connected component)를 찾는다.

- edge에 한 번도 등장하지 않은 chunk는 **isolated**로 분류
- component는 크기 내림차순 정렬
- 각 component가 하나의 기능 모듈 후보가 된다

## Topological Sort (Kahn's algorithm)

`topological_sort()`는 각 component 내에서 Kahn's algorithm을 실행한다.

1. component 내부 edge로 in-degree 계산
2. in-degree = 0인 노드(entry point)부터 큐에 넣고 순회
3. 순환(cycle)이 있으면 나머지 노드를 끝에 추가
4. 결과를 **reverse**하여 leaves-first(bottom-up) 순서 반환

이 순서는 wiki 페이지를 "의존하는 쪽부터 먼저" 생성하기 위해 사용된다.

## 모듈 이름 자동 유도

`_derive_module_name()`은 모듈에 속한 파일 경로들에서 이름을 추론한다:

1. 파일이 1개 -> 파일 stem 사용
2. 최장 공통 디렉토리 접두사 계산, `src`/`lib`/`app` 등 제너릭 이름 제외
3. 공통 접두사가 없으면 가장 빈번한 부모 디렉토리 이름 사용
4. 중복 이름은 `_deduplicate_names()`로 `-1`, `-2` 접미사 부여

큰 모듈(`> MAX_MODULE_CHUNKS = 40`)은 `_split_large_module()`로
서브디렉토리별 분할한다.

## WikiPlan 생성

`generate_wiki_plan()`이 전체 파이프라인을 실행한다:

```
call_edges + chunks + files
   |
   v
build_dependency_graph()      -- High+Medium edge만
   |
   v
find_connected_components()   -- BFS 무방향 탐색
   |
   v
topological_sort() per comp   -- Kahn's bottom-up
   |
   v
_derive_module_name()         -- 경로 기반 이름
   |
   v
WikiPlan { modules, isolated_modules, coverage }
```

**WikiPlan 구조**:
- `modules` — graph 기반 모듈 (크기 내림차순)
- `isolated_modules` — edge 없는 chunk를 디렉토리별 묶음 (MIN_MODULE_CHUNKS >= 2)
- `coverage` — 전체 chunk 중 모듈에 포함된 비율

`generate_all_wiki_pages()`가 각 모듈별 마크다운을 생성하고
`index.md`를 맨 앞에 삽입하여 `(WikiPlan, list[WikiPageContent])`를 반환한다.
wiki 페이지에는 파일 목록, entry point, 심볼별 호출/피호출 관계,
외부 의존성이 LLM 없이 결정적(deterministic)으로 기술된다.
