# Agent Handoff Trust Bench — 설계

**Status:** PROPOSED — 2026-07-15
**목적:** "개발 맥락의 진실성"을 측정하는 공개 벤치마크. 일반 메모리 벤치
(LongMemEval/LoCoMo) 점수 경쟁을 하지 않고, 아무도 측정하지 않는 축 —
**에이전트 간 인수인계에서 폐기된 사실·미완료 작업·근거 없는 확신이 전달되는가** —
를 frozen 프로토콜로 측정한다. 벤치마크 자체가 moat.

---

## 1. 시나리오 모델

한 시나리오 = 실제 개발 사이클 1회를 스크립트화한 6단계:

```
S1. PLAN      Claude Code가 계획 수립 (요구사항 → 접근 A/B/C 중 선택)
S2. REVIEW    Codex가 계획 검토 — 일부 항목 기각/수정 요구
S3. IMPLEMENT Claude가 (수정된 계획대로) 구현
S4. FIND      Codex가 코드 결함 발견 (review_finding)
S5. FIX       Claude가 수정 + 커밋 — 이 시점 HEAD가 "현재 사실"의 기준
S6. PROBE     완전히 새로운 세션에서 질문 세트 실행 (여기만 채점)
```

핵심 설계 결정: **S1–S5는 기록 생성 단계, S6만 채점**. S6의 질문과 정답
앵커는 **S1 시작 전에 작성·동결** (frozen holdout 규칙과 동일 — 사후 수정 금지).

각 시나리오는 의도적으로 "함정"을 심는다:

- S2에서 기각된 옵션 (→ 현재 사실로 전달되면 stale-fact 오류)
- S4에서 발견됐지만 S5에서 **일부만** 수정된 결함 (→ 완료로 전달되면 false-completion)
- S5 커밋으로 무효화된 S1의 결정 (→ commit-aware invalidation 측정)
- 한국어 계획 + 영어 코드/커밋 (→ cross-language 측정)
- 어떤 기록에도 없는 질문 1개 이상 (→ abstention 측정)

## 2. 데이터 구조

### 시나리오 정의 — `benchmarks/handoff_bench/scenarios/<id>.yaml`

```yaml
id: hb-001-auth-refactor
repo: <frozen repo path or git URL>
base_commit: <sha>            # S1 시작 시점
language_mix: ko-plan/en-code # ko/en/mixed
steps:
  - step: S1
    agent: claude
    prompt: "인증을 미들웨어로 옮기는 계획을 세워줘..."
    plants:                    # 이 단계가 심는 함정
      - kind: rejected_later   # S2에서 기각될 옵션
        key: "옵션 B: 세션 테이블 재사용"
  - step: S2
    agent: codex
    prompt: "이 계획을 검토해줘..."
    plants:
      - kind: rejection
        rejects: "옵션 B"
        reason: "동시성 이슈"
  # ... S3–S5
probes:                        # S1 이전에 작성·동결
  - id: P1
    question: "현재 인증 처리 방식과 그 결정 이유는?"
    gold:
      current_fact: "미들웨어 방식 (옵션 A)"
      evidence_anchors: ["src/middleware/auth.ts", "commit:<S5-sha>"]
      must_not_contain: ["옵션 B가 채택"]   # stale-fact 함정
  - id: P2
    question: "남은 작업은?"
    gold:
      open_items: ["레이트리밋 미구현"]     # S4 발견, S5 미수정
      must_not_claim_complete: ["레이트리밋"]
  - id: P3
    question: "<기록에 없는 주제>"
    gold:
      expected: abstain        # weak + fallback이 정답
```

### 실행 결과 — `results/<run>/<scenario>/<system>/probes.jsonl`

```json
{"probe": "P1", "answer": "...", "confidence": "strong",
 "evidence_cited": ["src/middleware/auth.ts:42", "commit:abc123"],
 "tokens": 1840, "latency_ms": 2100}
```

### 채점 산출 — `scores.json`

```json
{"scenario": "hb-001", "system": "memory-layer-mcp",
 "current_fact_accuracy": 0.9, "stale_fact_error_rate": 0.0,
 "false_completion_rate": 0.0, "evidence_precision": 0.85,
 "handoff_completeness": 0.8, "unsupported_strong_rate": 0.0,
 "cross_language_success": 1.0, "abstention_correct": 1.0,
 "tokens_mean": 2100, "latency_p50_ms": 1900, "ttfv_min": 4.5}
```

## 3. 지표 정의 (채점 공식)

| 지표 | 정의 | 채점 방식 |
|---|---|---|
| Current-fact accuracy | gold.current_fact와 일치한 probe 비율 | LLM judge 3표 다수결 + 앵커 문자열 검사 병행 |
| Stale-fact error rate | must_not_contain 항목을 현재 사실로 진술한 비율 | 문자열/judge 병행 — **핵심 지표, 낮을수록 좋음** |
| False-completion rate | must_not_claim_complete 항목을 완료로 진술한 비율 | 〃 |
| Evidence precision | 인용된 파일:라인/커밋 중 실제로 주장을 뒷받침하는 비율 | 인용 앵커를 frozen repo에서 기계 검증 (파일 존재 + 심볼/커밋 대조) → 애매하면 judge |
| Handoff completeness | gold의 {목표, 결정, open_items, 기각 사유} 중 보존된 비율 | 항목별 recall |
| Unsupported-strong rate | evidence_cited가 비었거나 검증 실패인데 confidence=strong인 비율 | 기계 검증 |
| Cross-language success | ko-plan/en-code probe의 current-fact accuracy | 부분집합 집계 |
| Abstention correct | expected: abstain probe에서 실제로 weak/abstain한 비율 | 기계 검증 |
| Tokens / Latency | probe당 소비 | 실행 로그 |
| Time-to-first-value | 클린 설치 → 첫 유용한 회상까지 (분) | 시스템당 1회, 수동 프로토콜 |

**합성 점수는 만들지 않는다** — 단일 리더보드 숫자는 게임을 부른다.
지표별 표만 공개하고 해석은 독자에게.

## 4. 채점 코드 구조

```
benchmarks/handoff_bench/
├── scenarios/*.yaml          # frozen 시나리오 (해시 고정)
├── runner.py                 # S1–S6 실행: 에이전트 세션 오케스트레이션
├── adapters/                 # 비교 대상별 어댑터
│   ├── baseline_grep.py      #   AGENTS.md+CLAUDE.md+git log+rg (대조군)
│   ├── memory_layer.py       #   우리
│   ├── claude_mem.py         #   설치 가능하면 — 불가면 사유 공개
│   ├── agentmemory.py
│   ├── total_agent_memory.py
│   └── native_codex.py       #   Codex native memories
├── scoring.py                # 위 지표 계산
├── judge.py                  # LLM judge 3표 (프롬프트 동결, 모델·버전 고정 기록)
└── report.py                 # markdown 리포트 생성 (README 수치 단일 소스)
```

`scoring.py` 핵심 로직 (의사코드):

```python
def score_probe(probe_gold, answer_record, repo):
    stale_hits = [t for t in probe_gold.must_not_contain
                  if asserted_as_current(answer_record.answer, t)]  # judge
    evidence_ok = [a for a in answer_record.evidence_cited
                   if verify_anchor(repo, a)]                        # 기계
    unsupported_strong = (answer_record.confidence == "strong"
                          and not evidence_ok)
    ...
```

`verify_anchor`: `file:line`은 frozen repo에서 파일 존재 + 해당 라인 주변에
주장 키워드 존재를 검사, `commit:sha`는 `git show`로 diff에 주장 대상 포함
여부 검사. 기계 검증 불가 판정만 judge로 넘긴다 (judge 의존 최소화).

## 5. Frozen 프로토콜 (신뢰 자산의 조건)

1. **사전 동결**: probes + gold는 S1 실행 전 작성, 시나리오 YAML의 해시를
   커밋. 결과를 본 후 시나리오·임계값·judge 프롬프트 수정 금지.
2. **단일 실행**: 시스템당 시나리오당 1회. cherry-picking 금지. 재실행은
   버전 명시한 별도 리포트로.
3. **실패 공개**: 우리 점수가 baseline(grep+git log)에 지는 지표도 그대로 공개.
   대조군에 지는 축이 있다는 것 자체가 벤치 신뢰성의 증거.
4. **경쟁 시스템 공정성**: 각 어댑터는 해당 프로젝트의 공식 설치 문서대로만
   구성. 설정 실패 시 "미측정 + 사유"로 표기 (불리한 추정치 기재 금지).
   가능하면 해당 프로젝트 메인테이너에게 설정 리뷰 요청 (공개 이슈로).
5. **비용 공개**: judge 포함 실행당 총 토큰/API 비용 명시.
6. **자기 벤치 이해상충 명시**: 리포트 첫 줄에 "이 벤치는 memory-layer-mcp
   메인테이너가 만들었다"를 못박고, 시나리오·채점 코드 전체 공개로 상쇄.

## 6. 규모와 단계

- **v0 (2주)**: 시나리오 3개 × 시스템 3개(baseline / native / 우리) —
  파이프라인 검증이 목적. 이 단계 결과는 공개하되 "pilot" 라벨.
- **v1 (46–90일 창)**: 시나리오 20개 (ko/en/mixed 배분, 저장소 2개) ×
  시스템 6개. 디자인 파트너 5–10명의 실제 시나리오 기부를 받으면 별도
  "field" 슬라이스로 추가.
- 시나리오 저장소는 holdout 규칙 준수: 우리 개발에 사용한 적 없는 신규 저장소
  (httpx/ripgrep은 burned — 사용 금지).

## 7. 리스크

- **runner의 에이전트 비결정성**: S1–S5 산출이 실행마다 달라짐 → 기록 생성
  단계는 1회 실행 후 트랜스크립트를 **동결 아티팩트**로 저장, 모든 시스템이
  같은 동결 기록을 ingest (시스템 간 차이는 ingest+retrieval에서만 발생).
  이것이 이 벤치의 가장 중요한 공정성 장치.
- **judge 편향**: judge 모델이 우리 답변 스타일에 유리할 가능성 → judge
  프롬프트에 시스템 익명화 (답변만 제시, 출처 시스템 미표기).
- **경쟁사 버전 드리프트**: claude-mem은 주 단위 릴리스 — 측정 시점 버전
  고정 기록 필수.
