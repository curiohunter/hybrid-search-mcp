# P0/P1 구현 명세 — Trust Layer 라운드

**Status:** PROPOSED — 2026-07-15
**입력:** 딥 리서치(2026-07-15, 21개 검증 클레임) + Codex 평가(연구 8/10, 경쟁분석 5/10 → 포지셔닝 수정)
**쐐기:** "Claude Code와 Codex 사이에서 계획·구현·리뷰 맥락을 이어주되, 모든 기억을
현재 코드와 커밋에 대조해 최신성·근거·불확실성을 함께 전달하는 개발 맥락 계층"
**북극성:** 설명 반복 ≈ 0, 잘못된 맥락 전달 ≈ 0

---

## P0-1. R1 — qa-lane supersession 노출 보장

**Status: IMPLEMENTED — 2026-07-15** (테스트 1210/1210, 신규 25)

구현 노트 (스펙 대비 변경점, 현장 캘리브레이션 결과):
- 신규 모듈 `memory/supersession.py` + `qa_supersession` 테이블 +
  `cli._run_qa_supersession`(reindex 후크) + `orchestrator._splice_superseding`.
- **인덱스 시점 그룹핑은 query-time보다 엄격하게** — 첫 실행에서 answer-only
  경로가 corpus-wide로 과다 그룹핑(98건 중 다수 오류 쌍) → ① 질문 경로 일치
  필수, ② 질문 단독 distinctive 2토큰 공유 필수, ③ machine payload
  (`<task-notification>` 등) 제외. 최종 48건, 표본 검사 통과.
  cross-language 쌍은 인덱스 시점엔 의도적으로 미매핑 (ADV3 레인 몫).
- **mark-only 경로 추가**: 교정본이 자력으로 stale 위에 랭크된 경우 splice
  없이 stale에 superseded 마커만 부착.
- splice는 confidence 분류 **후** 적용 (R1-T4 구조적 보장).
- E2E 실측: "계정 설정" 쿼리에서 stale qa rank2 + 교정본 미검색 → SPLICED-IN
  확인. 잔여 노이즈: 대화 턴 계열 저위해 쌍 일부 (P1 typed-memory에서 해소).
- R1-T3 gold set 벤치 재실행과 "해결" 선언용 3차 holdout은 다음 단계.

### 문제 (실측)

`memory_bench_v2_ripgrep_holdout_2026-07-13.md` R1: probe 문구가 구식 답변과
verbatim 일치 → old qa가 #1로 노출되는데, 교정 qa는 실제 코퍼스 히트에 밀려
**retrieval top-10에 아예 들어오지 못함**. 기존 supersession
(`orchestrator._merge_memory_results`의 topic-group representative,
`_order_qa_by_recency`)은 old/new가 **둘 다 검색됐을 때만** 동작한다 —
R1은 그룹에 old만 존재하므로 grouping이 아니라 exposure 갭.

### 설계 — supersession completion (read-time splice)

1. **인덱스 시점**: reindex 시 qa 코퍼스 전체에 `qa_topics.topic_group_indices`를
   돌려 topic group을 확정하고, 각 qa chunk에 대해
   `superseded_by: <newer chunk_id> | null`을 storage에 영속
   (신규 테이블 `qa_supersession(chunk_id, superseded_by, group_key)`).
   delta reindex에서는 신규/변경 qa가 속한 group만 재계산.
2. **쿼리 시점**: 최종 랭킹의 qa_log hit 중 `superseded_by`가 있고 그 대상이
   결과에 없으면, 대상 chunk를 DB에서 fetch해 **old의 바로 위 rank에 splice**
   (rank-bounded — `conv_in_flight.score_conv_in_flight`의 비교불가 점수
   splice 선례 재사용). old는 제거하지 않고 `trust_meta`에
   `[superseded → see above]`를 표기.
3. **경계**: splice는 qa-lane 내부 이동만 — 코드 chunk를 밀어내는 총 슬롯 수는
   불변(top-10 안에서 old가 차지하던 자리를 new+old 2개가 아니라, old 1개를
   new 1개로 대체 + old는 11위로 밀기. 결과 수가 limit 미만이면 old 유지).

### 판정표

| ID | 판정 기준 | 방법 |
|---|---|---|
| R1-T1 | planted R1 재현 케이스(구식 verbatim probe)에서 교정 qa가 old **위** rank로 노출 | 신규 회귀 테스트 `tests/test_supersession_splice.py` — holdout R1 구조 복제(코퍼스 crowding 포함) |
| R1-T2 | old qa 단독 노출(stale_only) 0건 | 동일 테스트: superseded_by 존재 시 new 미노출이면 fail |
| R1-T3 | 코드 recall 무회귀 | valuein gold set recall@10 Δ ≥ −0.02 |
| R1-T4 | false-strong 불변 | absent 9/9 weak 유지 (splice가 confidence 입력을 오염시키지 않음 — splice는 confidence 계산 **후** 적용) |
| R1-T5 | 지연 예산 | splice fetch ≤ +50ms p95 (chunk_id 단건 조회) |
| R1-T6 | delta reindex 정합 | qa 추가→그룹 재계산→`superseded_by` 갱신 E2E |

---

## P0-2. ADV3 — KO→EN cross-language retrieval

**Status: IMPLEMENTED — 2026-07-15** (신규 테스트 21)

구현 노트:
- `search/translation.py` (urllib 직접 호출 — SDK 무의존 컨벤션 유지),
  `orchestrator._cross_language_memory_results`. EN lane은 번역문 기준으로
  bm25_weight를 재분류.
- 킬 스위치 `HYBRID_SEARCH_TRANSLATION=0` + `tests/conftest.py`에서 스위트
  전역 차단 (테스트가 네트워크를 절대 못 침).
- 실측: cold 5.8s(번역 포함) → cached 1.3s. **스펙의 p95 +900ms 예산은
  비현실적이었음 — cold 1회 후 캐시로 상환하는 모델로 수정.** 번역 품질 실측 정확.
- ADV3-T1(실제 회수 품질)은 3차 holdout에서 검증.

### 문제 (실측)

한국어 probe가 영어 qa 메모리를 top-10에 전혀 회수하지 못함 (ripgrep holdout
ADV3, cleanrepro에서도 동일). BM25는 lexical 불일치로 당연히 실패, vector도
KO 질문 ↔ EN Q&A 텍스트 간 cosine이 임계 미달.

### 설계 — 쿼리 측 dual-query (인덱스 재빌드 불필요)

1. **감지**: `qa_topics._is_hangul` 기반 — 쿼리 토큰의 Hangul 비율 ≥ 0.3이면
   cross-language 후보.
2. **번역**: 임베딩 API와 동일한 OpenAI 키로 1회 경량 번역 호출(gpt-5-mini급,
   temperature 0, 프로젝트 원칙상 신규 상시 의존성 아님 — 임베딩과 같은 키/
   같은 장애 도메인). 프로젝트 root에 번역 캐시
   (`.hybrid-search/cache/query_translations.jsonl`, query_hash → en).
3. **retrieval**: 원 쿼리 lane + EN 번역 lane을 각각 BM25+vector로 돌리고
   RRF로 병합. EN lane은 **memory node_type 한정**으로 스코프를 좁혀
   비용/노이즈 통제 (코드 검색은 이미 cross-language가 동작 — 문제는 qa lane).
4. **degrade**: 번역 실패/타임아웃(800ms) 시 단일 lane으로 무손실 폴백 + 응답
   메타에 `cross_language_lane: skipped` 표기.
5. **역방향(EN→KO)**: 이번 라운드 범위 밖 — 수요 실측 후.

### 판정표

| ID | 판정 기준 | 방법 |
|---|---|---|
| ADV3-T1 | ADV3 구조 복제 케이스(KO probe, EN qa 코퍼스)에서 정답 qa top-10 진입 | `tests/test_cross_language_lane.py` — synthetic EN qa + KO probe |
| ADV3-T2 | KO 쿼리 무회귀 | valuein KO gold set Δ ≥ −0.02 (dual lane이 KO 결과를 밀어내지 않음) |
| ADV3-T3 | EN 쿼리 불변 | Hangul 미감지 시 코드 경로 완전 동일 (단일 lane) |
| ADV3-T4 | 번역 캐시 적중 | 동일 쿼리 2회차에 번역 API 0회 호출 |
| ADV3-T5 | 지연 예산 | dual lane p95 ≤ +900ms (번역 800ms + 병렬 retrieval) |
| ADV3-T6 | 장애 격리 | 번역 API 강제 실패 mock에서 단일 lane 결과 == 기존 동작 |
| ADV3-T7 | false-strong 불변 | EN lane 추가가 unanchored/corpus-absent 캡을 우회하지 못함 (`_cross_language_mismatch` 경로 회귀 테스트) |

---

## P0-3. Codex 1급 플러그인 설치 경로

**Status: IMPLEMENTED — 2026-07-15** (신규 테스트 11, 라이브 smoke 4/4 PASS)

구현 노트:
- `codex_plugin.py`: 매니페스트(`.codex-plugin/plugin.json`) + 레거시
  hooks.json/config.toml 병행 설치, `setup --codex`/`doctor --codex`,
  smoke 4종(훅/Stop 왕복+정리/양측 MCP/공유 root). smoke qa는 검사 후 즉시
  삭제 (코퍼스 오염 방지). teardown은 user-scope 매니페스트만 제거
  (프로젝트 파일 불변 — 기존 정책과 일치).
- CX-T5(맥미니 클린 5분 실측)는 수동 체크리스트로 남음.

### 문제

`codex_hooks.py`(SessionStart/UserPromptSubmit/Stop 핸들러)는 이미 있으나
설치가 별도 `install-codex-hook` 수동 단계 — Claude Code 대비 2급 경험.
쐐기가 "verified cross-agent handoff"인 이상 Codex 설치는 데모가 아니라 제품.

### 설계

1. `.codex-plugin/plugin.json` 패키지: hooks(3종) + `.mcp.json`(hybrid-search
   서버) + Codex용 search/maintain skill을 한 매니페스트로 번들.
2. `hybrid-search-mcp setup --codex` 단일 명령: 매니페스트 설치 → 훅 등록 →
   smoke test(아래) 자동 실행. 기존 `install-codex-hook`은 deprecated alias로 유지.
3. smoke test (`doctor --codex`): ① 훅 3종 등록 확인 ② 가짜 Stop 이벤트 주입 →
   qa 파일 생성 확인 ③ MCP search 1회 왕복 확인 ④ Claude Code 측과 같은
   `.hybrid-search/` root를 보는지 확인 (공유 메모리의 물리적 증명).
4. 목표 지표: 클린 맥(테스트: 맥미니)에서 **두 에이전트 연결까지 5분**.

### 판정표

| ID | 판정 기준 | 방법 |
|---|---|---|
| CX-T1 | `setup --codex` 멱등성 | 2회 연속 실행 시 중복 등록/에러 없음 |
| CX-T2 | smoke 4항목 전부 green | `tests/test_codex_plugin_setup.py` (훅 이벤트 fixture 주입) |
| CX-T3 | 공유 root 검증 | Claude 세션이 쓴 qa를 Codex 훅 컨텍스트 주입이 회수 (E2E fixture) |
| CX-T4 | teardown 대칭 | `teardown`이 Codex 매니페스트까지 제거 |
| CX-T5 | 수동 검증 | 맥미니 클린 설치 실측 ≤ 5분 (체크리스트 문서화, CI 외) |

---

## P1-1. Typed memory schema + write-time gate

**Status: IMPLEMENTED — 2026-07-15** (신규 테스트 17)

구현 노트:
- `memory/memory_types.py` 분류기(고정밀 편향 — 불확실하면 전부 inferred),
  QARecord v3 필드, trust_meta 노출.
- **스펙 변경: 기존 레코드 일괄 마이그레이션 안 함** — 전량 inferred 스탬프는
  기존 코퍼스 전체를 소리 없이 강등시키므로, legacy(필드 없음)는 현행 랭킹
  유지, 신규 레코드만 타입 부여.
- **리뷰 조건 반영 (2026-07-15 4라운드 사전 검토)**: legacy 우회 구멍 봉쇄 —
  strong 앵커는 `verified`/`accepted`만 허용. legacy(무필드)·inferred·
  needs_revalidation 전부 strong 불가(mixed 캡). 랭킹/검색/점수는 불변 —
  confidence 라벨만 제한.
- 승인 규칙은 문두 긍정/단독 진행/옵션 선택으로 한정 ("~해줘" 요청 전반 매칭
  버그를 테스트가 잡음).
- TM-T1(50건 수동 gold 80%)은 실사용 레코드 누적 후 측정 과제로 남음.

### 근거

MemGuard(arXiv 2605.28009, preprint 단서 유지): 기존 시스템 오답 분석에서
unverifiability 오류의 97.7%가 write-time 오염 연관 — read-time confidence만으로는
불충분. 자체 자기오염 감사(2026-07-09)의 결론(scanner→레인분리→confidence→
qa게이트)과 일치. 실사용 보고(claude-code#23769): 잘못 저장된 메모리 1건이
후속 세션을 연쇄 오염.

### 설계

1. `QARecord`(qa_log.py)에 frontmatter 2필드 추가:
   - `memory_type: observation | decision | hypothesis | task_state | procedure | review_finding`
   - `verification: verified | accepted | inferred | needs_revalidation | superseded`
2. **write-time 분류(휴리스틱, LLM 무호출)**:
   - `tools_used`에 실행 증거(Bash+테스트 통과 패턴, Edit+커밋) → `observation/verified`
   - 사용자 승인 패턴(질문→"응/진행해/좋아" 턴 구조) → `decision/accepted`
   - 그 외 모델 추론 → `hypothesis/inferred` (기본값 — 보수적)
   - Codex `review_finding`: codex_hooks Stop 이벤트에서 리뷰 판정 패턴 감지
3. **quarantine 규칙**: `inferred`는 confidence 계산에서 strong의 근거가 될 수
   없음 (`classify_confidence` 입력에서 가중 하향). `needs_revalidation`은
   기존 `_memory_status` 감쇠 경로(0.35×)에 합류하되 별도 계수(0.6×) +
   trust_meta에 사유 표기.
4. **마이그레이션**: 기존 qa는 `memory_type: observation, verification: inferred`
   기본값 — BF16 마이그레이션과 동일하게 첫 reindex에서 일괄, 재빌드 불필요
   (frontmatter만 갱신).

### 판정표

| ID | 판정 기준 |
|---|---|
| TM-T1 | 분류기 정확도: 라벨링된 기존 qa 50건에서 type 일치 ≥ 80% (수동 gold) |
| TM-T2 | `inferred` 단독 히트로 strong 불가 (unit: confidence 입력 조작 테스트) |
| TM-T3 | 마이그레이션 멱등 + 구버전 qa 파싱 하위호환 |
| TM-T4 | 민감 쿼리 게이트(`is_sensitive_query`)와 합성 시 누락 없음 |
| TM-T5 | write-time 오염 축 벤치: planted 오염 qa(허위 완료 보고) 주입 → strong으로 회수되지 않음 |

---

## P1-2. Commit-aware invalidation

**Status: IMPLEMENTED — 2026-07-15** (신규 테스트 12, 라이브 실측: HEAD 커밋의
SNS 초안 변경 → 해당 초안에 답한 옛 qa 8건 정확히 flagging)

구현 노트 (스펙 대비 변경점):
- **frontmatter 재작성 대신 `qa_revalidation` 사이드 테이블** — 파일을 고치면
  해시가 바뀌어 flagged 메모리 전부 재임베딩되는 비용 회피. enrich 시점 배치
  조회로 trust_meta에 `[needs_revalidation — <path> changed in <sha>]` 노출,
  0.6× 감쇠 + strong 차단은 P1-1 quarantine 레인 공유.
- anchor는 Top results 상위 3개 경로만 (deep rank 부수 회수로 인한 과잉
  무효화 방지). 커밋보다 나중에 쓰인 qa는 flagging 제외 (timestamp 가드).
- 커밋 누락 방지: `qa_reval_last_commit` meta로 (last, HEAD] 범위를 커밋별
  처리 (50개 캡).
- 해제 경로: 새 verified qa가 supersede(R1 인프라)하거나 chunk 삭제 시 orphan prune.

### 근거

경쟁 제품(conversational memory 계열)이 구조적으로 못 하는 것: "이 결정이
어느 커밋을 만들었고 **현재 코드에도 살아 있는가**". 우리는 commit 청크
(feature genesis)와 post-commit delta reindex를 이미 보유 — 연결만 남음.

### 설계

1. qa/card 기록 시 **anchor 수집**: top results의 file_path + 답변에 언급된
   파일/심볼(기존 cards.py `files:` 필드 확장) + 기록 시점 HEAD commit hash를
   frontmatter `anchors: {commit, files[], symbols[]}`로 저장.
2. post-commit reindex 훅에서 변경 파일 목록과 anchors 대조:
   - anchor 파일이 변경됨 → 해당 메모리 `verification: needs_revalidation`
     + `revalidation_cause: <commit hash>` 스탬프
   - anchor 파일이 삭제됨 → `superseded` 후보로 integrity pass에 위임
3. 검색 응답의 trust_meta에 노출: `[decision - needs_revalidation since def456]`
   — 에이전트가 "이 결정은 그 후 코드가 바뀌었다"를 인지하고 재검증.
4. 재검증 경로: 후속 세션에서 같은 topic의 새 qa가 `verified`로 기록되면
   supersession 그룹이 자연 해소 (P0-1 인프라 재사용).

### 판정표

| ID | 판정 기준 |
|---|---|
| CA-T1 | anchor 파일 수정 커밋 → 해당 메모리만 needs_revalidation (무관 메모리 불변) |
| CA-T2 | needs_revalidation 메모리는 strong 불가 + trust_meta 사유 노출 |
| CA-T3 | post-commit 훅 지연 예산: anchors 대조 ≤ +200ms (파일 경로 set 교집합) |
| CA-T4 | anchor 없는 구버전 qa는 경로 통과 (no-op) |
| CA-T5 | E2E: "결정 기록 → 관련 파일 수정 커밋 → 재질문" 시나리오에서 응답에 재검증 필요 신호 포함 |

---

## P1-3. Calibrated confidence — 통계 계약화

**Status: TOOLING IMPLEMENTED — 2026-07-15** (신규 테스트 15)

구현 노트:
- `src/hybrid_search/eval/calibration.py`(메트릭 라이브러리) +
  `benchmarks/calibration_report.py`(CLI). per-label precision **및
  coverage 동시** 보고, ECE/Brier(공표 nominal 0.95/0.60/0.20 기준),
  coverage-risk 곡선, corpus×language 슬라이스, 게이트(strong precision
  ≥0.95 ∧ coverage ≥20%) — 게이트 실패 시 exit 1 = "calibrated" 문구 사용 불가.
- **남은 것**: 실데이터 라벨 생성 파이프라인(confidence_eval 연동)과
  CC-T2 노이즈 마진(임베딩 5회 재샘플 flip율) — 3차 holdout 실행과 함께.

### 근거

Codex 지적 수용: false-strong 0/27은 "strong을 거의 안 주는" 게임으로도 달성
가능 — present 4/4 mixed가 그 신호. 'calibrated'를 주장하려면 coverage와 함께
측정해야 함. cleanrepro의 P4 flip(임베딩 비결정성으로 mixed→weak)은 경계값이
노이즈 폭보다 얇다는 실측.

### 설계

1. `benchmarks/calibration_report.py` (기존 confidence_eval.py 확장):
   - reliability diagram 데이터(신뢰도 bin별 실제 정답률)
   - ECE, Brier score, coverage-risk curve
   - confidence 라벨별: precision **과 coverage 동시 보고** (strong 비율 포함)
   - 저장소별(valuein/httpx/ripgrep) × 언어별(KO/EN) 분리
2. **노이즈 마진**: 경계 근처 케이스에 임베딩 5회 재샘플 → flip율 측정,
   경계값을 flip율 ≤ 5%가 되는 마진만큼 이동 (P4 flip 재발 방지).
3. **게이트(릴리스 기준)**: strong precision ≥ 0.95 **이면서** strong coverage
   ≥ 20% (answerable 질문 기준) — 한쪽만 최적화 방지. 미달이면 릴리스 노트에
   그대로 공개 (holdout 정직성 원칙).

### 판정표

| ID | 판정 기준 |
|---|---|
| CC-T1 | calibration_report가 3개 코퍼스에서 재현 가능 실행 (CI smoke: valuein만) |
| CC-T2 | 경계 flip율 ≤ 5% (5회 재샘플, P4 케이스 포함) |
| CC-T3 | strong precision ≥ 0.95 ∧ coverage ≥ 20% — 미달 시 실패가 아니라 **공개** (게이트는 마케팅 문구 사용 조건) |
| CC-T4 | 리포트 산출물이 README 숫자와 단일 소스로 연결 (수치 이중 관리 금지) |

---

## P2 (이번 라운드 범위 밖, 설계만 예약)

- **Verified Handoff Packet**: goal/approved_plan/completed/open_items/
  rejected_options/review_findings/current_commit/evidence/confidence의
  YAML 계약. P1-1 typed memory가 선행 조건 (packet 필드가 memory_type과 1:1 대응).
  → 별도 계획 문서로.
- Agent Handoff Trust Bench → `2026-07-15-agent-handoff-trust-bench.md`

## 실행 순서와 검증 규칙

1. P0-1 → P0-2 → P0-3 (각각 독립 PR, 4라운드 리뷰 프로세스 유지)
2. P1은 P0 머지 + 소프트 런칭 **후** 착수
3. R1/ADV3 수정의 "해결" 주장은 **세 번째 frozen holdout**(신규 저장소,
   KO→EN probe + write-time 오염 축 포함)으로만 — httpx/ripgrep은 burned,
   재실행은 회귀 확인용으로만 인용.
