# 기억 헤드 선택 — 회수를 네 번 고쳐 0이었고, 선택을 두 번 고쳐 움직였다

**작성** 2026-09-09 · **상태** 다음 세션 인계 · **브랜치** `feat/distilled-before-raw`
(**미푸시**) · **마지막 커밋** `e8d8a2b` · **테스트** 1,748 통과

---

## 한 줄 요약

여섯 라운드 동안 "무엇을 회수하고 어떻게 정렬하는가"를 고쳤는데 전부 0이었고,
**결과를 고르는 단계**(`_merge_memory_results` 이후)의 버그 하나와 규칙 하나를
고치자 처음으로 지표가 움직였다. **다음 대상은 그 선택 단계가 의존하는
주제 매처(`qa_topics`)다** — §12의 버그와 §13의 손실이 같은 매처에서 나왔고,
세 곳이 그것을 쓴다.

---

## 지금 수치 (얼린 스냅샷 · `--repeat 3` · 스프레드 0.00)

| | 세션 시작 | **현재** |
|---|---|---|
| Set A answer_found | 0.70 [0.48, 0.85] | **0.75 [0.53, 0.89]** |
| Set A **answer_in_top3** (1차 지표) | 0.60 [0.39, 0.78] | **0.70 [0.48, 0.85]** |
| Set A MRR | 0.531 | **0.624** |
| Set B answer_found | 0.15 | 0.12 |
| Set B top3 / MRR | 0.02 / 0.031 | 0.02 / 0.028 |
| 코드 축 primary-top5 / recall@10 / memory@3 | 0.92 / 0.77 / 0.16 | **동일** |

**목적함수**(이 라인의 모든 판단에 적용): **Set A top3가 1차 지표**,
Set B는 하한 제약. Set A top3를 0.70 아래로 떨어뜨리는 변경은 채택하지 않는다.

---

## 이번 세션이 한 일

| 커밋 | 내용 | 효과 |
|---|---|---|
| `d2d62fe` | 증류 노트를 원시 턴 위로 (conv 헤드를 기억 헤드 아래로) | top3 0.20 → 0.40 (당시 n=15) |
| `181c8f6` | `retrieval_depth`를 `limit`에서 분리, 한국어 과거회상 어미 분류기 | found 0.60 → 0.80 |
| `1a3f758` | in-flight 오버레이 차단 스위치, 슬롯 크기 측정, 텔레메트리 카드 상류 차단 | 측정 재현성 확보 |
| `70f02bc` | 골드셋 확대 (B 10→41, A 15→20), Wilson CI | **기준선 재설정** |
| `336caf9` | claim splitter + P1·P2 파일럿 | 설계 판단 |
| `b891daa` | P3~P5 (풀·집계 스윕, 병인 라벨링, 삽입 게이트) | 설계 판단 |
| `bfd4e4e` | 라운드2 외부 심사 반영 — 판정 정정 | 범위 축소 |
| `b834202` | 문장 span 폴딩 **구현 후 되돌림** | 0 또는 음수 |
| **`9ad7c97`** | **supersession 버그** — 통합 노트가 통합 노트를 대체 | **top3 0.60 → 0.65** |
| **`e8d8a2b`** | **토픽 묶음 대표를 최신 → 가장 잘 맞는 것** | **top3 0.65 → 0.70** |

계획 문서 두 개에 전 과정이 있다:
- `docs/plans/2026-09-04-memory-accumulation-recovery.md` §13~§17 (라운드 3~6 + 심사)
- `docs/plans/2026-09-08-claim-level-retrieval.md` §1~§13 (파일럿 P0~P5 + 심사 2라운드 + 구현/철회)

---

## 다음에 할 것 (우선순위)

### 1. 주제 매처 감사 (`src/hybrid_search/search/qa_topics.py`) — **최우선**

**왜 지금인가.** 이번 세션의 두 성과가 모두 이 매처의 오작동을 우회한 것이다:

- §12: 매처가 **무관한 통합 노트 두 개**를 같은 주제로 묶어 supersession
  쌍으로 만들었고, 정원이 찼을 때 답하던 노트가 결과에서 **삭제**됐다.
- §13: 같은 매처가 만든 묶음에서 "최신"을 대표로 뽑던 규칙이 관련도 높은
  멤버를 밀어냈다.

즉 두 번 다 **매처를 고친 게 아니라 그 결과에 덜 의존하게 만든 것**이다.
매처 자체는 아직 안 봤다.

**쓰는 곳 세 군데** — 하나를 고치면 셋이 같이 움직인다:
| 위치 | 용도 | 잘못되면 |
|---|---|---|
| `memory/supersession.py` (`_same_topic_strict`) | 인덱스 시점 old→new 매핑 | 답하던 기록이 교체·삭제됨 |
| `search/orchestrator.py` `_qa_topic_groups` | 기억 헤드 묶음 | 관련도 높은 멤버가 대표를 못 됨 |
| `search/orchestrator.py` `_order_qa_by_recency` | 최종 목록 최신순 재정렬 | 무관한 것끼리 순서가 뒤바뀜 |

**이미 있는 도구**: `benchmarks/topic_gold_set.json` (ko/en/mixed로 임계값을
뽑았던 골드셋), `benchmarks/topic_gold_eval.py`.

**첫 걸음**: valuein의 실제 통합 노트 20건 × 20건 전조합에 매처를 돌려
**같은 주제로 판정되는 쌍**을 전부 뽑는다. 사람이 봐서 몇 쌍이 진짜인지 센다.
§12에서 발견한 쌍(`ba961239` ↔ `3faa182b`)이 유일한 오탐인지, 빙산의 일각인지가
거기서 갈린다.

**주의**: 임계값을 만지면 세 곳이 동시에 움직인다. 세 축(Set A · Set B ·
코드 축)을 전부 재고, `benchmarks/topic_gold_eval.py`도 같이 봐야 한다.

### 2. 대화 레인 — 어휘 불일치 (벡터 작업)

Set B가 0.12에서 안 오른다. 병인은 §10.2에 라벨링돼 있다: **41문항 중 15개는
질의와 정답 문장이 단어를 하나도 공유하지 않는다.** 잘게 쪼개도(문장 색인
실측) 안 되고, 어떤 어휘적 수단으로도 안 된다.

- 처방은 문장 단위 **벡터** 색인이다. 프로브 (b)가 부모 top-3 0/10 → 6/10을
  냈다(당시 n=10, 구두점 split, 46분 · float32 346MB / 프로젝트).
- **단, 이번 세션이 배운 것을 먼저 적용할 것**: 레인 단독 측정은 파이프라인을
  예측하지 못한다(4회 연속). 벡터 span을 넣기 전에 **헤드 선택이 그 순위를
  읽기는 하는지**부터 확인해야 한다 — 1번을 먼저 하라는 이유다.

### 3. 미룬 것들

| 항목 | 상태 |
|---|---|
| claim/span 인덱스 | 구현·측정·**철회**(§11). 재구축은 1초. 헤드 선택이 순위를 읽게 되면 다시 짓는다 |
| Set A n=20 천장 | 이 코퍼스의 증류물이 통합 노트 20 + 실사용 카드 3뿐. Reflector를 더 돌려야 늘어난다 |
| valuein 잔여 Reflector 클러스터 | 2회차까지 20건 통합, 잔여 다수 |
| Reflector 자동화 | 수동 트리거라 조용히 멈춘다. `REFLECT.md` 백로그 신호는 있으나 훅 배선은 없음 |
| RRF ↔ 폴딩 순서 | 심사 #3이 지적한 미정의. span을 다시 쓸 때 반드시 먼저 정할 것 |

---

## 측정 규약 — 이대로 안 하면 숫자가 거짓말한다

```bash
# 1) 인덱스를 얼린다 (코퍼스가 측정 중에 움직인다 — 20분에 +80청크 실측)
SNAP=/tmp/hsnap
rsync -a --delete ~/.hybrid-search/projects/ "$SNAP/projects/"
rsync -a --delete ~/.hybrid-search/global/   "$SNAP/global/"
sed 's|data_dir = "~/.hybrid-search"|data_dir = "'"$SNAP"'"|' \
  ~/.hybrid-search/config.toml > "$SNAP/config.toml"

# 2) 세 축을 잰다 (러너가 HYBRID_SEARCH_IN_FLIGHT=0을 기본으로 건다)
G=~/.hybrid-search/benchmarks
python benchmarks/run_conv_bench.py --config $SNAP/config.toml \
  --gold $G/valuein_conv_gold.json --out /tmp/a.json --repeat 3        # Set A
python benchmarks/run_conv_bench.py --config $SNAP/config.toml \
  --gold $G/valuein_conv_gold_rawonly.json --out /tmp/b.json --repeat 3 # Set B
python benchmarks/run_valuein_bench.py --config $SNAP/config.toml \
  --gold benchmarks/valuein_gold.json --out /tmp/c.json --limit 10      # 코드 축

# 3) 골드셋을 고쳤으면 레인 순도를 검증한다
python benchmarks/verify_conv_gold.py --config $SNAP/config.toml --gold <파일>
```

- **골드셋은 레포 밖**(`~/.hybrid-search/benchmarks/`)에 있다. 실제 기관명·발화를
  인용하므로 커밋 금지 (`CLAUDE.md`).
- `--repeat`의 스프레드는 **결정성**이고 Wilson CI가 **표본오차**다. 둘은 다르다.
- 질의별 순위 변화표를 항상 같이 남길 것 — 집계보다 강한 증거다.
- 진단 도구는 `benchmarks/granularity_probes/` (a~d, p1~p6b, README 있음).

---

## 주의사항 — 이번 세션이 밟은 지뢰

1. **레인 단독 측정은 파이프라인을 예측하지 못한다.** 4회 연속 실패:
   conv lexical rerank(0), 랭킹 규칙 6종(0), 문장 span 폴딩(레인 단독
   0.10→0.40 인데 end-to-end 0 또는 음수), 기억 레인 BM25 가중치(상충).
   **항상 end-to-end로 판정할 것.**
2. **`meta.json`은 tantivy 자신의 인덱스 메타파일이다.** 사이드카를 그 이름으로
   쓰면 인덱스가 안 열리고, 파생 인덱스는 폴백하게 돼 있어 **에러 없이 조용히
   효과가 0**이 된다.
3. **in-flight 오버레이는 인덱스가 아니다.** 워킹 트리와 살아 있는 세션의
   트랜스크립트를 읽는다. 끄지 않으면 "얼린" 측정에 옆 세션이 들어온다.
4. **`benchmarks/run_memory_bench_v2.py`는 valuein 메모리에 합성 기록을 쓴다.**
   코퍼스를 오염시키므로 이 라인의 측정 중에는 돌리지 말 것.
5. **valuein 레포에 git 쓰기 금지.** 읽기와 자체 `.hybrid-search/` 도구 실행만.
6. **임베딩 백엔드에 벌크 작업을 동시에 걸지 말 것** (맥미니 ollama가 물린 전례).
7. **증류가 원시를 대체하면 안 된다** — 통제 실험에서 43.9% → 28.0%
   (arXiv 2601.00821). 동거만 허용.

---

## 관련 파일

| 파일 | 역할 |
|---|---|
| `src/hybrid_search/search/qa_topics.py` | **다음 작업 대상** — 주제 매처 |
| `src/hybrid_search/memory/supersession.py` | 인덱스 시점 old→new 매핑 (§12에서 통합 노트 제외) |
| `src/hybrid_search/search/orchestrator.py` `_merge_memory_results` | 기억 헤드 선택 (§13에서 대표 규칙 변경) |
| `src/hybrid_search/search/orchestrator.py` `_splice_superseding` | 낡은 답 교체 (§12에서 통합 노트 가드) |
| `src/hybrid_search/search/slot_planner.py` | 슬롯 배분 (3/3이 측정된 값) |
| `benchmarks/granularity_probes/` | 진단 도구 10종 + README |
| `benchmarks/topic_gold_set.json` · `topic_gold_eval.py` | 주제 매처 골드셋 (1번 작업에서 씀) |

---

## 마지막 상태

- 브랜치 `feat/distilled-before-raw` — **푸시 안 함**, main과 10커밋 차이
- 테스트 **1,748 통과** (174s)
- 인덱스 일관성: valuein·본 레포 모두 SQLite = BM25 = USearch, unfinished 0
- 스크래치(`/private/tmp/.../scratchpad/`)의 스냅샷·프로브 산출물은 세션과 함께
  사라진다. 필요하면 위 규약대로 다시 만들면 된다(스냅샷 rsync 1분, 프로브는
  전부 재실행 가능)
