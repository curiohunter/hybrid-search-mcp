# 대화 head 스플라이스는 자리를 뺏지 않는다 — 0-2 점검 (2026-09-27)

**질문**: PR #23 은 기억 head 스플라이스(`_merge_memory_results`, ambient `insert_at=2`)가 코드 레인에서
이미 1·2위에 있던 기억 항목을 빼서 3위로 **내리던** 결함을 고쳤다. `_merge_conv_results` 끝의
`[*live, *body[:cut], *indexed, *body[cut:]]` 도 같은 모양인가? (계획 `2026-09-27-open-the-doors.md` §3 0-2)

**답**: 아니다. 수정하지 않고 닫는다.

## 구조

- PR #23 결함의 조건은 "head 항목이 **입력 스트림에 이미 있다**"였다. 입력에서 빼고 insert 지점에
  다시 넣으니, insert 지점보다 위에 있던 항목은 내려갔다.
- 대화 레인은 `memory_intent` 이고 `node_types is None` 일 때만 검색된다. 바로 그 조건에서 메인
  레인은 `conv:` 로 시작하는 id 를 BM25·벡터 후보에서 걸러낸다. 따라서 `_merge_conv_results` 의
  `chunk_results` 에는 대화 턴이 없다 → `body` 에서 빠지는 항목이 없다 → `body[:cut]` 은 원래
  자리를 그대로 지키고, 대화 head 는 **순수 삽입**이다. `body[cut:]` 이 head 길이만큼 밀리는 것은
  설계(대화 head 를 증류 노트 바로 아래에 둔다)다.
- ambient 경로(`insert_at=2`, 기억 head 가 3위부터)에서는 대화 레인이 돌지 않으므로 `cut`(앞쪽에
  이어진 기억 행 수) 계산이 ambient 배치와 어긋날 일도 없다.

## 실측 (읽기만, 코드 변경 없음)

얼린 스냅샷 사본을 새로 떠서 현재 main 과 같은 상태(답 없는 기억 이주 · supersession 재계산)로
맞추고, `_merge_conv_results` 를 감싸 호출마다 셌다. `HYBRID_SEARCH_IN_FLIGHT=0`, 시계 고정.
예측(모든 항목 0)은 실행 전에 적었다.

| 질문 집합 | 문항 | 스플라이스 실행 | 입력에 대화 턴 | cut 위 강등 | 탈락 | head 강등 |
|---|---|---|---|---|---|---|
| 프로브 | 573 | 30 | 0 | 0 | 0 | 0 |
| Set A | 20 | 20 | 0 | 0 | 0 | 0 |
| Set B | 41 | 40 | 0 | 0 | 0 | 0 |
| 코드축 | 25 | 4 | 0 | 0 | 0 | 0 |

degraded(BM25 전용) 검색 0. 행 단위 덤프는 레포 밖(`~/.hybrid-search/benchmarks/`)에만 있다.

## 반증 조건

메인 레인이 대화 id 를 거르지 않게 되거나(예: `node_types` 에 `conv_turn` 을 명시한 호출이 대화
레인도 돌게 바뀌는 경우), 대화 레인이 ambient 경로에서도 돌게 되면 이 결론은 다시 봐야 한다.
