# Round-3 회귀 재실행 — ac71b8f vs head (2026-07-27)

**요구 프로토콜** (라운드 3 판정): 동일 코퍼스(valuein_homepage)·동일
인덱스·동일 설정, 원시 결과(표시 순위·node_type·양 레인 rank) 저장,
compounding은 answer_found(OR-지표) 대신 분해 지표.

**실행**: baseline = `ac71b8f` git worktree(코드·하네스 모두 baseline),
head = meta-recall 협소화 커밋(본 커밋). 산출물 전부 이 디렉터리에.

## 프로토콜 사고와 폐기 산출물 (투명성)

1차 파이프라인의 실행 순서(head 벤치 → compounding)가 **인덱스 상태
드리프트**를 만들었다: compounding이 plant한 레코드가 남은 인덱스에서
base 벤치가 돌았고, head 벤치는 그 전 상태에서 돌았다. 그 결과였던
`valuein_head_6ad6297.json` / `raw_head_6ad6297.json`은 **동등 조건
비교로 무효** — 삭제하지 않고 남기되 판정에 쓰지 말 것. 유효 비교는
`valuein_base_ac71b8f` vs `valuein_head_post_meta_fix`(동등 인덱스
상태에서 측정, per-query로 검증됨). 이 사고 자체가 M2/M5 recency-head
과대 트리거(bare "뭐였지")를 드러내 수정으로 이어졌다(아래).

## 결과 1 — 검색 gold (25쿼리)

- **순위·리콜: 사실상 동일.** per-query 변화 2건뿐, 둘 다 개선:
  S2 rank 5→4 (MRR 0.20→0.25), P1 rank 5→4 (MRR 0.20→0.25).
  카테고리 합계 델타는 MRR +0.004 (OVERALL)뿐.
- **confidence: 11건 strong→mixed** (S4, P2, P4, P5, R1~R4, M1, M3, M4).
  원시 검증: 해당 쿼리의 score-top이 **legacy qa(무 verification, 예:
  74d ago)** — 라운드 1 요구사항 "legacy는 strong 앵커 불가" quarantine의
  의도된 보수화. 검색 결과 자체는 동일, 라벨만 강등. **strong coverage에
  영향** — calibration 게이트(CC-T3) 측정 시 반영 필요.
- **표시 순서: 18/25에서 재배열** — superseded/meta-recall 마커·강등,
  splice 등에 의한 non-gold 슬롯 이동. gold 지표 영향은 위와 같이 2건.

## 결과 2 — compounding (20 pairs, 분해 지표, 양측 fresh clean 실행)

compounding은 cold 단계에서 qa 제거+전체 reindex로 시작하므로 인덱스
드리프트와 무관 (내부 리셋).

| track/phase | answer_found | qa_hit | memory_primary | recall@10 | MRR |
|---|---|---|---|---|---|
| identity/cold | 0.700 (=) | 0.000 (=) | 0.000 (=) | 0.525 (=) | 0.489 (=) |
| identity/warm | 0.950 (=) | 0.500 (=) | 0.800 (=) | 0.483 (=) | 0.428 (−0.003) |
| paraphrase/cold | 0.650 (=) | 0.000 (=) | 0.000 (=) | 0.433 (=) | 0.512 (=) |
| paraphrase/warm | 0.900 (+0.050) | 0.450 (−0.050) | 0.750 (−0.050) | 0.450 (=) | 0.412 (=) |

OR-지표(answer_found)에 기대지 않은 분해 지표에서 **base와 등가**
(paraphrase ±0.05는 1쌍 단위 노이즈). 이전 제출의 "품질 회귀 아님"
주장은 철회하고, 이 분해 등가로 대체한다.

## 재실행이 잡은 추가 결함 (수정 포함)

- **meta-recall 과대 트리거**: bare "뭐였지"가 topical 히스토리 질문
  ("hook 차이가 뭐였지?")에 recency head를 발동시켜 gold 타깃을 rank
  3→7로 밀었음(M2/M5). 패턴에서 제거 + negative 테스트 2건. 수정 후
  M2/M5는 base와 동일.

## 산출물 목록

- `valuein_base_ac71b8f.json` / `valuein_head_post_meta_fix.json` — 벤치 (유효쌍)
- `raw_base_ac71b8f.json` / `raw_head_post_meta_fix.json` — 25쿼리 원시
  (display rank, node_type, rrf, bm25/vector rank, trust_meta)
- `compounding_{base,head}_*_2026-07-27.{json,md}` — 분해 지표 원본
- `valuein_head_6ad6297.json` / `raw_head_6ad6297.json` — **무효** (위 사고 항 참조)
