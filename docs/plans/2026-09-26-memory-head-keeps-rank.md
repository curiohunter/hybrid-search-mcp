# 기억 head 는 이미 앉은 자리를 잃지 않는다 — 사전 등록

**상태**: 사전 등록 (측정 전, 코드 변경 전). 2026-09-26.
**배경**: 연구 문서 `docs/studies/2026-09-23-distillate-vs-raw-log.md` §23 — 질문 기반
판정에서 B 가 진 48건 중 40건의 첫 하락이 `_merge_memory_results` 의 ambient 재배치였다.

## 1. 결함

ambient 경로(`memory_intent` 가 아닐 때)는 `_merge_memory_results(..., insert_at=2)` 로
기억 head 를 코드 레인 3번째 자리에 끼운다. 목적은 docstring 대로 "최고 코드 결과를 밀어내지
않고 기억을 보장 노출"하는 것이다. 그런데 head 항목을 body 에서 **빼고 다시 넣기** 때문에,
코드 레인이 이미 1·2위에 올려 둔 기억 항목이 3위로 **내려간다**. 보장 노출 장치가 노출을
깎는다.

```python
body = [r for r in chunk_results if r.chunk_id not in seen]      # head 전부 제거
insert_at = max(0, min(insert_at, len(body)))
return body[:insert_at] + head + body[insert_at:]                 # 전부 insert 지점에
```

## 2. 변경안 (의사코드, 이대로 구현)

head 선택(`_qa_topic_groups` · `_prio` · `_seat_a_distillate` · `head_limit`)은 그대로다.
마지막 배치만 바꾼다.

```python
# head: 지금처럼 memory_head[:head_limit] 에서 chunk_id 중복 없이 뽑은 목록
if insert_at > 0:
    pos = {r.chunk_id: i for i, r in enumerate(chunk_results)}
    stay = {r.chunk_id for r in head if pos.get(r.chunk_id, len(chunk_results)) < insert_at}
    movers = [r for r in head if r.chunk_id not in stay]
else:
    movers = head                      # memory_intent 경로: 지금과 한 글자도 다르지 않다
mover_ids = {r.chunk_id for r in movers}
body = [r for r in chunk_results if r.chunk_id not in mover_ids]
insert_at = max(0, min(insert_at, len(body)))
return body[:insert_at] + movers + body[insert_at:]
```

- `stay` 항목은 body 에 남고, 그 위의 항목은 하나도 빠지지 않으므로(movers 는 전부
  insert 지점 이하에 있었거나 레인에 없었다) **원래 인덱스를 정확히 유지**한다.
- insert 지점 아래에 있던 head 항목의 동작은 지금과 같다(insert 지점으로 올라온다).
- 알고 남기는 것: insert 지점 **아래**(인덱스 ≥ `insert_at`)에 있던 head 항목은 지금처럼
  insert 지점으로 옮겨지므로, 다른 movers 뒤에 놓이면 원래보다 내려갈 수 있다. §3 은 이
  경우를 세지 않았다. 이번 변경의 범위 밖.
- 뒤 단계 영향: `_merge_conv_results` 의 `after_distilled` 는 스트림 앞쪽의 기억 행 수를 센다.
  `stay` 항목이 1위에 남으면 그 수가 달라질 수 있다 — 결과로 보고한다.

## 3. 영향 범위 (M 사본에서 읽기만, 측정 전)

2026-09-26 얼린 스냅샷 사본(`snap-m`, main 코드)에서, merge 를 감싸 "insert 지점보다 위에
있던 head 항목이 아래로 옮겨진" 경우를 셌다. 코드는 바꾸지 않았다.

| 질문 집합 | 전체 | ambient 경로 | intent 경로 | **영향 받음** | 이동 (전 → 후, 1-기준) |
|---|---|---|---|---|---|
| 프로브 (밀어냄 감사 573) | 573 | 543 | 30 | **524** | 1→3 371 · 1→2 150 · 2→3 3 (전체 634 기준) |
| Set A | 20 | 0 | 20 | 0 | — |
| Set B | 41 | 1 | 40 | 0 | — |
| 코드축 (`valuein_gold.json`) | 25 | 21 | 4 | **3** | 1→3 2 · 2→3 1 |

- 프로브의 91% 가 이 강등을 겪는다. 자기 답이 1위에서 2~3위로.
- **Set A · B 는 전부 intent 경로라 변경 대상이 아니다** → 목록이 바뀌지 않는다.
- **코드축 3문항**에서는 지금 기억 항목이 3위로 내려가며 코드 결과가 1·2위로 올라가 있다.
  수정 후에는 기억 항목이 1(또는 2)위에 남고 코드 결과가 한 칸씩 내려간다 — `_prio`
  docstring 의 2026-09-09 붕괴(primary-top5 0.92 → 0.72)와 같은 축의 위험이다.

## 4. 측정

- 갈래: **M** = main `9dbaba8` 코드, **F** = 이 브랜치(변경 적용) 코드. 같은 스냅샷 사본
  하나(`snap-m`, supersession 은 main 으로 이미 재계산됨 — 이 변경은 인덱스 시점 코드가 아니므로
  재계산 불필요)를 두 갈래가 순서대로 읽는다. 덤프 M → F → M′ 한 시간대.
- 질문: 2026-09-26 `--freeze` 파일 그대로(634).
- 러너·판정문 v1·렌더(`qa_window`)·모델(opus)·배치 10·두 차수·결합 규칙: 계획
  `2026-09-25-query-grounded-judgment.md` §3.1·§3.2 그대로.
- 코드축: 각 갈래에서 `run_valuein_bench.py --config <사본> --limit 10`.

### 4.1 판정자 보정 — 이번에는 승계한다 (감독 결정 필요)

§3 대로 Set A · B 목록은 M 과 F 가 같다 → 보정 대상 0 → 계획 §3.1 규칙("대상 < 5 면
보류")에 그대로 걸린다. 판정자 조건(판정문 v1 · 렌더 · 모델 · 배치 · 두 차수)이 2026-09-26
보정(5/5)과 **한 글자도 다르지 않으므로** 그 보정을 승계한다. 조건이 하나라도 바뀌면 승계하지
않는다. (대안: M vs G 보정 사례 50건을 이 판정자로 다시 판정해 일치율을 재확인 — 비용 10 호출.)

## 5. 채택 기준 (전부 충족)

1. 판정자 보정: §4.1 승계 (또는 감독이 정한 재확인 통과).
2. 프로브 판정 **F 승 ≥ M 승** (same · 무승부 · 노이즈 · 미판정 제외).
3. Set A · Set B found · top3 · mrr **≥ M** — 소수 넷째 자리, 허용 오차 없음.
4. 코드축 primary_top5 · recall@10 · memory@3 **≥ M — 허용 오차 없음.** 핵심 위험.

**기각**: 하나라도 미달. 코드축이 떨어지면 영향 받는 3문항을 문항별로 적는다.

**보고만**: 자기회수(573 중), same 비율, 순서 일치율, 노이즈 수, `after_distilled` 이 바뀐
질문 수, F 가 진 질문의 첫 하락 단계 분포(§23 의 추적기 그대로).

**무변동 예상 (미리 적는다)**: Set A · Set B 전 지표 완전 동일(목록 동일), 노이즈 0~1,
판정 사례는 프로브만. **예상**: F 승 > M 승(자기 답이 1위에 남는 524 문항), 코드축은
3문항 중 primary_top5 가 걸린 문항이 있으면 떨어질 수 있다 — 예상이 틀리면 그것도 결과다.

## 6. 그다음 순서

1. 채택되면 이 수정을 main 에 넣는다.
2. 그 뒤 B 를 새로 잰다 — **B+fix vs M+fix**. B worktree(`../hsm-gate`)에 fix 가 들어간
   main 을 병합하고, 계획 `2026-09-25` §3 기준 그대로(보정은 그때 M+fix vs B+fix 골드 목록이
   다르므로 새로 잰다). §23 의 가림막이 걷힌 상태에서 B 를 재는 것이 목적이다.
3. 기각되면 코드축 3문항을 읽고, ambient head 가 코드 결과와 1위를 다툴 때의 규칙
   (예: 코드 레인 1위가 기억 항목일 때만 유지)을 새로 사전 등록한다.

## 관련

- 원인 추적: 연구 문서 §23 · 추적 산출물 `~/.hybrid-search/benchmarks/query-judge-2026-09-26/trace-*.json`
- 영향 범위 산출물: 같은 폴더 `merge-count-M.json` · `merge-count-code-M.json` (커밋하지 않음)
- 코드: `src/hybrid_search/search/orchestrator.py` `_merge_memory_results` 마지막 세 줄
