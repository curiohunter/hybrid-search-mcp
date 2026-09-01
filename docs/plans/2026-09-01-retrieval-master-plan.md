# 리트리벌 마스터 플랜 — 조사 수렴점의 설계

**작성** 2026-09-01 · **상태** 설계 확정 대기 · **선행 문서**
`docs/handoff/2026-08-29-result-slot-allocation.md` (WS1의 상세 진단)

---

## 0. 이 문서가 답하는 질문

"벡터 임베딩만으로는 부족하다. 저장 후 LLM 합성(위키/그래프)과 새로운
리트리벌이 필요한 것 아닌가?" — 2026-09-01 3축 문헌 조사(GraphRAG /
write-time 합성 / 심볼릭 스토어)와 내부 증거(83% 인덱스 자기오염,
valuein 후기 v3, 슬롯 배분 진단)를 종합한 답이다.

**결론: 파이프라인에 빠진 칸은 없다. 만들어둔 칸들이 연결돼 있지 않다.**
새 아키텍처(그래프 DB, temporal KG, TypeDB)는 전부 과잉으로 판정됐고,
설계는 5개 작업줄기(WS)로 기존 자산을 연결한다.

근거 요약 (전문은 메모리 `project-retrieval-research-2026-09`):

| 조사 결과 | 설계 반영 |
|---|---|
| 병목은 write가 아니라 retrieval — 검색 방법 20pt vs 저장 전략 3–8pt (arXiv 2603.02473) | WS1이 최우선. 노출 수리 전의 모든 개선은 측정이 왜곡된다 |
| 그래프는 멀티홉만 승, 처방은 "lexical 히트의 1홉 확장" (LARGER, +13.9pt) | WS2. 그래프 DB 없이 기존 call graph 재사용 |
| 통합(Reflector)은 3자 수렴(Mastra·Dreaming·Hindsight), 단 compaction은 공식 공격 표면 | WS3. 산출물은 리뷰 가능한 diff + provenance 강제 |
| insight만 전이, 재서술은 negative transfer (MTL 2604.14004); staleness는 1급 실패 (EA-Graph) | WS4. 위키 재규정 + 커밋 앵커 |
| 벡터 블라인드스팟 = 집계·시간델타·부정. 해법은 SQLite 안 typed facts + 템플릿 쿼리 (자유 text-to-query는 60%대) | WS5. 최소 스키마, 템플릿만 |
| 발표 점수 재현 실패가 일상 (EverMemOS 92→38%) | 관통 게이트: 자체 holdout 벤치 없이 머지 금지 |

---

## 1. 설계 불변식 (모든 WS에 적용)

문헌과 내부 사고에서 도출했다. 위반하는 구현은 리뷰에서 반려한다.

1. **배분은 한 곳에서 결정한다.** 레인별 예산을 각자 계산하고 아무도 합을
   안 세는 구조가 슬롯 결함의 원인이었다(실패한 시도 3종 참조).
2. **합성물은 raw를 대체하지 않는다. 동거하되 레인을 분리한다.**
   RAPTOR의 승리 공식이자, 83% 오염 사고의 교훈. 합성물은 항상
   구분되는 node_type과 trust_meta 표기를 갖는다.
3. **LLM의 자유 쿼리 생성은 검색 경로에 넣지 않는다.** 프런티어 모델도
   실행 정확도 60%대(CypherBench). 분류기가 트리거하는 고정 템플릿만.
4. **통합 산출물은 provenance 없이는 존재할 수 없다.** 모든 파생 주장은
   원본(커밋/파일/qa id)을 가리킨다. 통합은 리뷰 가능한 diff로 나온다.
5. **grep과 싸우지 않는다.** BM25 레인은 우리의 grep 대응물이고
   (GrepRAG), 벡터·그래프·합성은 전부 그 보완재다.
6. **새 MCP 도구를 추가하지 않는다.** (기존 원칙 유지 — 도구당 ~1k 토큰
   영구 상주. 전부 CLI + 훅 + 기존 도구 응답 확장으로.)
7. **어떤 WS도 holdout 전후 측정 없이 머지하지 않는다.** (§7)

---

## 2. WS1 — SlotPlanner: 중앙 슬롯 배분자 (P0)

### 문제
`limit=10`에 실제 코드·문서 청크가 2칸. 모듈 카드(`limit//2`)와 메모리
(`limit//3`)가 각자 예산을 갖고 합을 아무도 확인하지 않으며,
`_complete_supersession`이 `_make_response` 뒤에 행을 덧붙여 최종 표시
집합은 그 뒤에야 확정된다. rrf 0.0(양 레인 무순위) 카드가 1위로 노출된다.

### 설계
새 모듈 `search/slot_planner.py`. **조립 파이프라인의 모든 레인 후보가
모인 시점에 한 번만 호출**되고, 이후 단계는 planner가 준 할당량을
초과할 수 없다.

```
입력:  limit, query_type, memory_intent, graph_intent,
       풀들: chunks / module_cards / module_members / memory(qa·card·commit)
       / supersession_pending / (WS4 이후) wiki·rationale
출력:  SlotPlan { lane별 확정 개수, 합 == limit }
```

배분 규칙 (핸드오프의 설계 제약을 계약으로 승격):

- 청크 ≥ `ceil(limit/2)`. **재료 부족의 정의**: 두 레인(BM25·벡터)
  어느 쪽에도 랭크된 청크 후보 수가 배정량 미만인 경우만. 관련성
  임계값은 도입하지 않는다(캘리브레이션 변수 증설 금지) — 랭크 존재
  여부만 본다.
- 메모리 ≥ 1, 과반 미만. `memory_intent` 질의는 캡 해제(기록이 곧 답).
  **판정 주체는 기존 `_has_memory_intent`**(토큰 기반, LLM 아님)이고,
  캡 해제 시에도 청크 최소 1칸은 유지한다(분류 오판 시 전멸 방지).
- 보조 레인 내 우선순위: **카드 → 메모리 → 멤버** (ablation 근거:
  카드는 F2·F4 승리 기여, 멤버는 최대 오염원에 기여 S1 1건).
- 자른 만큼 반드시 청크로 보충 — planner는 재료가 남아 있는 단계에서
  호출되므로 가능하다.
- supersession 추가 행은 planner가 남긴 예약 슬롯 안에서만.
- **rrf 0.0 행은 랭크 근거(bm25_rank 또는 vector_rank)가 있는 행을 밀어낼
  수 없다** — 무순위 카드는 보조 예산 안에서만 노출된다.

### 구현 경로
`_merge_memory_results`(head_limit)와 `_interleave_modules`(module_budget)
의 자체 예산 계산을 제거하고 SlotPlan 소비자로 바꾼다.
`_complete_supersession`은 SlotPlan의 예약분을 받는다.

### 완료 기준 (핸드오프 승계)
| 지표 | 현재 | 목표 |
|---|---|---|
| valuein gold | 17/25 (유효 16/19) | ≥ 18/25 (F5 회복) |
| limit=10 청크 비율 | 2/10 | ≥ 5/10 |
| 메모리 행 | 3~4 | 1 이상, 과반 미만 |
| 모듈 순위 6쿼리 | 전부 1위 | 유지 |
| rrf 0.0 행의 1위 노출 | 발생 | 0건 |

### 구현 결과 (2026-09-01)

`slot_planner.py` 신설(중앙 배분 + per-file cap 2), `_interleave_modules`
순수 레이아웃화, 오케스트레이터 배선. 실측:

- **청크 비율 5/10 달성** (S1: 2→5, 목표 구성 5/3/1/1 그대로),
  distinct files 9~10/10 (S1은 한 파일이 4칸 도배 → 1칸).
- **gold 17/25 무회귀** — 그러나 F5 미회복. 재측정 결과 **핸드오프의
  전제("자리가 없어 밀렸다")는 더 이상 성립하지 않는다**: F5 정답 문서는
  bdf9bbe(모듈 이름 벡터) 이후 자기 청크로는 top-50에 랭크가 없고
  module_member로 27위에만 나타난다. 자리 문제가 아니라 검색 문제 —
  잔여 miss(F5·S1·S3)의 원인은 ① valuein 인덱스의 archive/ 오염
  (S1 3위가 archive/dead-code 카드; 후기 v3 지적과 동일 — 단
  `docs/plans/_archive/`는 S3의 정답이 사는 곳이므로 루트 `archive/`만
  제외할 것) ② 문서 청크 자체의 랭킹 부족(WS2·WS4 영역).
- 한계 명시: 메인 레인에서 자기 랭크를 얻은 qa 행은 청크 스트림 안에
  남는다(F5에서 mem 2칸). 시도 3의 함정(스트림 내 메모리 skip → 전멸)을
  피하기 위한 의도적 보수 — 총구성 캡은 벤치 누적 후 재평가.

**후속 (같은 날): valuein 루트 `/archive/` 제외 + 재인덱싱 → gold
18/25 (유효 17/19), F5 회복. WS1 완료 기준 달성.** archive 309파일
제거가 랭크 공간을 풀자 F5 정답 문서가 top-10에 진입했다 — SlotPlanner
(노출)와 오염 제거(랭킹)의 합작. 잔여 miss S1·S3은 문서 랭킹 문제로
WS2·WS4에서 다룬다. valuein 쪽 변경은 그 레포의
`.hybrid-search-ignore`에 있다(루트 앵커 `/archive/`,
`docs/plans/_archive/`는 보존 — S3 정답과 genesis 문서가 산다).

---

## 3. WS2 — Graph-hop 후보 확장 (LARGER 패턴)

### 문제
멀티홉·구조 질문("누가 호출", "의존 경로")에서 벡터·BM25가 원리적으로
약하다. 풀 GraphRAG는 과잉(구축 57배 비용, KG 커버리지 65.8% 한계).

### 설계
LARGER(2605.16352)의 공식을 기존 자산으로 구현: **lexical/vector 상위
히트를 시작점으로, 기존 call graph에서 1홉(caller/callee/import)을
후보군에 합류**시킨다. graph_card(질문 트리거 시 카드 주입)는 유지하고,
확장은 모든 검색의 후보 단계로 승격한다.

- 확장 대상: RRF 상위 `k=5` 히트 중 코드 청크만.
- 폭증 방지: 히트당 이웃 ≤ 3, 확장 총량 ≤ limit. 이웃은 별도 레인이
  아니라 **일반 청크 후보로 합류하되 출처 표기**(`via_graph_hop: true`)
  — RRF 융합이 최종 순위를 정하게 둔다 (RANGER: 그래프는 대체재가 아닌
  보완재).
- 신뢰도 필터: 이웃 청크와 질의의 코사인 하한(캘리브레이션에서 도출)
  또는 시작 히트와 동일 모듈일 것.

### 완료 기준
holdout에 멀티홉 슬라이스(§7) 신설 후 전후 비교. 멀티홉 정답률 상승 +
비-멀티홉 gold 무회귀 + p50 latency 증가 ≤ 20%.

### 구현 결과 (2026-09-01)

`search/graph_hop.py` 신설 + `_expand_graph_neighbors` 배선. 신뢰도
필터는 새 임계값 대신 **기존 엣지 confidence 등급(inferred+)을 재사용**
(D1 준수). 이웃 점수는 출처 히트의 0.5배(RRF 스케일 유지), 안정 삽입
병합으로 기존 순서 불교란. 킬스위치 `HYBRID_SEARCH_GRAPH_HOP=0`.
선행 조건 D4 검증 완료: 델타 경로가 caller 기준 삭제+재삽입으로
call_edges를 갱신하고, 파일 처리 시마다 resolve_call_edges가 돈다.

- **멀티홉 슬라이스 신설**(benchmarks/multihop_gold.json, 6문항 —
  정답 파일은 call_edges 실측으로 검증): A/B **4/6 → 5/6**
  (MH4 classify_confidence 소비처 회복).
- valuein gold **18/25 유지** (무회귀), latency 증가 없음 실측.
- 미회복 MH3: 정답(hook_runtime.py)보다 테스트 파일 호출자들이 같은
  엣지 confidence로 per-hit 캡 3을 선점. 프로덕션-우선 휴리스틱은
  벤치 과적합 위험이 있어 보류 — harvested 케이스가 쌓이면 재평가.

---

## 4. WS3 — QA Reflector: 통합·supersede·모순 해소

### 문제
QA 로그가 append-only로 쌓인다. near-duplicate·stale이 최신을 이기는
랭킹 경쟁(Hindsight의 3대 실패 중 2개)이 이미 우리 레인에서 관찰된다
(valuein 후기: 무관 qa가 top-3).

### 설계
주기 실행(기존 maintain 스킬 + post-commit 백그라운드에 편승 —
zero-touch 원칙, 새 트리거 없음):

1. `_qa_topic_groups`(기존)로 토픽 클러스터를 얻는다.
2. 클러스터 내에서 LLM이 병합 후보·supersede 관계·모순을 판정한다.
3. 산출물은 **원본을 건드리지 않고** `qa-consolidated/`에 쓴다. 각
   통합 노트는 frontmatter에 `sources: [qa ids]`, `supersedes: [...]`를
   강제한다 (불변식 4).
4. 결과는 git diff로 나온다 — 커밋 전 사람이(또는 세션이) 리뷰할 수
   있다 (Dreaming의 "리뷰 가능한 diff" 방식, poisoning 방어).
5. 검색에서 통합 노트는 memory 레인에서 원본보다 우선하고, 원본은
   supersession 체인으로 접근 가능하게 남는다 (Eywa: evidence before
   belief — 증거는 보존, 믿음은 승격).

기존 `supersession.py`(신규 답이 구식 답을 이김)가 이 구조의 절반이다.
Reflector는 그 관계를 **읽기 시점 추론에서 쓰기 시점 기록으로** 옮긴다.

### 완료 기준
memory 레인 노이즈율(무관 qa가 top-3에 드는 비율)을 selfeval 이벤트로
전후 측정. 통합 노트의 provenance 누락 0건(테스트 강제).

---

## 5. WS4 — insight 위키 + 앵커 + rationale 레인

### 문제
(a) 위키가 코드 재서술이면 raw와 임베딩 공간에서 경합한다 — negative
transfer(MTL)이자 83% 오염의 원리. (b) 위키 주장은 소스가 바뀌어도
살아남아 조용히 썩는다(EA-Graph: "빠진 것은 스스로 알리지만, 낡은 것은
알리지 않는다"). (c) 프로젝트의 최대 why 자산인 긴 서술형 주석이 코드
청크에 묻혀 검색에 안 잡힌다(valuein 후기 ④, f32 genesis 실측).

### 설계
셋을 한 묶음으로:

1. **위키 생성 프롬프트 재규정**: 재서술 금지. 설계 이유·불변식·함정·
   트레이드오프만. "코드가 스스로 말할 수 있는 것은 쓰지 않는다."
2. **주장 앵커**: 위키 각 섹션 frontmatter에 `anchors: [file 또는
   commit]`. delta reindex가 앵커 파일 변경을 감지하면 해당 섹션을
   affected / unaffected / unprovable로 3분류하고 affected만 재합성
   큐에 넣는다. 기존 stale-wiki 갱신·좀비-wiki 삭제 로직의 확장이다.
3. **rationale 청크 레인**: AST 청커가 긴 서술형 주석 블록(임계: 서술
   문장 N자 이상, docstring/블록 주석)을 코드와 **별도 청크**
   (`node_type: rationale`)로 추출한다. 코드 청크에는 그대로도 남는다
   (동거 원칙). 검색에서 rationale은 memory 레인 대우를 받아 why 질의
   가중치를 받는다.
4. **노출**: WS1의 SlotPlanner에 wiki·rationale 소수 슬롯을 배정한다.
   합성물 표기(불변식 2)와 청크 과반 계약이 오염 재발을 구조로 막는다.

### 완료 기준
genesis 슬라이스(§7)에서 why 질의 정답률 전후 비교 — f32 케이스(정답
커밋이 top-5 밖)가 1호 회귀 항목. 앵커 3분류의 단위 테스트.
generated_ratio 경고 문구 재검토(슬롯 배분이 원인이었음을 반영).

---

## 6. WS5 — typed facts 테이블 (최후순위)

### 문제
집계("X가 몇 개"), 시간 델타("Y 이후 뭐가 바뀜"), 부정은 top-k 검색으로
원리적으로 불가.

### 설계
DB 교체 없이 기존 SQLite에 테이블 하나:

```sql
facts(id, subject, predicate, object,
      valid_from, valid_to,          -- Graphiti의 interval 무효화 차용
      source_chunk_id, provenance)   -- 불변식 4
```

- 추출원: commit 청크(이미 파싱함), WS3 통합 노트, 모듈 레지스트리.
  새 사실이 옛 사실과 충돌하면 옛 행의 `valid_to`를 닫는다(삭제 금지).
- 검색: router가 집계/시간 신호를 감지하면 **고정 템플릿 쿼리**
  (v1은 2종: "~이후 변경" / "~는 몇 개")를 실행해 결과를 응답에 별도
  섹션으로 첨부한다. 자유 생성 금지(불변식 3).
  **부정 질의는 v1에서 제외한다** — facts 테이블은 존재하는 사실만
  담으므로 부재를 단언할 수 없다(closed-world 가정 불가). 부정 단언이
  필요해지면 커버리지 메타(무엇을 스캔했는지)와 함께 별도 설계.
- TypeDB·그래프 DB·Kuzu류는 채택하지 않는다 (서버 전용·벡터 부재·
  거버넌스 리스크 실증).

### 완료 기준
템플릿 3종의 골드 질의 세트(각 5문항) 신설, 정답률 ≥ 4/5.
템플릿 미트리거 질의에 대한 무영향(오발동률 측정).

---

## 7. 관통 게이트 — holdout 벤치 확장

모든 WS의 판정 기준. 남의 점수는 쓰지 않는다.

| 슬라이스 | 재료 | 검증 대상 |
|---|---|---|
| 기존 gold | valuein 25문항 (유효 19) | WS1 무회귀 |
| harvested | selfeval이 실사용에서 자동 수확한 배신 케이스 | 전 WS (쓸수록 증가) |
| 멀티홉 (신설) | "누가 호출/의존 경로" 유형 ~10문항 | WS2 |
| genesis (신설) | why 질의 ~10문항, f32 케이스가 1호 | WS4 |
| facts (신설) | 집계·시간·부정 15문항 | WS5 |

보고 형식은 accuracy × tokens-to-answer × latency 삼중 기록(업계 수렴
표준). 하니스는 레포에 공개 유지 — 재현 러너 없는 점수는 불신받는
규범이 우리 무기다.

---

## 8. 실행 순서와 의존성

```
WS1 SlotPlanner ──┬──> WS2 graph-hop ──> (독립)
   (모든 것의 선행)  ├──> WS4 위키·rationale (노출 슬롯이 WS1에 의존)
                  └──> WS3 Reflector ──> WS5 facts (추출원으로 WS3 산출 사용)
```

- **WS1이 무조건 먼저다.** 노출이 고장난 상태에서는 어떤 개선도 측정이
  왜곡된다 (문헌: 검색 20pt vs 저장 3–8pt). 설계 진단은 이미 완료
  (2026-08-29 핸드오프), 남은 것은 구현.
- WS2는 WS1 직후 최고 가성비 (기존 call graph 재사용, +13.9pt 근거).
- WS3·WS4는 병렬 가능. WS5는 WS3 산출물을 추출원으로 쓰므로 마지막.

## 9. 하지 않을 것 (명시적 컷)

- GraphRAG 풀 도입 (엔티티 KG 구축·커뮤니티 요약) — 비용 57배, 좁은 이득
- temporal KG / Zep Graphiti 모델 — 독립 검증 붕괴 (84→58%)
- TypeDB·Neo4j·Kuzu류 외부 DB — 제약 충돌 + 거버넌스 리스크
- 자유 text-to-query (Cypher/SQL 생성) — 60%대 정확도
- 새 MCP 도구 — 기존 원칙 유지
- LongMemEval v1 점수 경쟁 — 포화·불신. V2 축(workflow/gotcha)은 §7
  genesis·harvested 슬라이스가 흡수한다

---

## 10. 라운드1 외부 심사 반영 (DeepSeek, 2026-09-01)

판정: **수정 후 착수** (재설계 불요). 처리표:

| # | 지적 | 처리 | 반영 |
|---|---|---|---|
| D1 | WS1 "재료 부족"·memory_intent 캡 해제 기준 모호 | **수용** | §2 본문 수정 — 랭크 존재 여부로만 판정, 판정 주체는 기존 토큰 분류기, 캡 해제 시에도 청크 1칸 보장 |
| D2 | rrf 0.0 규칙의 강제 지점 부재 | **수용** | SlotPlan이 레인별 "개수"만이 아니라 **행 선별까지** 반환한다 — 이후 단계는 SlotPlan의 행을 재배열만 할 수 있고 교체 불가. 테스트로 강제 |
| D3 | supersession이 planner 이후 발생하는 불일치 | **수용(이미 §2에 예약 슬롯로 존재)** | 예약 슬롯 미소진 시 청크로 환원한다는 문장 추가로 명확화 |
| D4 | WS2 call graph 최신성 가정 | **수용** | 착수 전 확인 항목: delta reindex가 call_edges를 갱신하는지 검증, 미갱신이면 WS2 선행 조건으로 편입 |
| D5 | WS3∥WS4 레인 경합 (통합 노트 vs rationale) | **수용** | Reflector의 대상은 **qa 레인만**으로 한정. rationale·위키는 Reflector 대상 밖(소스가 코드/커밋이므로 앵커 무효화(WS4)가 담당). 경합 자체가 소멸 |
| D6 | Reflector 멱등성/중복 실행 | **수용** | 기존 reindex lock 패턴 재사용 + 통합 노트에 입력 qa id 집합 해시 기록(같은 입력 재실행 = no-op) |
| D7 | 재서술 금지 프롬프트의 검증장치 부재 | **수용** | 합성 후 자동 검증: 위키 섹션과 앵커 코드 청크의 코사인 상한 게이트(초과 = 재서술로 판정, 반려). 임계는 오염 사고 데이터로 캘리브레이션 |
| D8 | facts로 부정 표현 불가 | **수용** | §6 수정 — v1 템플릿 2종으로 축소, 부정 제외 및 사유 명기 |
| D9 | 불변식 2·5 충돌(합성물이 BM25 순수성 훼손?) | **용어 명확화로 해소** | "레인"은 코퍼스 분리가 아니라 **랭킹·배분 라벨**이다. 모든 청크는 같은 인덱스에 살고, node_type이 배분(WS1)과 표기(trust_meta)를 결정한다. BM25 레인의 순수성이란 개념은 설계에 없음 |
| D10 | 불변식 7의 WS1 순환 의존 | **수용(재정의)** | WS1의 게이트는 "수리 후 gold 무회귀 + 슬롯 구성 지표(청크 비율·rrf0.0 노출 0)"의 사후 측정. 전후 비교는 WS2 이후부터 적용 |
| D11 | 롤백 계획 부재 | **수용** | 공통 규칙 신설: 각 WS는 독립 커밋(들)로 완료 기준 미달 시 revert 가능해야 하며, 스키마 변경(WS5)은 additive-only(테이블 추가만, 기존 테이블 불변)로 revert 안전성 확보 |
| D12 | LLM 비용 예산 부재(WS3·4·5) | **수용** | 각 WS 착수 전 예상 토큰량·금액 산정 후 실행("재인덱싱은 비용" 원칙). Reflector·재합성은 델타만 처리(전량 재처리 금지) |
| D13 | "gold 자체가 오염 인덱스 산물일 가능성" | **기각** | gold 정답은 사람이 정한 파일 경로이고, 오염은 이 레포 자기 인덱스 문제였음(수리·재빌드 완료). valuein gold와 무관 |
| D14 | diff 리뷰 과잉 | **부분 수용** | diff 산출은 유지(poisoning 방어·감사 추적), 단 **논블로킹** — 승인 대기 없이 적용되고 git이 사후 감사 계층. 원 설계 의도 명문화 |
| D15 | 앵커 3분류 과잉(unprovable 무행동) | **부분 수용(행동 정의로 해소)** | unprovable 섹션은 "미검증" 배지를 달고 재합성 큐 저순위 + 검색 노출 시 trust_meta에 표기. 2분류 축소는 기각 — "낡은 것을 아는 척"이 EA-Graph가 지목한 바로 그 실패 |
| D16 | valid_from/to 과잉 | **부분 수용** | v1 시간델타 템플릿은 commit 청크로 답 가능하므로 facts interval 의존 제거. interval 자체는 충돌 해소(D8의 supersede 판정) 기반이므로 스키마에는 유지 |
