# 다음 세 트랙 — 노트 질문 정체성 · Set B 벡터 · LongMemEval 좁은 슬라이스

**작성** 2026-09-10 · **상태** 다음 세션 인계 · **브랜치** `main` (origin과 동기)
**마지막 커밋** `2025304` · **테스트** 1,762 통과 (extra 있는 경로·없는 경로 양쪽)
**전말** `docs/plans/2026-09-10-displacement-audit.md` (§10에 외부 심사·2차 코퍼스)

---

## 지금 수치 (얼린 스냅샷 · `--repeat 3` · 스프레드 0.00)

| | 값 | 비고 |
|---|---|---|
| Set A found / **top3** / MRR | 0.75 / **0.70** / 0.571 | **1차 지표** |
| Set B found | **0.05** | 41문항 중 2개 — 트랙 2의 대상 |
| 코드축 top5 / recall@10 / memory@3 | 0.92 / 0.77 / 0.16 | |
| 밀어냄 정밀도 (1차 코퍼스) | 9/9 = 100% · Wilson [0.70, 1.00] | |
| 밀어냄 정밀도 (**2차 코퍼스, 미보정**) | **3/6 = 50%** · [0.19, 0.81] | **전이 안 됨** |
| 술어 수용률 | 9.7 / 10k쌍 | |
| 프로브 재현율 손실 | 31% | 대칭 겹침 바가 원인 |

**목적함수 불변**: Set A top3가 1차 지표, Set B는 하한 제약.
**Set A top3를 0.70 아래로 떨어뜨리는 변경은 채택하지 않는다.**

---

## 트랙 1 — 통합 노트의 질문 정체성 (최우선)

### 문제

`reflector.py:100`이 통합 노트의 질문을 이렇게 정한다:

```python
representative_query: str  # newest member's query, verbatim
```

그리고 `reflector.py:293`이 그것을 노트의 `query:` 프론트매터로 그대로 쓴다.
**노트가 자기 클러스터를 종합한 답을 담고 있는데, 질문은 멤버 하나의 것을
복사한다.** 그래서 노트는 **자기가 답하지 않는 질문과 질문 겹침 1.00**을 갖고,
질문 기반 신호 전부(supersession, `_qa_topic_groups`, `_order_qa_by_recency`,
Reflector 클러스터링)가 이 경우에 눈이 먼다.

### 증거 (2차 코퍼스, 임계값 보정에 쓰이지 않은 코퍼스)

damage 3건 중 1건이 **모든 바를 통과한다**:

| chunk | 판정 | q_ov | a_ov | sym | 짝이 통합노트 |
|---|---|---|---|---|---|
| `40d491cd1eb6e55a` | **damage** | **1.00** | 0.63 | **1.00** | 예 |

임계값으로는 못 푼다 — 세 신호가 전부 만점이다. 나머지 damage 2건
(`bc18eb1eef7dee26`, `93a7b9b94c6740f3`)은 대칭 겹침이 **정확히 0.33**으로
1차 코퍼스에서 뽑은 값에 아슬하게 걸린다(과적합).

### 두 갈래 — 어느 쪽이든 먼저 재고 고를 것

**(A) 노트에 자기 질문을 만들어 준다.** `qa-reflect --prepare`가 에이전트에게
주는 지시문에 "이 노트가 답하는 질문 한 줄"을 요구하고, `finalize`가 그것을
`query:`로 쓴다. 대표 멤버 질문은 `representative_query:`로 따로 남긴다.

- 장점: 질문 경로가 노트에도 정직해진다. 검색 품질도 오를 수 있다.
- 단점: **기존 노트 37건**(valuein 20 + 이 레포 17)이 낡은 형식이다.
  소급 처리 필요 — 노트를 다시 쓰게 하거나, 형식 버전을 붙여 구형은 (B)로 취급.
- 착수점: `reflector.py` `prepare()` (212행), `finalize()` (257행), 노트 작성
  지시문 템플릿.

**(B) 노트를 질문 경로에서 뺀다.** `_same_topic_strict`에서 한쪽이 통합 노트면
질문 겹침을 신뢰하지 않고 **답변 겹침만으로** 판정한다.

- 장점: 코드 한 곳, 소급 문제 없음, 즉시 측정 가능.
- 단점: 노트↔원시 턴의 정당한 매핑(설계된 노출 경로)이 줄 수 있다 —
  라벨 legitimate 8건 중 3건이 그 모양이었다. **재현율 비용을 반드시 잴 것.**
- 착수점: `supersession.py` `_same_topic_strict()`, `_is_consolidation()` 이미 있음.

**권고**: (B)를 먼저 재라. 30분이면 4축이 나오고, (A)가 필요한지 그 숫자가
말해 준다. (A)는 노트 재작성 비용이 있어 되돌리기 어렵다.

### 판정 게이트 (둘 다 만족해야 채택)

1. **2차 코퍼스에서 `40d491cd1eb6e55a`가 더 이상 매핑되지 않는다.**
2. 밀어냄 정밀도를 **두 코퍼스 모두** 재고, 2차가 50%에서 오른다.
3. Set A top3 ≥ 0.70, Set B ≥ 0.05, 코드축 무변동.
4. 재현율(프로브 수준)이 31%에서 나빠지지 않는다.

```bash
SNAP=<스냅샷>   # 규약대로 rsync
python benchmarks/recompute_supersession.py --config $SNAP/config.toml
for P in valuein_homepage hybrid-search-mcp; do
  python benchmarks/displacement_audit.py --config $SNAP/config.toml \
    --project $P --sample 120 --labels ~/.hybrid-search/benchmarks/valuein_displacement_labels.json \
    --out /tmp/disp_$P.json
done
python benchmarks/supersession_recall.py --config $SNAP/config.toml \
  --project valuein_homepage --labels ~/.hybrid-search/benchmarks/valuein_cut_labels.json --probe 120
```

**2차 코퍼스 라벨이 아직 파일로 없다.** 위 6건의 판정은 계획서 §10 표와
이 문서에 있다 — `~/.hybrid-search/benchmarks/hsmcp_displacement_labels.json`으로
먼저 옮겨 적고 시작할 것. (실제 발화를 담으므로 레포 밖.)

---

## 트랙 2 — Set B 문장 단위 벡터

### 먼저 읽을 것: 이미 기각된 적이 있다

`docs/plans/2026-09-08-claim-level-retrieval.md` §"4. Set B는 별건으로 분리한다 —
15건의 어휘 불일치는 **입도로 안 풀린다**". 그때 기각된 것은 **claim/span 분해**
(어휘 단위 쪼개기)다. **문장 단위 벡터는 다른 처방이고 아직 안 했다.**

### 이미 있는 증거

프로브 `benchmarks/granularity_probes/b_sentence_index.py` 실측
(계획서 `2026-09-08-claim-level-retrieval.md` §18행 표):

| | 청크 단위(현행) | 문장 색인 → 부모 반환 |
|---|---|---|
| 부모 top-3 | **0/10** | **6/10** |
| 부모 top-10 | 3/10 | 8/10 |

비용 실측: **46분 · float32 346MB / 프로젝트**.

**반대 증거도 있다**: SGMem([2509.21212](https://arxiv.org/html/2509.21212v1))이
정확히 같은 구조(문장으로 검색해 부모 청크로 되돌림)로 LongMemEval top-5
0.690 vs 베이스라인 0.676 — **+1.4pt뿐**이다. 우리 프로브가 훨씬 큰 이득을
보이는 이유는 **Set B가 어휘 불일치만 골라 담은 슬라이스**라서일 수 있다.
즉 Set B는 오르고 전체는 안 오를 가능성을 열어 두고 시작할 것.

### 착수 전에 확인할 것 — Set B가 조용히 떨어졌을 수 있다

**숫자 이력이 안 맞는다.** `2026-09-08-claim-level-retrieval.md` §8 P0 표의
기준선은 **Set B found 0.15** [0.07, 0.28]이고, `2026-09-09` 인계 문서는 0.15 →
0.12로 적었다. 그런데 **이번 세션이 얼린 스냅샷의 기준선은 0.02**였다(그 뒤
답변없는 레코드 규칙으로 0.05로 올랐다).

0.15 → 0.02는 **설명되지 않은 하락**이고, 이번 주 작업 이전에 이미 그 상태였다.
가능성 세 가지: ① 골드셋이 그 사이 바뀌었다 ② 코퍼스가 움직였다(qa 2,017건까지
늘었다) ③ 어딘가에서 회귀했다.

**문장 벡터를 만들기 전에 이걸 먼저 가려라.** ③이면 벡터 작업보다 훨씬 싸고
효과가 크다. 골드셋 파일의 수정 시각과 문항 수(41)부터 비교하고,
`benchmarks/verify_conv_gold.py --config $SNAP/config.toml --gold <파일>`로
레인 순도를 확인할 것.

### 비용 조건 (착수 전 필수)

임베딩 백엔드는 **맥미니 로컬 ollama**(Tailscale)다 — **요금 0원**, 시간만 든다.
대상 규모: valuein `conv_turn` **1,900개 / 5.5M자**, 이 레포 263개 / 0.8M자.

- **맥미니 ollama에 벌크 작업을 동시에 걸지 말 것** (물린 전례가 있다).
- 디스크 346MB/프로젝트를 감당할지 먼저 확인.
- 인덱스 구조 변경이므로 **스키마 버전과 마이그레이션**이 필요하다
  (`storage/db.py`의 `index_meta.schema_version` 패턴 참고).

### 단계

1. **BM25만으로 문장 레인을 먼저 넣는다.** tantivy 삽입에는 임베딩이 필요 없다
   (계획서 §"BM25-only로 시작한다"). 문장 색인 자체가 파이프라인에서 도는지,
   부모 반환·슬롯 배분·레인 분류가 깨지지 않는지 **공짜로** 확인된다.
2. 1번이 Set A를 안 깨면 벡터를 얹는다. 여기서 46분·346MB가 든다.
3. **RRF ↔ 부모 폴딩 순서를 먼저 정할 것** — 미정의 상태다(계획서 심사 #3).
   span을 다시 쓸 때 반드시 먼저 정하라고 적혀 있다.

### 판정 게이트

- **Set B found**: 위 확인 결과에 따라 목표를 정한다. ①②면 지금 0.05가
  기준선이고 개선 목표는 **≥ 0.15**(과거 값 회복). ③이면 회귀를 먼저 고친다.
- **Set A top3 ≥ 0.70** (하나라도 깨면 채택 안 함)
- 코드축 무변동
- 밀어냄 정밀도·재현율 무악화 (문장 레인이 supersession 입력을 바꾼다)

### 함정

- **레인 단독 측정은 파이프라인을 예측하지 못한다.** 이 라인에서 5회 확인됐다.
  프로브의 6/10을 end-to-end 예측으로 쓰지 말 것.
- `meta.json`은 tantivy 자신의 메타파일이다. 사이드카를 그 이름으로 쓰면
  인덱스가 안 열리고 **에러 없이 조용히 효과가 0**이 된다.

---

## 트랙 3 — LongMemEval knowledge-update 슬라이스

### 먼저: 이 트랙은 두 번 기각된 결정과 충돌한다

1. `docs/plans/2026-07-15-agent-handoff-trust-bench.md` — "일반 메모리 벤치
   (LongMemEval/LoCoMo) **점수 경쟁을 하지 않고**, 아무도 측정하지 않는 축을
   frozen 프로토콜로 측정한다. **벤치마크 자체가 moat**."
2. `docs/plans/2026-09-01-retrieval-master-plan.md` §안 하는 것 — "**LongMemEval
   v1 점수 경쟁 — 포화·불신.** V2 축(workflow/gotcha)은 §7 genesis·harvested
   슬라이스가 흡수한다"

**그래서 이 트랙은 "점수 경쟁"으로 하면 안 된다.** 사용자가 요청한 목적은
다르다 — **"남과 같은 자를 하나라도 갖기"**(비교 가능성)다. 그 좁은 버전만 한다.

### 좁은 버전의 정의

- **knowledge-update 서브셋만.** 전체 500문항이 아니다. 우리 supersession이
  정면으로 겨냥하는 축이고, 그 외 축은 우리 설계 목표가 아니다.
- **순위표에 올리지 않는다.** 목적은 "우리 숫자를 남의 자로 한 번 읽는 것".
- **결과가 나쁘면 나쁜 대로 공개한다.** 좋을 때만 인용하면 비교 가능성이 아니다.

### 착수 전에 답해야 하는 것 (여기서 막히면 트랙을 접어라)

LongMemEval은 **영어 채팅 세션** 데이터다. 우리 시스템은 **프로젝트 레포**를
인덱싱한다. 그래서 다음이 먼저 확정돼야 한다:

1. **데이터를 어떻게 넣나.** 세션 히스토리를 `.hybrid-search/qa/` 형식의
   합성 코퍼스로 변환하는 어댑터가 필요하다. 우리 파이프라인은 프론트매터
   (`query:`, `timestamp:`, `## Answer excerpt`)를 읽는다 — 그 형식으로
   변환 가능한지 데이터 스키마를 먼저 확인할 것
   ([xiaowu0162/longmemeval](https://github.com/xiaowu0162/longmemeval)).
2. **무엇을 우리 숫자로 볼 것인가.** LongMemEval은 답변 정확도를 재고 우리는
   회수(top-k)를 잰다. **회수만 재고 생성은 안 재는 게 정직하다** — 우리는
   답변 생성기가 아니다. 그러면 SGMem류 논문의 top-5 회수 수치와 비교 가능.
3. **비용.** 코퍼스가 커지면 임베딩 시간이 든다(요금은 0원). 문항 수 × 세션
   길이를 먼저 세고, 트랙 2와 **동시에 맥미니를 때리지 말 것**.

### 판정 게이트

- **어댑터가 30분 안에 안 되면 접는다.** 형식이 안 맞으면 이 트랙의 비용이
  이득을 넘는다 — 그때는 선행 결정(점수 경쟁 안 함)이 옳았다는 결론을 적고 끝낸다.
- 되면: knowledge-update 서브셋 회수 수치 하나를 **README에 공개**하고,
  우리 Set A/B와 나란히 놓는다. 좋든 나쁘든.

---

## 측정 규약 (변경 없음, 도구만 늘었다)

```bash
# 1) 인덱스를 얼린다 (코퍼스가 측정 중에 움직인다)
SNAP=/tmp/hsnap
rsync -a --delete ~/.hybrid-search/projects/ "$SNAP/projects/"
rsync -a --delete ~/.hybrid-search/global/   "$SNAP/global/"
sed 's|data_dir = "~/.hybrid-search"|data_dir = "'"$SNAP"'"|' \
  ~/.hybrid-search/config.toml > "$SNAP/config.toml"

# 2) 매처/supersession을 고쳤으면 매핑을 다시 계산해 넣는다 (순수 CPU)
python benchmarks/recompute_supersession.py --config $SNAP/config.toml

# 3) 세 축
G=~/.hybrid-search/benchmarks
python benchmarks/run_conv_bench.py --config $SNAP/config.toml \
  --gold $G/valuein_conv_gold.json --out /tmp/a.json --repeat 3        # Set A
python benchmarks/run_conv_bench.py --config $SNAP/config.toml \
  --gold $G/valuein_conv_gold_rawonly.json --out /tmp/b.json --repeat 3 # Set B
python benchmarks/run_valuein_bench.py --config $SNAP/config.toml \
  --gold benchmarks/valuein_gold.json --out /tmp/c.json --limit 10      # 코드축

# 4) 파괴 축 (정밀도 + 재현율, 둘 다 봐야 한다)
python benchmarks/displacement_audit.py --config $SNAP/config.toml \
  --project <이름> --sample 120 --labels <라벨.json> --out /tmp/disp.json
python benchmarks/supersession_recall.py --config $SNAP/config.toml \
  --project <이름> --labels <컷라벨.json> --probe 120

# 5) 매처 임계값을 만질 때 (골드셋=재현율 바닥, 코퍼스=정밀도 신호)
python benchmarks/topic_threshold_sweep.py --config $SNAP/config.toml --project <이름>
```

- **`--repeat`의 스프레드가 0.00이 아니면 그 판독은 버려라.** 이 라인에서 한 번
  0.05가 나왔고, "나빠졌다"가 아니라 "측정이 오염됐다"였다.
- 골드셋·라벨은 **레포 밖**(`~/.hybrid-search/benchmarks/`). 실명·발화를 인용한다.
- 진단 도구: `benchmarks/granularity_probes/` (a~d, p1~p6b, README 있음).

---

## 주의사항 (이 라인이 밟은 지뢰, 누적)

1. **레인 단독 측정은 파이프라인을 예측하지 못한다** — 5회 연속. 항상 end-to-end.
2. **`meta.json`은 tantivy 자신의 메타파일** — 사이드카에 그 이름을 쓰면 조용히 0.
3. **in-flight 오버레이는 인덱스가 아니다** — 끄지 않으면 옆 세션이 들어온다.
4. **`run_memory_bench_v2.py`는 코퍼스에 합성 기록을 쓴다** — 이 라인에선 금지.
5. **valuein 레포에 git 쓰기 금지** — 읽기와 자체 `.hybrid-search/` 도구만.
6. **맥미니 ollama에 벌크를 동시에 걸지 말 것** — 물린 전례.
7. **증류가 원시를 대체하면 안 된다** — 통제 실험 43.9% → 28.0%. 동거만.
8. **측정된 비용이 있고 측정된 이득이 없으면 싣지 않는다.**
9. **2글자 접두는 기능어와 주제어를 구별하지 못한다** — 그래(그래프)·그리(그리드)·
   이미(이미지)·자기(자기오염)는 불용어 목록 금지. 테스트가 지킨다.
10. **목록을 만지기 전에 코퍼스 문서빈도부터.** `이유`를 넣었다가 DF 0%를 보고 뺐다.
11. **한 골드 쌍을 통과시키려 목록을 만지지 말 것** — 기준을 먼저 말하고 한 번에.
12. **두 지표가 반대를 가리키면 대개 원인을 못 찾은 것이다** — 비율끼리 저울질하지
    말고 라벨을 붙여 정확도로 바꿔라.
13. **분리자를 하나씩 고르지 말 것** — 넣은 뒤 나머지가 여전히 값을 하는지 다시 봐라.
    질문 겹침 바가 그렇게 세금이 됐다.
14. **술어를 좁혔는데 매핑이 늘면 알고리즘을 의심할 것** — greedy 완전연결 그룹에서
    짝을 읽으면 그렇게 된다. 지금은 쌍 단위라 단조롭고 테스트가 지킨다.
15. **어휘를 저정보로 내리면 그 어휘로 묻는 사람이 답을 잃는다.**
16. **임계값을 코퍼스 하나에서 뽑지 말 것** — 0.33이 2차 코퍼스에서 정확히
    경계에 걸렸다. 최소 두 코퍼스의 교집합에서 고를 것.
17. **n=9에 100%라고 쓰지 말 것** — Wilson CI를 붙여라. [0.70, 1.00]이다.

---

## 마지막 상태

- 브랜치 `main`, origin과 동기 · 작업 트리 clean
- 테스트 **1,762 통과** · 골드셋 게이트 PASS (두 설치 경로 양쪽)
- 이 기계는 **기본 설치**(`ko-prefix-1`), 라이브 매핑도 그 백엔드로 재계산 완료
- 러너 6종 · 라벨 2종(레포 밖) — 2차 코퍼스 라벨은 아직 파일화 안 됨(트랙 1)
- 외부 심사 전문은 세션 스크래치에만 있었다. 판정과 수용 항목은 계획서 §10에 있다
