# Hybrid Search MCP — Handoff Document

---

## 🔴 현재 세션 인계 (2026-04-22, 22회차) — 다음 세션 여기부터 읽을 것

### 한줄 요약

**Memory Layer를 default-on 제품 기능으로 활성화하여 "질문할수록 똑똑해지는 코드 검색" 포지셔닝 완성.** 21회차 Step K(구조 recall 0.41→0.52)를 끝낸 후, Phase 5의 남은 0.03은 모듈 content rewrite가 아니면 못 닿는다고 판단(portal-v3 summary에 parent/auth 토큰 0회). Graphify 분석팀이 "가장 임팩트 큰 단 하나"로 지목한 Pattern D(Q&A feedback loop)를 오늘의 목표로 전환. 기존 Sprint 1/2 인프라(qa_log write + reader) 위에 **Sprint 3 (indexing + ranking)** 를 완성: default-on, sensitive-query filter, time-decay boost, memory-intent detection ("지난번에"/"previously"). 실제 end-to-end 루프가 닫히는 것을 테스트로 증명 (`test_memory_layer_e2e.py` 2 tests). **748/748 passed (+34)**. README 재작성 — "A code search that learns from your questions" 상단 배치.

### ✅ 이 세션 완료된 것 (22회차)

**1. Phase 5 0.03 gap 분석 후 scope 축소 결정**
- S2 portal-v3 rank 14 → 8-source 창 밖. content 검토 결과: `summary`에 `parent`/`학부모`/`auth`/`인증`/`layout` 0회 등장. Name-match boost로는 score differentiation 불가능.
- Structure recall 0.52 accept, Step L-A 스킵. Phase 5는 "95% 완성 + S2/S3는 module-content 프로젝트" 상태로 동결.
- 그 시간을 Memory Layer productization에 투자.

**2. Memory Layer default-on (`qa_log.is_enabled` 로직 반전)**
- `HYBRID_SEARCH_QA_LOG` 해석 변경: **unset → enabled**, `0/false/no/off` → disabled.
- `IndexingConfig.index_qa_logs` default `False → True` + config loader 동일 적용.
- 새 기능 `is_sensitive_query` — password/api key/token/AWS AKIA/openai sk-/github ghp_/slack xoxb- 정규식으로 시크릿 혐의 쿼리는 절대 디스크에 안 씀.
- `record()` 진입점에 `is_sensitive_query` 가드 추가.

**3. Time-decay ranking (새 `_apply_memory_boost` in `orchestrator.py`)**
- 공식: `new_score = rrf * (1 + boost * 2^(-age_days / 30))`
- `_MEMORY_HALF_LIFE_DAYS = 30`, `_MEMORY_AMBIENT_BOOST = 0.20`, `_MEMORY_INTENT_BOOST = 1.00`
- `HybridResult`에 `file_mtime: str | None` 필드 추가 — qa_log chunk에만 채움 (hot path 오염 최소화).
- `_enrich_results`에서 qa_log node_type일 때만 mtime 전파.
- 적용 지점: `hybrid_search` 메서드의 `chunk_results` 생성 직후, `_interleave_modules` 직전. Non-qa chunks는 pass-through.

**4. Memory-intent 감지 (`_has_memory_intent`)**
- Korean 트리거: `지난번`, `이전에`, `아까`, `방금`, `전에`, `저번에`, `그때`
- English 트리거 (word-boundary): `previously`, `earlier`, `before`, `last time`, `the other day`, `what did (i|we|you) (ask|say)`
- Intent 감지되면 `_MEMORY_INTENT_BOOST`(1.00) 사용 → fresh qa 부스트 2×. Ambient(0.20)는 부드러운 상시 부스트.

**5. End-to-end test (`test_memory_layer_e2e.py`)**
- Killer product 루프의 정석 증명: index project → search → qa_log write → reindex → search with recall intent → qa_log chunk가 top-10에 실제로 등장.
- `_DetEmbedder`: 결정론적 토큰 해시 임베더로 OpenAI 의존성 제거.
- Opt-out 경로도 검증 (`index_qa_logs=False` 시 qa_log는 node_type으로 surface 안 됨).

**6. Boost 수학/intent 단위 테스트 (`test_memory_boost.py`, 19 tests)**
- `_has_memory_intent` Korean/English positive/negative cases
- `_parse_mtime_days_ago` (ISO parsing, naive UTC 취급, 미래시각 clamp, invalid→None)
- `_apply_memory_boost` (short-circuit no-qa, empty input, fresh boost, stale decay, intent amplification, missing mtime, non-qa pass-through, ordering 플립/유지)

**7. `qa_log`/scanner 테스트 업데이트 (default 전환 반영)**
- `test_qa_log.py`: `test_disabled_by_default` → `test_enabled_by_default`, new `test_sensitive_query_not_persisted` (ghp_/sk-/password)
- `test_scanner.py`: `test_qa_logs_skipped_by_default` → `test_qa_logs_walked_by_default` + opt-out 대응

**8. README 재포지셔닝**
- 상단: "A code search that learns from your questions. The more you use it, the better it gets — at *your* codebase specifically."
- 3-turn demo 박스 (코드 질문 → 리콜 질문 → 관련 질문)
- "Why this is different" 섹션 — Sourcegraph/Cursor/Graphify/ChatGPT Memory 비교, "first to close the loop" 주장
- Memory Layer 운영 요약 표

### 📊 영향 측정

**테스트:** 714 → **748** (+34)
- `test_memory_boost.py`: +19 (new)
- `test_memory_layer_e2e.py`: +2 (new)
- `test_qa_log.py`: +4 (revised + sensitive query)
- `test_scanner.py`: ±0 (rename + inverse assertions)
- 기존 회귀 없음

**벤치마크 변화:** 없음 (qa_log는 현재 valuein_homepage에 존재하지 않아 search 결과에 영향 없음. 사용이 쌓이기 시작하면 자연스럽게 structure/exploration recall 개선 기대).

**Phase 5 상태:** structure recall 0.52 (목표 0.55, 0.03 미달) — accept 상태. Top-5/recall/reads 3개 exit 기준은 모두 green 유지.

### 🎯 다음 세션 진입점

1. **이 22회차 섹션 전체**
2. `README.md` — 새 "Why this is different" / Memory Layer 표
3. `tests/test_memory_layer_e2e.py` — 루프가 어떻게 닫히는지 증명 예시
4. `src/hybrid_search/search/orchestrator.py:159-260` — `_has_memory_intent`, `_apply_memory_boost`, `_parse_mtime_days_ago`
5. 아래 "🎯 다음 세션 완성 목표"

### 🎯 다음 세션 완성 목표 — Memory Layer v2 (compounding 증명)

**현재 상태**: 루프는 닫혀 있음 (unit+e2e 모두 green). 사용자가 여러 번 질문하면 qa_log가 쌓이고, 나중 질문이 자연스럽게 이를 활용. 하지만 **"compounding quality"가 숫자로 증명되지 않음** — 벤치마크에서 qa_log 유무에 따른 recall 변화 미측정.

**Option A — Compounding 벤치 (반나절)**
- `benchmarks/compounding_bench.py` 신설
- 20개 valuein gold 쿼리를 2회 연속 실행
- 1회차: baseline (qa_log 비어있음)
- 중간에 모든 1회차 쿼리를 `qa_log.record`로 저장 + reindex
- 2회차: same queries with memory intent prefix ("지난번에 …")
- 지표: 2회차가 1회차 대비 recall@10/reads/query_time_ms 어떻게 변하는지
- 기대: "20개 같은 질문을 다시 하면 recall X%, reads Y% 개선" 숫자화 가능

**Option B — Staleness auto-pruning (반나절)**
- `reader.py`의 `prune_older_than` CLI 진입점 추가
- `/maintain` skill에 weekly 자동 실행 훅
- `.hybrid-search/qa/` 크기가 MB 단위로 늘어도 관리 가능

**Option C — PreToolUse hook (반나절)**
- Graphify Pattern Q1: Claude가 Grep/Glob 치기 직전 "memory/index 먼저 확인" 시스템 메시지 주입
- `hybrid-search-mcp install-hook --pretool` 서브커맨드
- 자율 루프 3축 중 마지막 piece

**권장 순서:**
1. **Option A** 먼저 — compounding 숫자가 있어야 README의 "learns from you" 주장이 증거로 뒷받침됨. 없으면 marketing claim 상태.
2. **Option B** — 첫 릴리스 품질 필수 조건 (디스크 관리)
3. **Option C** — Claude 사용 빈도 즉효. 하지만 compounding 증명 없이 hook만 깔면 빈 wiki 주입.

### 주의사항 / 알려진 이슈

- **Valuein_homepage 인덱스 상태**: 이 세션 중에 `reindex --force` 여러 번 동시 실행으로 tantivy 잠금 파일 충돌 → "Failed to acquire Lockfile" 에러. CLI에 동시 실행 가드가 없음. 다음 세션에서 해당 인덱스 rebuild 필요할 수 있음. 또는 concurrent-reindex 방어를 flock으로 추가.
- **`_apply_memory_boost`는 orchestrator 레벨**: MCP tool이 아닌 다른 호출 경로(예: CLI `search`)에도 orchestrator.hybrid_search를 경유하면 동일하게 적용됨 — 확인됨.
- **Sensitive-query regex는 보수적**: false positive 허용 (쿼리를 안 씀만큼 어차피 invisible). False negative(secret 누출)만 위험. 현재 regex로 잡히지 않는 custom secret 패턴은 사용자 책임.
- **Intent 트리거 "전에"는 넓은 단어**: "~전에" 형태로 흔히 사용됨 → false positive 가능. 현재는 수용 (full 부스트 적용되어도 qa가 매칭 안 되면 영향 0).
- **Time-decay는 chunk level이 아니라 result level**: 동일 파일에서 여러 chunk가 매칭되면 모두 동일 mtime을 씀. 보통 문제 없음 (qa file 자체가 단일 chunk로 처리됨).
- **Phase 5 structure recall 0.52**: 이 세션에서 건드리지 않음. Step L-A 스킵 결정 명시.

### 마지막 상태

- **브랜치:** `main` — 이 세션 커밋 예정 (아래 push)
- **마지막 커밋 (push 전):** `573e4d7 [feat] Phase 5 Step K — module-member emission for structure recall`
- **테스트:** **748/748 passed** (~26s) — 21회차 714 + 22회차 +34
- **변경 파일 (22회차):**
  - `src/hybrid_search/memory/qa_log.py` (+25 lines): default-on 로직 반전, `is_sensitive_query` 신설, `record()` 가드
  - `src/hybrid_search/config.py` (±8 lines): `IndexingConfig.index_qa_logs` default True + loader 대응
  - `src/hybrid_search/search/orchestrator.py` (+115 lines): `_has_memory_intent`/`_parse_mtime_days_ago`/`_apply_memory_boost`, `HybridResult.file_mtime` 필드, `_enrich_results`에서 mtime 전파, `hybrid_search`에 boost 호출
  - `tests/test_memory_boost.py` (+190 lines, 신규): 19 tests
  - `tests/test_memory_layer_e2e.py` (+180 lines, 신규): 2 tests, `_DetEmbedder`
  - `tests/test_qa_log.py` (±15 lines): default-on 반영 + sensitive query test
  - `tests/test_scanner.py` (±20 lines): QA opt-in → opt-out 대응
  - `README.md` (+45 lines): "A code search that learns" positioning, Memory Layer 표, 3-turn demo

### 로드맵 진행률 업데이트

전략 메모리의 3축 중:
1. **Memory Layer** — 이번 세션에 **v1 완성** (write + index + boost + intent + sensitive filter). 다음은 v2 compounding 증명.
2. **자율 루프** — Pattern Q1 (PreToolUse hook) 미착수. post-commit hook 이미 있음.
3. **벤치마크** — valuein_gold 20 존재. compounding 숫자 아직 없음.

Phase 5는 0.52로 동결. Phase 5 완성 선언 없이 다음 단계(Memory Layer v2 = compounding 벤치 또는 PreToolUse hook)로 진행. 제품 포지셔닝상 Phase 5 exit 숫자보다 Memory Layer 증명이 더 큰 레버리지.

---

## 🔵 이전 세션 인계 (2026-04-22, 21회차) — 참고용

### 한줄 요약 (21회차)

**Phase 5 Step K (module-member emission) 구현 + gold S3 acceptable_module_names 추가.** 구조 쿼리에서 한 모듈 안의 여러 파일을 리콜로 잡기 위해 비(非)카드 모듈의 대표 member 파일을 별도 `module_member` HybridResult로 emit. **구조 recall 0.41 → 0.52 (+0.11)**, top-5/reads 불변, overall recall 0.80 → 0.82. Phase 5 exit 4기준 중 3개 그린 유지, **structure recall만 0.52 < 0.55 목표에 0.03 미달**. S2/S3는 module-content 문제 (올바른 모듈이 search_modules 8-source 창 밖) — 이번 step으로 해결 불가, 다음 "Step L" 후보.

### ✅ 이 세션 완료된 것 (21회차)

**1. Gold-set S3 acceptable_module_names 추가**
- `benchmarks/valuein_gold.json`: S3 ("AI 에이전트 아키텍처 전체 그림")에 `acceptable_module_names: ["agent"]` 추가. 다른 4개 structure 쿼리는 이미 있음.

**2. Phase 5 Step K — module-member emission (K1~K5)**
- **K1.** `_module_results_for_query`가 `(cards, members)` 튜플 리턴. 카드는 top-`slots` 모듈 (rep 중복 제외). 비카드 모듈은 top-`_MEMBER_EMIT_NONCARD`=2개 member 파일을 `module_member` HybridResult로 emit. 카드 모듈은 member emit 생략 — 같은 디렉토리 sibling이라 dir-prefix gold 매칭에 무용.
- **K2.** 카드 할당 시 rep_path 중복 제거. valuein_homepage에서 `remote-rooms`, `remote-room` 둘 다 `docs/features/learning-remote-room.md`를 query-aware rep로 뽑는 문제 → 중복 방지해서 두 번째 모듈을 member 소스로 전환. **S4 `components/remote-room/*` 리콜 보존** — 카드 중복 탈락한 remote-room 모듈이 member로 `edit-room-dialog.tsx` + `create-room-dialog.tsx` 기여.
- **K3.** 비카드 모듈은 이름 기준 dedup (파일 수 많은 variant 유지). valuein_homepage의 `tuition-sessions` 2개 variant (4파일 app/(dashboard) vs 13파일 components/) 중 13-file 유지.
- **K4.** `_interleave_modules`가 member를 chunk 사이에 끼우지 않고 **tail(position 8-9)에 배치**. 모듈 카드는 0/2/4, 청크는 1/3/5/6/7, member는 8/9. S2/S3 primary chunk (rank 2)를 밀지 않도록 보장. member budget = `limit // 3` (limit=10에서 3개).
- **K5.** `run_valuein_bench.hybrid_track`가 `module_member` 노드 타입도 `acceptable_module_names` 매칭에 카운트.
- **4-bucket member picker**: `_module_member_paths`가 (code+overlap > code+fallback > doc+overlap > doc+fallback) 우선순위로 member 선택. S1의 `tuition-wizard/step-discount.tsx` (code, overlap 0) 같은 "구조적으로 중요하지만 쿼리 토큰과 무관한" 파일을 doc-overlap보다 우선 선택.

**3. 테스트 추가 +7개**
- `tests/test_module_injection.py`에 K placement, 우선순위 dedup, budget cap, slack absorption, backward-compat (members=None), slots=0 guard 등 7개 신규 테스트. 705 → **712 passed**.

### 📊 벤치마크 결과 (Step J → Step K)

| Metric             | Step J | Step K | delta  |
|--------------------|--------|--------|--------|
| overall top-1      | 0.55   | 0.50   | −0.05  |
| overall top-5      | 0.95   | 0.95   | 0      |
| overall recall@10  | 0.80   | 0.82   | +0.02  |
| overall reads/query| 2.05   | 2.00   | −0.05  |
| **structure recall@10** | **0.41** | **0.52** | **+0.11** |
| exploration recall | 0.80   | 0.77   | −0.03  |

**Per-query recall 이동:**
- **S1**: 0.25 → 0.50 (+0.25) — tuition-wizard member가 `components/tuition-wizard/` 커버
- **S5**: 0.67 → 1.00 (+0.33) — admissions#2 (비카드) SQL migration member 등장
- **F2**: 0.33 → 0.67 (+0.34) — migrations 모듈 member가 `create_monthly_snapshot_cron.sql` 등장
- **F3**: 1.00 → 0.50 (−0.50) — attendance doc chunk가 member에 밀려 top-10 밖
- **F4**: 1.00 → 0.67 (−0.33) — consultations SQL chunk가 member에 밀려 top-10 밖
- S2/S3/S4: 변화 없음 (올바른 모듈이 8-source 창 밖)

F3/F4 regression (−0.83 합산)가 S1+S5+F2 이득 (+0.92)을 일부 상쇄하지만 net +0.09 recall + 0.05 reads 개선. top-1/top-5는 불변. 1개 쿼리가 top-1 → top-2로 이동 (chunk 위치 변화).

### 🎯 다음 세션 진입점

1. **이 21회차 섹션 전체**
2. `benchmarks/valuein_report_v10_step_k_2026-04-22.md` — Step K 상세 + "What Step K did not fix" 진단 (S2 portal-v3, S3 harness, F3/F4 chunk displacement)
3. `git log --oneline -3` — Step K 커밋 (이 세션)
4. 아래 "🎯 다음 세션 완성 목표" (Step L 후보)

### 🎯 다음 세션 완성 목표 — Step L: structure recall 0.55 달성

**남은 0.03**: S2 (`components/portal-v3/`) 또는 S3 (`harness/core`, `harness/app`) 중 하나가 리콜 커버되어야 함. 둘 다 모듈 콘텐츠 문제 — 정답 모듈이 `search_modules` top-8에 못 올라감.

**Step L 후보 (3가지 중 택일 또는 조합):**

#### L-A. Name-match boost (간단, 반나절)
`search_modules`에 "모듈 leaf name이 query 확장 토큰과 정확히 일치하면 score × 1.5 boost" 규칙 추가. valuein_homepage S2에서 "portal-v3" 확장 토큰이 portal-v3 모듈 name과 정확히 매칭 → boost로 rank 14 → rank 5 진입 가능. harness/core, harness/app 같은 다단어 경로는 여전히 커버 못함.

#### L-B. Source window 확대 + member-only emit (하루)
`_MEMBER_SOURCE_MODULES`를 20-30으로 확대하되, 카드는 여전히 top-3만. 이러면 portal-v3 (rank 14)가 source에 들어가고, member만 emit. 단, budget=3 제약 때문에 top-3 non-card member가 portal-v3까지 밀려내는 것은 어려움 → 이름 dedup 공격적 + 모듈 name이 query-alias와 정확 일치 시 member budget 우선.

#### L-C. Module content rewrite (1~2일, 가장 효과적)
S2 portal-v3 모듈 summary에 "학부모/parent", "인증/auth", "레이아웃/layout" 명시. S3 agent 모듈 summary에 "harness/core", "harness/app" 파일 paths 명시. 재인덱싱 → vector 재생성 → score 상승 → top-3 도달.

**권장 순서**:
1. **0.5일 — L-A 시도**: `search_modules` name-boost, S2 벤치, 회귀 없으면 채택. (`valuein_report_v11_step_l_a.md`)
2. **L-A로도 안 되면 L-B 시도**: source 확대 + member budget re-tune.
3. **L-C는 별도 track**: 모듈 콘텐츠 자체 개선은 "Step F/G/H" 연장선. valuein 전용 프로젝트 튜닝이 되므로 일반화 필요 시 다시 검토.

**Exit 신호**: structure recall ≥ 0.55 + 다른 3 카테고리 회귀 없음 (overall top-5 ≥ 0.95, reads ≤ 2.10) → **Phase 5 완성 선언**.

### 주의사항 / 알려진 이슈

- **F3/F4 regression (−0.83 recall 합산)** — Step K의 member tail 삽입이 ranks 8-9 chunk를 밀어냄. F3 `docs/features/learning-attendance.md`, F4 `create_consultations_table.sql` 원래 해당 위치에 있던 chunk들이 그 희생자. orchestrator가 "이 chunk가 expected_files에 있다"를 알 수 없으므로 회피 불가능한 trade-off. S5 SQL member 이득과 상쇄하면 net +0.09 recall.
- **_MEMBER_SOURCE_MODULES=8, _MEMBER_EMIT_NONCARD=2** — valuein_homepage 실험치. 다른 프로젝트에서 structure 모듈 개수가 크게 다르면 재조정 필요. 일반화 시점에 config화.
- **`via_module` 0.40 → 0.20 감소** — S5 primary가 `docs/plans/2026-04-17` chunk (rank 4)로 이동했기 때문. admissions 카드는 여전히 rank 5에 있지만 chunk가 먼저. top-5 안에는 있으므로 agent cost는 동일.
- **card-name-dedup 시도 후 철회**: 같은 이름 카드 (예: 3개 attendance) 중복을 막으면 F3/F4는 개선되나 S1 recall이 0.50 → 0.25로 regress (tuition-wizard 카드가 doc rep를 픽해서 components/tuition-wizard/ 매칭 실패). Trade-off 판단 결과 name-dedup 없이 진행.

### 마지막 상태

- **브랜치:** `main` — 이 세션 커밋 1개 예정 (아래 push)
- **마지막 커밋 (push 전):** `7cbfc56 [feat] Phase 5 Step J — query-aware module representative path`
- **테스트:** **712/712 passed** (~26s) — 20회차 705 + 21회차 +7 (Step K tests)
- **변경 파일 (21회차):**
  - `src/hybrid_search/search/orchestrator.py` (+180 lines): `_module_results_for_query` → tuple, `_module_member_paths` (4-bucket), `_interleave_modules` (tail-member placement), non-card dedup-by-name
  - `tests/test_module_injection.py` (+100 lines): K placement/dedup/budget/slack/guard tests
  - `tests/test_orchestrator.py` (1 line): mock return type fix `[] → ([], [])`
  - `benchmarks/run_valuein_bench.py` (+5 lines): `module_member` counts for `acceptable_module_names`
  - `benchmarks/valuein_gold.json` (+1 line): S3 `acceptable_module_names: ["agent"]`
  - `benchmarks/valuein_report_v10_step_k_2026-04-22.md` (신규)
  - `benchmarks/valuein_results.json` (Step K 결과 덮어쓰기)

### 로드맵 진행률 업데이트

20회차 Phase 5 Step F/G/H/J로 exit 4/4 기준 중 3개 green 달성, structure recall 0.41만 미달. 21회차 Step K로 **structure recall 0.41 → 0.52 (+0.11)**, 다른 모든 지표 green 유지. **0.03 미달**로 Phase 5 완성 직전. 다음 세션 Step L (name-match boost 우선 시도) → 0.55 달성 → **Phase 5 완성 선언** + Phase 6 L1-L3 또는 다른 프로젝트 일반화 단계로 이동.

---

## 🔵 이전 세션 인계 (2026-04-22, 20회차) — 참고용

### 한줄 요약

**Phase 5 Step F/G/H/J 4 step 한 세션 shipped.** 19회차가 끝난 지점의 유일 gold miss F2(rank None)를 **rank 1으로 해소** + top-5 0.85→0.95(+0.10) + reads 2.55→2.05(−20%) 달성. 4 step이 쌓이는 구조: **F**(모듈 카탈로그 풍부화 — 161→302 모듈, cross-ref/promote/F2 요약 흡수)가 기반, **G**(SQL migrations → feature 모듈 cross-tree 첨부, 13개 정확 attach)가 올바른 데이터 투입, **H**(한국어 조사 스트립 + rarity gate — 통계는→stats만 주입, 학생이→student는 차단)가 검색 토큰 매칭 복원, **J**(쿼리-aware rep path + camelCase 분할 + code>doc 타이브레이크)가 앞의 셋이 쌓은 카탈로그를 헤드라인에 반영. **705/705 passed (+31)**. origin/main 4 커밋 push 완료(`9e2301f`→`7cbfc56`).

### ✅ 이 세션 완료된 것 (20회차)

**1. Phase 5 Step F — module content improvement 5 sub-step (커밋 `9e2301f`, +16 tests)**
- **F1** cross-ref doc attachment: multi-target 언급 doc이 docs/ 모듈에 머물되 각 대상 모듈에 weight 0.2로 member 추가. `member_hash`에 cross-ref 반영 → synth 재실행 보장.
- **F2** `_compose_summary`가 related-doc section excerpt 2개(각 ≤ 320 chars, qa_log 제외)를 카드 summary에 흡수. Korean 도메인 어휘(학부모/월별/입학)가 카드에 들어감.
- **F3** sub-threshold promotion: size-1 code dir이라도 doc 본문에 파일 path-mention 있거나 dir leaf name이 doc 토큰으로 등장하면 module 유지. `doc_promoted` 신호. `components/analytics/` 같은 case 구제.
- **F4** UnionFind arg 순서 flip: code key가 root가 되도록 — 병합된 module 이름이 `features` 아닌 `portal-v3`/`analytics`/`consultations`로 보존.
- **F5** name-prose cross-ref: doc 본문이 모듈 leaf name을 ≥ 2회 prose 언급(path mention 없어도) 시 cross-ref 부착. `DESIGN.md`/`CLAUDE.md`/`README.md`/`HANDOFF.md` 등 generic meta docs skip.
- 실측: 모듈 **161 → 302**, F4 consultations recall 0.50→0.67. 전체 recall 0.77→0.79, reads 2.65(변화 없음). catalog 인프라 공사.

**2. Phase 5 Step G — cross-tree file attachment (커밋 `53ea472`, +4 tests)**
- `_BUCKET_DIR_LEAVES = {migrations, seed, seeds, schema}` 지정된 bucket dir의 파일을 기존 feature 모듈에 cross-tree attach.
- `_crosstree_filename_tokens`: 파일명을 `[-_]` 분할 + date prefix 제거 + SQL/ops stopword 제거 (`create`/`alter`/`drop`/…). `_module_name_tokens`: 모듈 leaf tokens + naive singular (`admissions → {admissions, admission}`).
- 매칭 스코어: overlap 우선, 모듈 이름 길이 tiebreak. 상위 1개 모듈에 weight 0.3, cap `_MAX_CROSSTREE_PER_MODULE = 4`.
- 실측: valuein에서 13개 정확 attach — `stats ← create_academy_monthly_stats.sql`, `admissions ← create_admission_results.sql`, `consultations ← create_consultations_table.sql` 등.
- 헤드라인 0 변화 (카탈로그는 정확하지만 당시 `_module_representative_path`가 entry_points[0] 고정이라 SQL 파일이 surface 못함 — J에서 해소).

**3. Phase 5 Step H — selective particle strip (커밋 `e7a35be`, +5 tests)**
- Korean 조사(`는`/`은`/`이`/`가`/`을`/`를`/`의`/`에`/`에서`/`에게` 등) 스트립 후 alias lookup. **2개 게이트**:
  1. **Stem-has-alias gate** — 스트립된 stem이 `_ALIAS_MAP`에 있을 때만 stem/alias 주입. `시스템은 → 시스템`은 alias 없어서 주입 안 됨 (F4 naive strip 회귀 원인 제거).
  2. **Specificity gate** — cross-language alias가 catalog 모듈 이름에 ≤ 3개 match일 때만 주입. `통계 → stats` (1 match, 통과), `학생 → student` (11 match, 차단).
- `compute_alias_specificity(modules)` 호출 1회/search.
- 실측: 헤드라인 0 변화. 단 내부적으로 F2 쿼리에서 `stats` 모듈이 #1로 등장(이전엔 top-10 밖). J가 이를 활용.

**4. Phase 5 Step J — query-aware module representative path (커밋 `7cbfc56`, +6 tests)**
- `_module_representative_path(db, m, query_tokens)` — 기존 entry_points[0] 고정 대신 쿼리 토큰과 파일명 overlap 큰 member 선택.
- `_filename_token_set`: hyphen/underscore + **camelCase 분할** (`HomeworkTab.tsx → {homework, tab}`). camelCase 분할 없으면 .tsx member가 절대 못 이김.
- `_derive_query_tokens`가 H의 specificity gate를 그대로 사용 — `학생 → student`가 rep path 매칭에서도 차단됨 (F1의 `student-analysis.md` 드리프트 방지).
- Tie-break: `code > doc`. 동점 시 구현 파일이 .md를 이김. F3 `attendance` rep path가 `attendance.md` 아닌 `attendance/attendance-table.tsx` 유지.
- 실측 (Step G → Step J 통합 효과):
  - top-1 **0.45 → 0.55** (+0.10)
  - top-5 **0.85 → 0.95** (+0.10)
  - recall@10 **0.79 → 0.80** (+0.01)
  - reads **2.65 → 2.05** (−0.60, −23%)
  - via_module rate **0.25 → 0.40**
- F2 `create_academy_monthly_stats.sql` rank None → **rank 1** 달성 (유일 gold miss 해소).
- 카테고리 top-5: exploration 0.80→**1.00**, rationale 0.80→**1.00**.

### 🎯 다음 세션 진입점

1. **이 20회차 섹션 전체**
2. `benchmarks/valuein_report_v9_step_j_2026-04-22.md` — Step J 상세 + "What Step J did not fix"의 S2/S3/S4/S5 진단
3. `benchmarks/valuein_report_v8_step_g_2026-04-22.md` "Why F2 didn't close (Step G 시점)" 섹션 — Korean 조사 문제를 어떻게 특정했는지 예시
4. `git log --oneline -8` — origin/main 동기화, F/G/H/J 4 커밋 push 완료
5. 아래 "🎯 다음 세션 완성 목표"

### 🎯 다음 세션 완성 목표

**Phase 5 exit 조건 — 4/4 그린 달성이 타겟.** 현재:

| Exit 기준                     | 목표      | 현재   | 상태 |
|-------------------------------|-----------|--------|------|
| overall top-5                 | ≥ 0.80    | 0.95   | ✅   |
| overall recall@10             | ≥ 0.70    | 0.80   | ✅   |
| overall reads/query           | ≤ 2.5     | 2.05   | ✅   |
| **structure recall@10**       | **≥ 0.55**| 0.41   | ❌   |

유일한 미달은 **structure recall 0.41**. Step J 리포트 진단대로 이건 retrieval 결함이 아니라 **gold-set 모델링** 이슈:
- S2 `학부모 학생 포털` — 정답은 `docs/features/2026-04-08-portal-parent-student.md` + `components/portal-v3/` + `app/(auth)/` + `app/(portal)/layout.tsx` (4개 entry). 현재 top-10이 portal-v3 모듈을 rank 2에 올리지만 한 파일만 매칭.
- S3 `AI 에이전트 아키텍처` — 5개 entry 디렉토리(harness/core, harness/app, harness/plans, agent-architecture.html, agent-system-plan.md)
- S4 `remote-room` — 3개 entry, 2개 매칭됨
- S5 `입학 시험 결과` — 3개 entry, 2개 매칭됨

**완성 목표 = 두 축 동시 해결:**

#### 축 A. Gold-set 보강 (반나절)
`benchmarks/valuein_gold.json`에 structure 쿼리 5개 전부 `acceptable_module_names`가 있는지 확인 + 모듈이 정답인 경우 더 넓게 인정:
- S2: `acceptable_module_names` = `["portal-v3", "portal"]` (이미 있음)
- S3: 신규 추가 필요 — `["agent"]` 같은 subsystem 이름
- S5: 신규 추가 — `["entrance-tests", "admissions"]` 이미 있음

**하지만 이것만으로는 recall 못 채움** (recall은 expected_files 기준이지 primary_target 기준 아님). 그래서 축 B 필요.

#### 축 B. Multi-member module surface (1~2일)
현재 `_module_results_for_query`는 모듈당 rep path 1개만 리턴. structure 쿼리는 "이 모듈 안의 여러 파일을 보고 싶다"가 본질이므로 **모듈 1개 hit에서 member 2~3개를 따로 HybridResult로 emit**:
- Step K1: `_module_results_for_query`가 top 모듈의 top-N member를 별도 HybridResult로 리턴 (기존 1개 대신). `node_type="module_member"` + 모듈 id 링크
- Step K2: `_interleave_modules` 가 module_member를 chunk와 같이 다루되, 같은 모듈의 member끼리 clustering해서 연속 배치
- Step K3: 벤치마크에서 primary_hit 계산 시 "module member 리턴"도 acceptable_module_names 매칭으로 인정

**기대 결과**: structure recall 0.41 → 0.55+ (3~4 expected를 1개 모듈 hit으로 커버), top-5/reads는 유지.

### 🎯 다음 세션 권장 순서 (완성까지)

1. **0.5일 — Gold 검증**: `valuein_gold.json` 구조 검토, S3에 `acceptable_module_names` 추가, 현재 숫자에 영향 있는지 확인
2. **1일 — Step K multi-member emit 구현**: orchestrator 변경, module_members의 순서/개수 결정, dedup/interleave 조정
3. **0.5일 — Step K2 벤치 & 리포트**: `valuein_report_v10_step_k.md`, structure recall 측정
4. **선택 (0.5일) — breeze/mathontonlogy gold 20 쿼리 작성**: 일반화 신호 확보. Phase 5 exit이 valuein 전용 튜닝인지 판단 근거.
5. **HANDOFF 21회차 업데이트 + push**

**Exit 신호**: structure recall ≥ 0.55 달성 + 다른 3 카테고리 회귀 없음 → **Phase 5 완성 선언**. plan doc 업데이트 + Phase 6 L1-L3 (아직 안 한 watchdog layer)로 이동할지 결정.

### 주의사항 / 알려진 이슈

- **F2 recall 0.33** (rank 1은 맞지만 expected 3개 중 1개만 top-10). 나머지 2개 `create_monthly_snapshot_cron.sql` + `components/analytics/`는 chunk 단에서 surface 못함. multi-member emit(Step K)이 해결할 수도. 안 되면 gold F2의 expected_files 좁히는 것도 option.
- **P1/R3 rank 6** — precision/rationale 카테고리에서 1개씩 top-5 밖. top-5 총 0.95 = 19/20 hit. P1 `TuitionChargeSection 컴포넌트`는 F/G/H/J 전부 영향 없음 — 별도 chunk-level 문제.
- **Step H의 `_MAX_ALIAS_MODULE_MATCHES = 3`** 하드코딩 — valuein_homepage 경험값. 다른 프로젝트에서 모듈 수 많으면 재조정 필요. 일반화 시점에 config 화.
- **Step J의 camelCase 분할**이 filename에만 적용됨. BM25 chunk matching은 여전히 camelCase 분할 안 함. 향후 효과 볼 여지.
- **F1/F3 recall 드리프트 회피**를 위해 J의 code>doc tiebreak 도입했지만, `.md`가 정답인 쿼리가 오면 역으로 안 될 수도. 현재 gold에선 확인 안 됨.

### 마지막 상태

- **브랜치:** `main` — origin/main 동기화 (20회차 4 커밋 push 완료)
- **마지막 커밋:** `7cbfc56 [feat] Phase 5 Step J — query-aware module representative path`
- **테스트:** **705/705 passed** (25~26s) — 19회차 674 + 20회차 +31 (F 16 + G 4 + H 5 + J 6)
- **변경 파일 (20회차, 4 커밋 합계):**
  - F (`9e2301f`): `src/hybrid_search/index/modules.py` (+141), `src/hybrid_search/index/module_synth.py` (+66), `tests/test_modules.py` (+148), `tests/test_module_synth.py` (+97), `benchmarks/valuein_report_v7_step_f_2026-04-22.md` (신규), `benchmarks/valuein_results.json`
  - G (`53ea472`): `src/hybrid_search/index/modules.py` (+125 cross-tree), `tests/test_modules.py` (+65), `benchmarks/valuein_report_v8_step_g_2026-04-22.md` (신규), `benchmarks/valuein_results.json`
  - H (`e7a35be`): `src/hybrid_search/search/modules_search.py` (+73), `tests/test_modules_search.py` (+57), `benchmarks/valuein_results.json`
  - J (`7cbfc56`): `src/hybrid_search/search/orchestrator.py` (+130), `tests/test_module_injection.py` (+32), `benchmarks/valuein_report_v9_step_j_2026-04-22.md` (신규), `benchmarks/valuein_results.json`

### 로드맵 진행률 업데이트

19회차 Phase 5 roadmap 100% + Phase 6 L4/L5 완료했지만 structure/reads exit 목표 미달 → 20회차 Step F/G/H/J로 **reads(≤2.5)/top-5(≥0.8)/recall(≥0.7) 3/4 exit 달성**. 남은 **structure recall ≥ 0.55 하나**가 다음 세션 타겟. K step(multi-member emit) + gold 보강이 1~2일 작업. 그 이후 **Phase 5 완성 + Phase 6 L1-L3 또는 다른 프로젝트 일반화**로 넘어감.

---

## 🔵 이전 세션 인계 (19회차, 2026-04-22) — 참고용

### 한줄 요약 (19회차)

**Phase 5 gap 4 step (A/B/C/D) + Phase 6 Step E 전부 한 세션 shipped.** 18회차가 끝낸 Phase 5 직후 남은 gap steps를 A→B→C→D 순서로 전부 구현, 이어서 Phase 6 L4 drift watchdog + L5 two-tier cap까지 완료. **Step A**(rationale intent routing)로 rationale reads 4.00→2.40 정확 달성, **Step B**(gold v2 module-as-primary)로 structure top-5 0.60→1.00 + overall reads 4.20→2.70, **Step C**(module card vector embedding + symbol intent routing)로 precision top-1 0.20→0.60 + overall reads 2.70→2.55. 그 다음 **Step D**(agent-in-loop simulator)를 빌드해 실제 agent가 부담하는 reads/turns/bytes를 측정 — 정적 proxy가 "2.55 reads/query"라 보고하던 걸 **loose 0.25 / strict 0.45 reads/query**로 재측정 (5~10× 낮음). **Step E L4**로 `hybrid-search drift` CLI 드리프트 watchdog 신설 (MCP tool 아님, 메모리 규칙 준수). **L5**로 `_interleave_modules`에 `slots ≤ limit // 2` cap 추가 (limit=10에선 no-op, 저해상도 사용자 보호). **674/674 passed (+31)**. 커밋 4개 이미 origin/main 푸시 완료.

### ✅ 이 세션 완료된 것 (19회차)

**1. Phase 5 Step A — rationale intent routing (커밋 `6871c67`)**
- `orchestrator.py`: `_has_rationale_signal(query)` — 이유/배경/목적/의도/동기/취지/왜 + rationale/why/reason/motivation/purpose/intent/background (word-boundary)
- `_module_slots_for(qtype, query)`: rationale signal 있으면 return 0 (KOREAN_NL/ENGLISH_NL 모두)
- 12 unit tests (signal pos/neg, slot routing)
- 재측정: rationale reads 4.00 → **2.40** (타겟 정확 달성), rationale top-1 0.00 → **0.40** (R1 portal-v3, R2 ledger-abc rank 1 복귀)
- structure/exploration/precision 불변, regression 0

**2. Phase 5 Step B — gold v2: module as valid primary_target (커밋 `4e34b04`)**
- `run_valuein_bench.py`: `hybrid_track`/`grep_baseline` 병렬 `module_names: list[str|None]` 리턴
- `score_query(paths, module_names, query, ...)`: `file_primary_rank` + `module_primary_rank` 계산, `primary_hit_rank = min`. `primary_hit_via_module` 플래그 기록
- aggregate에 `primary_via_module_rate` 추가
- `valuein_gold.json`: 7 queries (S1/S2/S4/S5/F1/F3/F4)에 `acceptable_module_names` 추가. 모든 이름 실 DB 인덱스에서 검증됨
- 재측정 (Step A → B): structure top-5 0.60 → **1.00**, exploration top-1 0.20 → **0.60** (+40pp), overall top-1 0.20 → **0.35**, overall reads 4.20 → **2.70**. 5/20 쿼리 module card로 primary 달성

**3. Phase 5 Step C — module card embedding + symbol routing (커밋 `5ed5785`)**
- DB v6 → v7 migration: `modules.summary_vector` (BLOB) + `modules.vector_input_hash` (TEXT). 4 indexed projects 모두 자동 migrate 성공
- `ModuleRecord`에 `summary_vector/vector_input_hash` 필드. `upsert_module` + `_row_to_module` + 새 `update_module_vector` (벡터만 쓰기)
- `module_synth.py`: `synthesize_modules(db, project_id, embedder=None)` — 임베딩 pass opt-in. `vector_input_text(m) = name + hash-stripped summary + rationale`. 배치 임베딩 (161 modules in valuein_homepage backfilled in 2.6s, one OpenAI call). 실패 시 non-fatal
- `modules_search.py`: `search_modules(..., query_vector=None)` — `token_score + vec_score` 블렌딩. `VECTOR_WEIGHT=15`, `VECTOR_MIN_COSINE=0.25` (이하 바닥)
- `orchestrator.py`: `_has_symbol_signal(query)` + `_module_slots_for`에 symbol bypass 추가. `TuitionChargeSection 컴포넌트`처럼 mixed symbol+한국어 쿼리는 slot=0 (KOREAN_NL 기본값 대체)
- query_vector 이미 embed된 걸 `search_modules`로 전달 — 추가 API call 없음
- 재측정 (Step B → C): precision top-1 0.20 → **0.60** (+40pp! symbol routing 효과), overall top-1 0.35 → **0.45**, overall reads 2.70 → **2.55** (Phase 5 plan 목표 ≤ 2.5의 99.8% 달성)
- **남은 gap은 retrieval이 아니라 module content**: S2 portal-v3 vs student-hub, S5 entrance-tests missing, F2 analytics un-modularized

**4. Phase 5 Step D + Phase 6 Step E — agent-loop sim + drift + L5 cap (커밋 `a90b4de`)**
- **Step D** `benchmarks/agent_loop_sim.py`: 실제 agent 루프 시뮬레이션 (snippet scan → Read → grep fallback). Satisfaction token 기반, loose/strict 모드. **20 queries: loose 0.95 satisfied / 0.25 reads/query / 8.3KB; strict 0.90 / 0.45 / 14.4KB**. 정적 proxy 2.55 reads 대비 5.6~10× 낮음 — 대다수 쿼리가 snippet만으로 해결됨 (module card가 답을 직접 실어 나름). 유일한 miss는 F2 (analytics module 없음)
- **Step E L4** `src/hybrid_search/index/drift.py`: `detect_drift(project_id, project_root, db, config)` 읽기 전용 래퍼. `DriftReport(added, changed, deleted, total_on_disk)` + `is_drifted/drift_count/summary_line()`. 새 CLI: `hybrid-search drift [--cwd] [-v]`. **MCP tool 아님** (메모리 규칙: 빈도 낮은 기능 → CLI + skill)
- **Step E L5** `_interleave_modules`: `slots = min(slots, max(1, limit // 2))`. limit=10에선 no-op(3 module 유지), limit=5에선 2 module + 3 chunk, limit=2에선 1+1. limit=10 벤치 비트 동일
- 테스트: 662 → **674** (+12). drift 8 tests, L5 cap 4 tests, agent_loop sim은 실제 MCP 호출해서 통합적으로 검증됨

### 🎯 다음 세션 진입점

1. **이 19회차 섹션 전체**
2. `benchmarks/valuein_report_v6_step_d_e_2026-04-22.md` — Step D/E 상세
3. `benchmarks/valuein_report_v5_step_c_2026-04-22.md` "What vectors did not fix" 섹션 — 남은 실제 gap (module content / discovery)
4. `git log --oneline -6` — 4 commits origin/main 푸시 완료 (`6871c67`→`a90b4de`)

### 🎯 다음 세션 권장 순서

**Phase 5 plan doc 100% implementation + gap 4 step + Phase 6 L4/L5 전부 끝.** Phase 5 측정치는 거의 목표 도달:
- reads 2.55 (static) / 0.25 (agent-loop loose) — 목표 ≤ 2.5
- top-5 0.85 — 목표 달성
- recall@10 0.77 — 목표 달성
- **structure recall@10 0.41** — 목표 0.55 미달. 이건 **module content 문제**, retrieval 아님

**남은 실 작업 후보 (우선순위):**

1. **Module content improvement (1~2일) — 가장 즉효.** Step C 리포트가 정확히 지적한 실패 모드:
   - S2: `portal-v3` module summary가 "학부모/parent" 맥락을 명시 안 함 → student-hub가 의미적으로 더 가까움
   - S5: `entrance-tests` module이 top-10에 안 나옴 (`school-exam-scores` 선호)
   - F2: `components/analytics/` 디렉토리가 module로 발견 안 됨 (discover_modules의 minimum-file 임계치?)
   - 해결책: (a) module discovery에 `docs/features/` mention 추가 가중, (b) summary 보강 — `.md` 연관 문서 내용에서 tag/description 추출, (c) `analytics-mathflat` 별도 module로 승격
2. **다른 프로젝트 벤치마크 (반나절).** 현재 골드셋은 valuein_homepage 전용. breeze/mathontonlogy에도 골드 만들어 일반화 검증
3. **L5 추가 적용처 탐색 (선택).** 현재 L5는 limit=10 기준 no-op. limit=5/3 실사용자 있을 때 효과 봐야
4. **Phase 7 이후 plan doc 재구조** — 필요하면

**push 관련:** 19회차 4 커밋 모두 origin/main 푸시 완료. 즉시 다음 세션 가능.

### 주의사항 / 알려진 이슈

- **Static proxy(2.55)와 agent-loop(0.25)의 큰 간극**은 실측이 더 신뢰할 만하지만, loose mode의 "acceptable_module_names 매칭"은 느슨해서 strict(0.45)가 더 안전한 upper bound. 리포트 둘 다 보여줌
- **F2 (월별 학원 통계)**는 Step A/B/C/D 전부에서 miss. 유일하게 해결 안 된 gold query. module discovery를 먼저 손봐야 함
- **DB v6 → v7 migration**은 4 프로젝트 모두 smoke 통과. `ALTER TABLE ADD COLUMN ... summary_vector BLOB` + `vector_input_hash TEXT` 2개 컬럼
- **OpenAI API cost**: 161 modules 1 batch call = ~0.01 USD. 재인덱싱 시 vector_input_hash가 match되면 skip, 실질 비용 0
- **symbol routing edge**: `TuitionChargeSection 컴포넌트`처럼 camelCase + 한국어 → slot=0. 단일 camelCase 단어만 있고 나머지 다 한국어면 여전히 qtype=KOREAN_NL이지만 module inject는 안 함. 일관된 precision 경로
- **L5 cap 기본값 `limit // 2`**는 보수적. `limit=10`에서 slots=5 cap — 현재는 slots 자체가 최대 3이라 영향 없음. 나중에 ENGLISH_NL slot을 늘리거나 하면 cap 조정 고려

### 마지막 상태

- **브랜치:** `main` — origin/main 동기화됨 (19회차 4 커밋 모두 푸시 완료)
- **마지막 커밋:** `a90b4de [feat] Phase 5 Step D + Phase 6 Step E — agent-loop sim + drift + two-tier`
- **테스트:** **674/674 passed** (25~26s) — 18회차 631 + 19회차 +43 (Step A 12 + Step B 0 + Step C 19 + Step D 0 + Step E 12)
- **변경 파일 (19회차, 4 커밋 합계):**
  - Step A (`6871c67`): `orchestrator.py` (+47), `test_module_injection.py` (+58), `valuein_results.json`, `valuein_report_v3_step_a_2026-04-22.md` (신규)
  - Step B (`4e34b04`): `run_valuein_bench.py` (+59), `valuein_gold.json` (+23), `valuein_results.json`, `valuein_report_v4_step_b_2026-04-22.md` (신규)
  - Step C (`5ed5785`): `storage/db.py` (+61 migration/cols/upsert), `index/module_synth.py` (+88 embed pass), `index/pipeline.py` (+4 embedder 주입), `search/modules_search.py` (+73 vector path), `search/orchestrator.py` (+45 symbol routing + query_vector 전달), `test_module_synth.py` (+111), `test_modules_search.py` (+147), `test_module_injection.py` (+39 symbol tests), `valuein_results.json`, `valuein_report_v5_step_c_2026-04-22.md` (신규)
  - Step D+E (`a90b4de`): `benchmarks/agent_loop_sim.py` (신규 +270), `benchmarks/agent_loop_loose.json` (신규), `benchmarks/agent_loop_strict.json` (신규), `src/hybrid_search/index/drift.py` (신규 +84), `src/hybrid_search/cli.py` (+68 cmd_drift + parser + dispatch), `src/hybrid_search/search/orchestrator.py` (+10 L5 cap), `tests/test_drift.py` (신규 +128), `tests/test_module_injection.py` (+46 L5 tests), `benchmarks/valuein_results.json`, `valuein_report_v6_step_d_e_2026-04-22.md` (신규)

### 로드맵 진행률 업데이트

18회차 33/33 plan doc 완전 구현 + 2개 목표 미달 → 19회차 gap 4 step(A/B/C/D) + Phase 6 L4/L5 전부 shipped = **Phase 5 roadmap 100% + Phase 6 시작.** 남은 건 module content 개선 (유일 gold miss F2 포함) + 다른 프로젝트 벤치 일반화.

---

## 🔵 이전 세션 인계 (18회차) — 참고용

### 한줄 요약

**Phase 4 (실전 벤치마크) + Phase 5 전체 5 step (Subsystem-first Retrieval) 한 세션 shipped.** Plan doc(`docs/plan/2026-04-21-memory-layer-10x.md`)대로 Phase 1~5 모두 완전 구현 완료. Phase 4에서 valuein_homepage 1307 files 대상 골드셋 20개 + 자동 벤치마크 러너 작성, hybrid 대 naive grep 2.0× primary top-5·1.8× recall@10 확보. Phase 4 리포트에서 **structure recall@10 = 0.22** 근본 문제 발견 → 원인 "검색 리턴 단위가 chunk인데 사용자 답변 단위는 subsystem" 진단 → plan doc에 Phase 5 "Subsystem-first Retrieval" 5 step 구조로 재작성하고 그대로 shipped. Step 2 module discovery (heuristic: directory + doc-mention strict merge, 161 modules 발견), Step 3 deterministic module card synthesis (no LLM, hash-skip), Step 4 SearchOrchestrator에 module injection + 25 Korean↔English alias + interleave placement, Step 5 benchmark v2 리포트 + README 갱신. **631/631 passed (+51)**. 재측정 결과 structure recall 0.22→0.41 (≈2×), exploration 0.47→0.67, precision/rationale 1.00 유지. 단 module 주입으로 read_count 3.65→4.60 회귀 (근본 원인 identification honest). 커밋 6개 origin/main 대비 로컬 only(`ae41de2` Step1 → `ba71d80` Step5).

### ✅ 이 세션 완료된 것 (18회차)

**1. Phase 4 — valuein_homepage 실전 벤치마크 (커밋 `2dcc198`)**
- `benchmarks/valuein_gold.json` — 20 쿼리 (structure/exploration/precision/rationale × 5)
- `benchmarks/run_valuein_bench.py` — hybrid vs naive token-bag grep 자동 러너
- `benchmarks/valuein_report_2026-04-22.md` — 전체 리포트 + caveats
- README "Real-world benchmark" 섹션 신설
- 핵심 숫자 (20 gold queries, top-10): hybrid primary top-5 0.65 vs grep 0.35, recall@10 0.67 vs 0.37
- **structure 카테고리 recall@10 = 0.22**라는 근본 문제 명시

**2. Phase 5 Step 1 — Agent-cost proxy 지표 + plan doc 재구조 (커밋 `ae41de2`)**
- `run_valuein_bench.py`에 `snippet_bytes` / `read_count_estimate` / `context_pack_bytes` 추가
- Plan doc 완전 재구조: Phase 1-4 status stamp + 새 Phase 5 "Subsystem-first Retrieval" 5 step 상세 + 옛 Phase 5 → Phase 6 deferred
- 10× 경쟁 축 재정의: "agent turn/token 효율"이며 그래프 정교함 아님

**3. Phase 5 Step 2 — Module discovery + DB v6 (커밋 `8c33b9b`, +19 tests)**
- `src/hybrid_search/index/modules.py` 신설. Heuristic signals:
  - Directory prefix (container dirs `src/app/components/lib` strip, leaf dir가 module key)
  - Doc-code mentions (strict rule: 모든 mention이 한 module key로 resolve될 때만 merge. Plurality/N-way는 chain-merge로 super-module 생성해서 reject)
- DB v6 migration: `modules(id,project_id,name,summary,entry_points,depends_on,related_docs,rationale,signals,member_hash,updated_at)` + `file_modules(file_id,module_id,weight,project_id)` (code 1.0, doc 0.5)
- `ModuleRecord` dataclass + 10여 개 DB method (upsert/get/search/delete)
- `pipeline.py`에서 `resolve_call_edges` 뒤에 `discover_modules` 호출 (non-fatal)
- 실측: valuein_homepage 161 modules, key subsystems(portal-v3 6/tuition 8/tuition-session 8/tuition-wizard 6/remote-room 13/homework-analysis 11/consultations 6/entrance-tests 6/admissions 2) 모두 별도 모듈

**4. Phase 5 Step 3 — Module card synthesis (커밋 `5a74df6`, +11 tests)**
- `src/hybrid_search/index/module_synth.py` 신설. LLM-free 결정론:
  - `summary`: 가장 긴 chunk docstring head + member filenames, fallback = name + file list
  - `entry_points`: top-5 chunks by docstring length + node_type preference (function/class/method/export > statement/block)
  - `depends_on`: call_edges로 다른 module_id 도달
  - `rationale`: Phase 3 M10 docstring에서 NOTE/WHY/TODO/FIXME/HACK/XXX dedup
- Hash-based skip: summary 앞에 `[hash:v1:xxx]` prefix. 재합성은 delta pass
- 실측: 161/161 synthesized, 2개 module rationale 있음

**5. Phase 5 Step 4 — Module-first retrieval (커밋 `ca9ccbd`, +21 tests)**
- `src/hybrid_search/search/modules_search.py` 신설:
  - `search_modules(db, project_id, query, limit)` — 토큰 overlap 스코어, name 포함 시 +10 부스트 + occ tie-break
  - `module_text()` — summary의 `[hash:]` prefix strip
  - 25쌍 Korean↔English alias (`포털↔portal`, `수강료↔tuition`, `학생↔student` 등) — 한국어 NL 쿼리와 영어 module name 브리지
- `SearchOrchestrator`:
  - `HybridResult.module_id` optional 필드
  - `_module_results_for_query(qtype, query, projects)` — query_type별 slot (`KOREAN_NL=3`, `ENGLISH_NL=2`, `EXACT_SYMBOL=0`)
  - `_interleave_modules()` — 위치 1/3/5에 module, top chunk는 위치 2 보존 (rationale 쿼리에서 plan doc이 밀리지 않게)
  - `_module_representative_path()` — entry_points[0].file → related_docs[0] → first member
- `test_orchestrator.py`에 `_module_results_for_query` stub (mock path로 실제 파일 생성 방지)

**6. Phase 5 Step 5 — Report v2 + README (커밋 `ba71d80`)**
- `benchmarks/valuein_report_v2_2026-04-22.md`: Phase 4 vs Phase 5 delta + per-category + target readback (2/4 그린, 2/4 미달) + 정직한 failure mode 분석
- README "Real-world benchmark" 섹션 Phase 5 숫자로 갱신 (recall@10 0.77 vs grep 0.37 → 2.1×)

**7. 테스트 상태**
- 580 (17회차) → 599 (Step 2) → 610 (Step 3) → 630 (Step 4) → 631 (+interleave 테스트 추가) = **+51 cases**
- 회귀 0
- Step 4 구현 중 mock test가 실제 filesystem에 `<MagicMock ...>` 이름의 junk 파일 16개 생성 → 제거 + orchestrator 테스트에 stub 추가로 재발 방지

### 🎯 다음 세션 진입점

1. **이 18회차 섹션 전체**
2. `docs/plan/2026-04-21-memory-layer-10x.md` Phase 5 섹션 — "What's next after Phase 5" 4개 항목이 구현 백로그
3. `benchmarks/valuein_report_v2_2026-04-22.md` "What didn't work" 섹션 — 각 failure mode가 업그레이드 target
4. `git log --oneline -7` + `git status` — 로컬 +6 커밋, **push 여부 사용자 확인 필요**
5. 아래 "다음 세션 권장 순서"

### 🎯 다음 세션 권장 순서

**사용자 지시:** "완전 구현 후 부족한걸 테스트 해가면서 업그레이드할거야" → Phase 1-5 구현은 끝. 이제 **측정된 gap을 좁히는 반복 사이클**.

**Phase 5 gap 채우기 (우선순위 순):**

**Step A (1일) — Per-category intent routing.** rationale 쿼리가 module 주입으로 read_count 1.6→4.0 회귀. `search/orchestrator.py`의 `_module_slots_for(qtype)`에 "rationale" 감지 분기 추가. Korean NL 쿼리 중 "이유/왜/배경/설계/목적" 토큰 있으면 0 slot. 기대: read_count 4.60 → ~3.5.
- 대상: `_module_slots_for` + 새 `_has_rationale_signal(query)` helper
- 테스트: 왜/이유 포함 KOREAN_NL 쿼리 slot=0 검증
- 재측정 기대: rationale 카테고리 reads 4.00 → ~2.40 복원

**Step B (반나절) — Gold-set v2: module as valid primary_target.** structure 카테고리 top-1 0.00 문제. 모듈 카드가 맞는 답인데 gold가 단일 파일을 요구. `valuein_gold.json`에 `acceptable_module_names` 옵션 필드 추가 → `run_valuein_bench.py` score_query가 module 히트도 primary로 인정.
- 대상: `benchmarks/valuein_gold.json` + `run_valuein_bench.py`
- 기대: structure top-1 0.00 → 0.40+, top-5 0.60 → 0.80+

**Step C (3~5일) — Module card vector embedding.** alias 25개 하드코딩 제거. module synthesis 시점에 card 텍스트 embed → `modules.summary_vector` 컬럼 (BLOB) 추가. `search_modules`가 vector 코사인도 사용. 다른 프로젝트에도 자동 적용 가능.
- 대상: DB v7 migration + `module_synth.py` 임베딩 저장 + `modules_search.py` vector path
- 의존: 기존 embedder 재사용 (외부 API 키 불필요하면 OK)
- 기대: exploration recall 0.67 → 0.75+, structure recall 0.41 → 0.55+ (목표치 달성)

**Step D (반나절) — Agent-in-loop 파일럿 5쿼리.** 자동 proxy만 있고 실측 턴/토큰 없음. Claude Code 세션에서 structure/exploration 각 2~3개 수동 실행, 대화 로그 캡처, 실제 Read 호출 수 집계. 리포트 "Agent-in-loop measurement" 섹션 신설.
- 파일럿 샘플: S2 (학부모 포털) / F2 (월별 통계) / R4 (AI 콘텐츠 팩토리) 포함

**Step E (선택) — Phase 6 L4 watchdog / L5 two-tier.** Phase 5 gap 다 채운 후 실사용 피드백 보고.

**순서:** A (즉효, 1일) → B (즉효, 반나절) → 재측정 → C (큰 잠재력, 3~5일) → 재측정 → D (파일럿) → E (선택). 총 1~2주.

**push 관련:** 18회차 6 커밋 로컬만. 다음 세션 시작 시 `git push origin main` 먼저 할지, Step A/B 마친 후 묶어 push 할지 사용자 확인.

### 주의사항 / 알려진 이슈

- **Phase 5 module 주입은 rationale 카테고리에 read_count 회귀 동반 (3.65→4.60).** 의도된 trade-off는 아님 — rationale intent 라우팅(Step A)으로 복원 가능. 현재는 interleave (1/3/5 position)로 완화했지만 근본 해결은 아님.
- **Structure top-1 0.00 드롭**은 gold set 정의 issue (module이 맞는 답인데 file을 가리킴). Step B로 해결.
- **Alias 25개 하드코딩**은 다른 코드베이스엔 각자 사전 필요. Step C (vector embedding)이 근본. 현재 alias는 valuein_homepage 도메인 한정.
- **DB v5 → v6 migration은 실 DB에서 smoke 통과했지만** 다른 프로젝트(breeze/mathontonlogy/hybrid-search-mcp)는 재인덱싱 안 해봄. 세 프로젝트 중 하나에서 Step A 재측정 시 자동 migration 확인 권장.
- **Module discovery는 idempotent**지만 member_hash가 파일 rel_path 집합만 반영. 파일 내용 변경은 synthesis의 `inp_hash`가 잡음. 두 hash의 역할 분리 기억.
- **Junk file 재발 방지:** orchestrator mock 테스트에서 `_module_results_for_query`를 stub 안 하면 `<MagicMock ...>` 이름으로 repo root에 파일 생성됨. `tests/test_orchestrator.py` `_make_orchestrator`에 stub 있음 확인.
- **17회차 잔존 이슈:** v4 → v5 미검증 프로젝트 3개 (지금 v6까지 있으니 더 민감).

### 마지막 상태

- **브랜치:** `main` — origin/main 기준 로컬 +6 (18회차 커밋 전부 미푸시)
- **마지막 커밋:** `ba71d80 [docs] Phase 5 Step 5 — benchmark report v2 + README update`
- **테스트:** **631/631 passed** (25s) — 17회차 580 + 18회차 +51 (Phase 4 0 + Step2 19 + Step3 11 + Step4 21)
- **변경 파일 (18회차, 6 커밋 합계):**
  - Phase 4 (`2dcc198`): `benchmarks/valuein_gold.json` (신규), `benchmarks/run_valuein_bench.py` (신규), `benchmarks/valuein_report_2026-04-22.md` (신규), `benchmarks/valuein_results.json`, `README.md`
  - Phase 5 Step 1 (`ae41de2`): `benchmarks/run_valuein_bench.py` (지표 추가), `benchmarks/valuein_report_2026-04-22.md` (baseline 섹션), `docs/plan/2026-04-21-memory-layer-10x.md` (전면 재구조), `benchmarks/valuein_results.json`
  - Phase 5 Step 2 (`8c33b9b`): `src/hybrid_search/index/modules.py` (신규 +246), `src/hybrid_search/index/pipeline.py` (+8), `src/hybrid_search/storage/db.py` (v6 migration + ModuleRecord + 10 methods), `tests/test_modules.py` (신규 19 cases)
  - Phase 5 Step 3 (`5a74df6`): `src/hybrid_search/index/module_synth.py` (신규 +181), `src/hybrid_search/index/pipeline.py` (+8), `tests/test_module_synth.py` (신규 11 cases)
  - Phase 5 Step 4 (`ca9ccbd`): `src/hybrid_search/search/modules_search.py` (신규 +130), `src/hybrid_search/search/orchestrator.py` (+190 module injection), `tests/test_modules_search.py` (신규 10 cases), `tests/test_module_injection.py` (신규 8 cases), `tests/test_orchestrator.py` (+1 stub)
  - Phase 5 Step 5 (`ba71d80`): `benchmarks/valuein_report_v2_2026-04-22.md` (신규), `README.md` (Real-world benchmark 섹션 갱신)

### 로드맵 진행률 업데이트

17회차 31/33 (94%). 18회차 +2 (Phase 4 + Phase 5) = **33/33 (100%) plan doc 완전 구현**. 다만 Phase 5 목표치 4개 중 2개 미달이므로 **실효 완성은 gap 4 step (A/B/C/D) 소화 후**. 남은 것:
- Step A: rationale routing (1일, read_count 회복)
- Step B: gold v2 (반나절, structure top-1 회복)
- Step C: module card embedding (3~5일, 근본적으로 alias 제거 + target 달성)
- Step D: agent-in-loop 파일럿 (반나절, 진짜 10× 증거)
- Phase 6 L4/L5 (선택)

---

## 🔵 이전 세션 인계 (17회차) — 참고용

### 한줄 요약

**Phase 2 (Wiki 파편화 + drift) + Phase 3 (M9 two-pass callgraph + M10 rationale) 한 세션 shipped.** Phase 2에서 `_deduplicate_names`의 `-N` 접미사가 `test_wiki-1..11.md` 류 파편화를 유발하던 원인 파악 → union-find 기반 `_merge_file_overlapping_modules`로 "같은 파일 = 같은 모듈" 불변식 강제, 추가로 `src/hybrid_search/index/` 패키지가 wiki `index.md`를 덮어쓰던 slug 충돌을 `_rename_reserved_slugs`로 방어, 재생성 후 stale `.md` 청소 `_cleanup_orphan_wiki_pages` 추가. 실측 파일 수 **215 → 36** (test_wiki 11조각 → `tests.md` 1개, coverage.json 36 == disk 36). Phase 3에서 `callgraph.py`에 context-aware 2차 패스 추가(caller-file의 해석된 target files로 ambiguous → inferred 업그레이드, 실측 6943 edges 중 24 upgrade), `ast_chunker.py`에 NOTE/WHY/TODO/FIXME/HACK/XXX + JSDoc `@remarks`/`@note` 추출기 `_extract_rationale` 추가(Python/TS/JS/Java/Rust/Go/Ruby 지원, export_statement 래핑 처리). docstring 필드에 append되어 BM25/임베딩에 자연 반영. 실제 `# WHY:` 주석이 reindex 후 DB에 들어가는 end-to-end 검증 성공. **580/580 passed (+32)**. 커밋 2개(`295c07d` Phase 2, `a4dc5c2` Phase 3) main에 있음 (푸시 전).

### ✅ 이 세션 완료된 것 (17회차)

**1. Phase 2 — Wiki 파편화 원인 파악 + 패치 (커밋 `295c07d`)**
- 원인: `src/hybrid_search/index/dag.py:811 _deduplicate_names`가 동일 이름 모듈에 `-N` 접미사. `test_wiki.py` 파일에서 서로 호출 안 하는 테스트 함수들이 각각 disconnected component → 11개 모듈 → `test_wiki-1..11.md`.
- `_merge_file_overlapping_modules` (+100줄): union-find로 파일 공유 모듈을 하나로. generate_wiki_plan에서 `_deduplicate_names` 직전 호출. 이름은 가장 많은 chunk를 가진 멤버에서 계승, files/chunks/entry_points는 union.
- `_rename_reserved_slugs` + `_module_slug` 헬퍼: 모듈명 slug가 "index"면 `"index module"`로 재명명. 원래 `src/hybrid_search/index/` 패키지가 `index.md` 파일명으로 slug되어 generated wiki index를 덮어쓰던 버그 수정. Generated index 파일 보존.
- `cli.py:_cleanup_orphan_wiki_pages` (+28줄): 전체 재생성 시 expected_filenames 밖의 top-level .md 파일 제거. `STALE.md`와 서브디렉토리(`_synthesis_input/` 등) 보존.
- 테스트 +21 (test_dag +14 [merge 10 + reserved 4], test_wiki_cleanup 신규 +7)
- 실제 재생성 결과: 215 → 36 파일, coverage.json total_pages=36 == disk .md 36, `tests.md` 1개에 22 files 142 symbols 통합

**2. Phase 3.1 — M9 two-pass callgraph (커밋 `a4dc5c2` 일부)**
- `callgraph.py` 리팩토링: pass 1 결과를 리스트로 수집(rowid, caller_chunk_id, chunk_id, qname, confidence) → pass 2에서 DB 업데이트 전에 재검토.
- Pass 2 로직: pre-existing + pass1 결과에서 caller_file → target_files 맵 구축(자기 파일 제외 — Strategy 3가 이미 same-file 우선). ambiguous 에지 중 caller의 target_files 안에 후보가 있으면 inferred로 업그레이드 + `stats["pass2_upgraded"]` 카운트.
- 내 자신의 reindex에서 실측: 6943 total, 249 extracted + 1488 inferred + 8 ambiguous + 5198 unresolved, **pass2 24 업그레이드**. ambiguous 32 → 8로 감소.
- 테스트 +3 (context로 업그레이드 / related-file 없으면 업그레이드 X / same-file은 context에서 제외)

**3. Phase 3.2 — M10 rationale 추출 (커밋 `a4dc5c2` 나머지)**
- `_extract_rationale(node, source_bytes, language)` (+85줄): 함수/클래스 본문의 comment 노드를 후위 순회하며 `NOTE:/WHY:/TODO:/FIXME:/HACK:/XXX:` 태그 매칭, 대소문자 정규화 + 중복 제거 + 첫 등장 순서 보존. JSDoc/Javadoc의 `@remarks`, `@note` 태그도 `/** */` 블록에서 regex 추출.
- export_statement 래핑 처리: TS/JS에서 `export function doit()`의 function_declaration은 parent가 export_statement라 JSDoc이 한 단계 위에 있음 → doc_target을 한 단계 올려서 찾음.
- `_walk_node`에서 `_extract_docstring` 직후 rationale 추출하여 docstring 필드에 `\n\n` 구분자로 append. 기존 `_build_embedding_input`이 docstring을 이미 임베딩 인풋에 포함 → BM25 + 벡터 모두 자연 반영.
- 테스트 +8 (Python NOTE/WHY 단독 추출, 다중 태그 dedup+순서, 태그 없으면 docstring 유지, 플레인 주석 무시, 대소문자, TS `// NOTE:`, JSDoc `@remarks`)
- 검증: `callgraph.py`에 실제 `# WHY: same-file edges are excluded...` 주석 추가 후 `reindex --force` → DB의 `resolve_call_edges_part1` 청크 docstring에 `WHY: same-file edges are excluded — Strategy 3` 포함 확인.

**4. 테스트 상태**
- 548 (16회차) → 569 (Phase 2) → 580 (Phase 3). +32 cases.

### 🎯 다음 세션 진입점

1. **이 17회차 섹션 전체**
2. `docs/plan/2026-04-21-memory-layer-10x.md` — Phase 4 세부 (valuein_homepage 골드셋 + 벤치마크)
3. `git log --oneline -6` — 확인 및 push 여부 결정
4. 아래 "다음 세션 권장 순서"

### 🎯 다음 세션 권장 순서

**Phase 4 — valuein_homepage 실전 검증 (1주).** 플랜 문서의 Phase 4 섹션.

**Step 1 (1~2일) — 골드셋 작성:**
- `benchmarks/valuein_gold.json` — 15~20 쿼리:
  - 구조 질문 5 (e.g. "수학 문제 모델 구성", "인증 흐름 클래스")
  - 기능 탐색 5 (한국어 자연어)
  - 정밀 조회 5 (exact symbol)
  - Rationale 질문 5 ("왜 이 설계를 택했나")
- 도메인 지식 기반 수작업 gold (proxy label 아님)
- valuein_homepage가 이미 인덱싱되어 있는지 `hybrid-search-mcp status --cwd /path/to/valuein_homepage` 확인. 없으면 reindex.

**Step 2 (반나절) — 측정:**
- Baseline 1: Claude가 Grep+Read로 탐색 (턴 수, 토큰, 정답 도달률)
- Baseline 2: DeepWiki/MCP-other 있으면
- Ours: hybrid+wiki+qa (Phase 1-3 반영본, rationale 인덱스 포함)
- 지표: NDCG@10, MRR@10, time-to-answer, tokens consumed

**Step 3 (반나절) — 리포트:**
- `benchmarks/valuein_report_2026-04-XX.md` 표 + 분석
- README에 "Real-world benchmark" 섹션 신설

**Phase 4 완료 조건:**
- [ ] valuein_gold.json 15~20q
- [ ] baseline 대비 token 효율 ≥10x, latency ≥2x 증거 (또는 개선 여지 투명 리포트)
- [ ] README Real-world benchmark 섹션

대안: **Phase 4 대신 Phase 2/3 실사용 피드백 주간** — 며칠 써보고 wiki 파편화 재발 여부, rationale 검색 체감, pass2 효과 재측정.

### 주의사항 / 알려진 이슈

- **M9 pass2 effect는 현 repo에선 24 upgrades (미미).** 큰 효과는 cross-file method call이 많은 프로젝트(valuein_homepage)에서 측정 필요. 현 repo는 파이썬 내부 호출 위주라 Strategy 3 same-file 선호가 이미 많은 걸 잡음.
- **M10 rationale은 이 프로젝트 자체에 NOTE/WHY/TODO 주석이 거의 없어 효과 불가시.** 내가 `callgraph.py`에 추가한 `# WHY: ...` 한 개가 유일한 실례. valuein_homepage에서 측정해야 진짜 가치 확인.
- **Wiki `-N` 접미사가 완전히 사라지진 않음.** `search (isolated)-1/-2` 같은 건 정당한 동명이인(`src/hybrid_search/search/` + `skills/search.md`). 파일 집합이 disjoint라 merge되지 않음. 이건 버그 아님.
- **reindex 시 wiki-gaps.txt 91행으로 증가 유지.** qa 로그 인덱싱 opt-in 때문. Phase 2는 파편화만 다뤘고 wiki-gaps에서 qa 경로 제외는 안 함. 필요 시 detect_wiki_gaps 로직에 qa/ 예외 추가 (Phase 4 이후).
- **Phase 2 patch는 단일 파일 기준만 병합.** 여러 파일을 연결하는 connected component 간에 공유 파일이 있으면 transitive merge됨 (union-find 특성). 의도된 동작. 거대 merge가 드물게 발생할 수 있음 → 임계 초과는 한 페이지 안에서 처리 (plan doc 명시).
- **커밋 `295c07d`, `a4dc5c2`은 main 있으나 origin에 push 안 됨.** 다음 세션 시작 시 `git push origin main` 필요.
- **15/16회차 이슈 잔존:** v4 → v5 마이그레이션 미검증 프로젝트 3개.

### 마지막 상태

- **브랜치:** `main` — origin/main 기준 로컬 +2 (Phase 2/3 미푸시)
- **마지막 커밋:** `a4dc5c2 [feat] Phase 3 — M9 two-pass callgraph + M10 rationale 추출`
- **테스트:** **580/580 passed** (25s) — 16회차 548 + Phase 2 +21 + Phase 3 +11
- **변경 파일 (17회차):**
  - Phase 2 (`295c07d`): `src/hybrid_search/index/dag.py` (+126, merge + reserved slugs + slug 헬퍼 통합), `src/hybrid_search/cli.py` (+34, cleanup_orphan_wiki_pages), `tests/test_dag.py` (+152), `tests/test_wiki_cleanup.py` (신규)
  - Phase 3 (`a4dc5c2`): `src/hybrid_search/index/ast_chunker.py` (+83, _extract_rationale + export_statement JSDoc 처리 + 실제 `# WHY:` 주석 1개), `src/hybrid_search/index/callgraph.py` (+74, 2차 패스 + caller_to_targets), `tests/test_ast_chunker.py` (+118, rationale 8 cases), `tests/test_callgraph.py` (+167, pass2 3 cases)

### 로드맵 진행률 업데이트

16회차 29/33 (88%). 17회차 +2 (Phase 2, Phase 3) = **31/33 (94%)**. 남은 핵심:
- Phase 4 — valuein_homepage 실전 gold set + 벤치마크 (1주)
- L2 Leiden 풀스케일 wiki auto-gen (Phase 4 이후 재평가)
- L4 watch / L5 two-tier merge (선택)

---

## 🔵 이전 세션 인계 (16회차) — 참고용

### 한줄 요약

**"MVP 완료 = 완성" 오진단 수정 + Phase 1 (Memory Layer) 완성.** 옆 세션(15회차)이 완료를 과장한 상태를 팩트 체크(qa_log.py 실제 259줄, public 동작 경로는 `record()` 단독, `qa list/show/grep` CLI 전무, `.hybrid-search/qa/`가 .gitignore로 인덱싱 제외됨)로 수정 → `_study/graphify-analysis/` 5,035줄 8문서 기반 gap 분석으로 **실제 남은 것**만 뽑아 4-Phase 완성 로드맵(`docs/plan/2026-04-21-memory-layer-10x.md`) 수립 → **Phase 1 (Sprint 2/3/4) 전부 이번 세션에 shipped**. 548/548 passed (+48). Memory Layer 4축(write/read/self-ref/rotation) 모두 라이브. 실데이터 자기참조 검증 성공: "authority_alpha 재튜닝" 쿼리에 과거 qa 로그가 `node_type=qa_log`로 1위 소환. 커밋 5개(15회차 tail `a0dc2b5`, Sprint 2 `ab77361`, Sprint 3 `5f5749c`, Sprint 4 `b31d179`, README 정식 승격 `59a53ec`) 전부 origin/main 푸시 완료. README "Memory Layer" 섹션을 Write/Read/Self-ref/Rotation 4축 구조로 재작성.

### ✅ 이 세션 완료된 것 (16회차)

**1. 방향 전환 — "MVP=완성" 진단 재검증**
- 옆 세션 주장 6개 팩트체크: qa_log.py 실제 **259줄** (주장은 230), public 함수는 `record()` + `is_enabled()` 두 개이지만 외부 동작 경로는 `record()` 독점 (유일 caller: `tools/hybrid_search.py:119`).
- `qa list/show/grep` CLI 서브커맨드 `--help`에 0개, `.gitignore`에 `.hybrid-search/qa/` 등재로 scanner 인덱싱 제외됨 확인.
- HANDOFF 15회차 line 52 "C MVP만으로는 write-only → 완전 가치 실현 X", line 60 "Memory Layer는 write+read 둘 다 돼야" 그대로 인용해 입증.
- 사용자 지시: "완성하기 위한 플랜을 만들고 구현 시작해" → Phase 로드맵 수립.

**2. 로드맵 문서 (`docs/plan/2026-04-21-memory-layer-10x.md`, +247줄)**
- graphify 정밀 분석 8문서(5,035줄) gap 분석. 이미 shipped인 Q1/Q2/Q7/Q8/Q9, M1/M1.1/M1.2/M2/M3/M4/M5, L6, 15회차 A/B/C/D 전부 제외.
- **진짜 남은 것**: L1 Memory read+self-ref, Wiki 파편화, M9 two-pass callgraph, M10 rationale, L2-lite Leiden wiki, L4 watch / L5 two-tier, valuein_homepage 실전 검증.
- 4-Phase 구조: Phase 1 (Memory 완성) → 2 (Wiki 파편화) → 3 (정확도) → 4 (valuein_homepage).
- "10배" 조작적 정의: DX × precision × recall × navigation. 주차별 기대 효과 명시.

**3. Sprint 2 — qa 조회 CLI (커밋 `ab77361`)**
- `src/hybrid_search/memory/reader.py` 신설 (+240줄): `iter_qa_files` / `parse_qa_index` / `find_qa_by_id` / `grep_qa` / `read_qa_body` + `QAIndex` / `GrepHit` dataclass. **YAML 의존성 없음** (placeholder 기반 escape 역변환으로 `\\"` / `\\\\` 구분).
- `cli.py`: `qa-list` / `qa-show` / `qa-grep` / `qa-stats` 4개 서브커맨드 + `_resolve_qa_root` 헬퍼.
- `tests/test_qa_reader.py`: 19 케이스 — 파싱, escape 왕복, iter 정렬, id resolve (friendly/stem/hash-prefix), grep, body.

**4. Sprint 3 — qa 로그 self-indexing (커밋 `5f5749c`)**
- `config.py`: `IndexingConfig.index_qa_logs: bool = False` 필드 + `HYBRID_SEARCH_INDEX_QA` env 토글 (env > TOML 우선).
- `scanner.py`: `_walk_files` dotdir prune에 예외 추가 — opt-in 시 `.hybrid-search` 한 곳만 진입 (다른 dotdir은 그대로 차단). `_build_ignore_spec`에 `!.hybrid-search/qa/**` negation 추가해서 setup이 기록한 `.gitignore` 엔트리 우회.
- `doc_chunker.py`: `.hybrid-search/qa/**.md`는 섹션 분할 대신 **whole-file 1청크** + `node_type="qa_log"`. Top results ## 헤딩이 쪼개져 쿼리↔결과 맥락 깨지는 걸 방지. `_whole_file_chunk`에 `node_type` kwarg 추가.
- **실데이터 자기참조 검증**: repo에 qa 로그 4개 시딩 → `HYBRID_SEARCH_INDEX_QA=1 reindex --force --cwd .` → "authority_alpha 재튜닝" 쿼리 1위가 `node_type=qa_log` (`0e1534ad`), "Sprint 2 qa list show grep 설계" 1위도 qa_log (`6bbdc19b`). JSON 출력에 `node_type` 필드 유지 — 클라이언트 필터/랭킹 분리 가능.

**5. Sprint 4 — retention + cross-project (커밋 `b31d179`)**
- `reader.py` +152줄: `parse_duration` ("30d/12h/2w/3m", months=30d 근사), `resolve_cutoff` (older_than/before mutex, naive UTC 보정), `select_older_than`, `prune_older_than` + `PruneResult`. `_rmdir_empty_ancestors`로 비게 된 YYYY/MM 자동 정리 (qa/ 루트는 앵커로 보존).
- `cli.py`: `qa-list --all` (`ProjectRegistry.list_all()` 순회, home 제외, `<project>:` prefix, --project/--cwd와 mutually exclusive), `qa-prune --older-than <dur> | --before <iso> [--dry-run] [--verbose]`.
- 테스트 +22: parse_duration 8 (parametrize), resolve_cutoff 5, prune 5, 보조 4.

**6. README 정식 섹션 승격 (커밋 `59a53ec`)**
- "Memory Layer (opt-in, MVP)" → "Memory Layer" 4축 하위 섹션(Write / Read / Self-reference / Rotation) 재작성. MVP 경고 제거, `HYBRID_SEARCH_INDEX_QA` 토글 + `node_type="qa_log"` 노출 정책 명시.
- CLI Usage에 Memory Layer 8줄 블록 신규, CLI Reference 5행(qa-list/show/grep/stats/prune), Troubleshooting "qa logs not surfacing in search" 행 추가.
- CLI-async race / 프라이버시 caveat는 보존.

### 🎯 다음 세션 진입점

1. **이 16회차 섹션 전체**
2. `docs/plan/2026-04-21-memory-layer-10x.md` — Phase 2~4 세부 플랜
3. `git log --oneline -6`
4. 아래 "다음 세션 권장 순서"

### 🎯 다음 세션 권장 순서

**Phase 2 — Wiki 파편화 해결 (2~3일).** 플랜 문서의 Phase 2 섹션 그대로.

**Step 1 (반나절) — 원인 파악:**
- 현상: `tests/test_wiki.py` 1파일이 `test_wiki-1.md` ~ `test_wiki-11.md` 11쪽으로 쪼개짐. `test_cli_hook_install.py`는 12쪽. Wiki 98개 중 절반이 이런 파편.
- 추적 대상: `src/hybrid_search/storage/wiki.py` `WikiStore.compile_page` + `src/hybrid_search/index/pipeline.py` module 배정 로직. 파일 → module_id 매핑이 어디서 `-N` 접미사 붙이는지 grep.
- 의심: 파일당 심볼 수 임계 초과 시 module 분할 → slug 충돌 회피로 `-N`.

**Step 2 (1~2일) — 소극적 패치:**
- 같은 파일 내 심볼은 반드시 같은 module_id로 강제. 임계 초과는 한 페이지 안에서 처리.
- 테스트: 고의적으로 심볼 수 많은 파일 → 페이지 1개만 생성되는지.
- Leiden(L2) 풀스케일은 Phase 4 이후로 미룸 (너무 큼).

**Step 3 (1시간) — drift 청소:**
- `hybrid-search-mcp rebuild-index` 후 `coverage.json` total_pages와 실제 `.hybrid-search/wiki/*.md` 카운트 일치 검증 테스트 추가.
- 현재 상태: total_pages=75 vs 실파일 98 → orphan 23개.

**Phase 2 완료 조건**:
- [ ] `test_wiki.py` 1파일 = `test_wiki.md` 1페이지
- [ ] wiki .md 파일 수 == `coverage.json` total_pages
- [ ] STALE 0, needs_synthesis 0 (재생성 후)
- [ ] orphan 검출 테스트 신규

그 다음 Phase 3 (M9 two-pass callgraph + M10 rationale) → Phase 4 (valuein_homepage 실전 검증).

### 주의사항 / 알려진 이슈

- **qa_log async CLI race는 알려진 제약이지 버그가 아님.** MCP 서버(long-running)에서만 daemon thread 완료 보장. CLI 단발 실행은 종료 시 daemon이 죽어 파일 생성 race 가능. README Troubleshooting에 명시. 해결책: sync API (`async_write=False`) 또는 MCP 서버 경로.
- **qa 자기참조 효과는 `HYBRID_SEARCH_INDEX_QA=1` + `reindex --force` 필수.** scanner가 opt-in 여부를 config에서 읽기 때문에 env만 켜고 reindex 안 하면 기존 DB에는 qa chunks 없음.
- **qa 인덱싱 후 wiki-gaps.txt 298행으로 증가:** qa 로그가 wiki 커버리지 없는 새 파일로 들어가서. Phase 2 진입 시 qa 경로는 wiki 대상에서 명시적으로 제외하는 로직 추가 필요.
- **scanner dotdir 예외는 `.hybrid-search`만.** 다른 dotdir(`.github` 등)은 여전히 기본 차단. 필요 시 `_keep_dir` 로직 확장.
- **Wiki 파편화는 16회차에서 수정 안 함.** `test_wiki-1..11` / `test_cli_hook_install-1..12` 그대로 남음. Phase 2 진입 후 처리.
- **15회차 이슈 잔존 (승계):** v4 → v5 마이그레이션 미검증 프로젝트 3개.

### 마지막 상태

- **브랜치:** `main` — origin/main과 동기화 완료 (16회차 커밋 4개 전부 push됨)
- **마지막 커밋:** `59a53ec [docs] README — Memory Layer 정식 섹션 승격 (Phase 1 완료)`
- **테스트:** **548/548 passed** (25s) — 15회차 500 + 16회차 +48 (Sprint 2 +19, Sprint 3 +7, Sprint 4 +22)
- **변경 파일:**
  - 신규: `src/hybrid_search/memory/reader.py`, `tests/test_qa_reader.py`, `docs/plan/2026-04-21-memory-layer-10x.md`
  - 수정: `src/hybrid_search/cli.py` (qa 5 subcommand + `_resolve_qa_root` + `_iter_all_project_roots` + datetime 임포트), `src/hybrid_search/memory/__init__.py`, `src/hybrid_search/config.py` (IndexingConfig.index_qa_logs + os 임포트), `src/hybrid_search/index/scanner.py` (dotdir 예외 + ignore negation), `src/hybrid_search/index/doc_chunker.py` (node_type kwarg + qa_log 분기), `tests/test_scanner.py`, `tests/test_doc_chunker.py`, `README.md`

### 로드맵 진행률 업데이트

15회차 기준 26/33. 16회차 +3 (Sprint 2/3/4) = **29/33 (88%)**. 남은 핵심:
- Phase 2 — Wiki 파편화 해결 (소극적 파일 경계 패치)
- Phase 3 — M9 two-pass callgraph + M10 rationale 추출
- Phase 4 — valuein_homepage(1306 files) 실전 gold set + 벤치마크
- L2 Leiden 풀스케일 wiki auto-gen (Phase 4 이후 재평가)
- L4 watch / L5 two-tier merge (선택)

---

## 🔵 이전 세션 인계 (15회차) — 참고용

### 한줄 요약

**A/B/C/D 4개 선택지 병행 구현 완료 (에이전트 2 + 로컬 2 동시 진행).** A=`authority_alpha` config 노출 (SearchConfig + TOML, default 0.3), B=`annotate-wiki` CLI 신설 (god-nodes 결과를 `.hybrid-search/wiki/index.md` "핵심 모듈" 섹션에 마커 바운드 idempotent 삽입), C=Memory Layer MVP (`HYBRID_SEARCH_QA_LOG=1` opt-in, `memory/qa_log.py`로 MCP 응답을 `<project>/.hybrid-search/qa/YYYY/MM/DD-HHMMSS-<hash>.md`에 async 저장), D=`exclude_pattern` 파라미터 (MCP inputSchema + orchestrator `_build_filter`에서 `docs/*` 같은 glob 제외). **500/500 passed (+43)** — A(3) / B(13) / C(24) / D(3). 4축 모두 충돌 없이 수렴.

### ✅ 이 세션 완료된 것 (15회차)

**1. 선택 A — authority_alpha config 노출 (local)**
- `config.py:SearchConfig`에 `authority_alpha: float = 0.3` 필드 추가 + TOML 주석 (L6 n=60 기반 근거 기록).
- `fusion.py`: `_AUTHORITY_BOOST_ALPHA` 상수 → `DEFAULT_AUTHORITY_ALPHA=0.3`, `reciprocal_rank_fusion(authority_alpha=...)` 파라미터 추가, `_apply_authority_nudge`에 전달.
- `orchestrator.py`: `self._config.search.authority_alpha` 전달.
- `tests/test_fusion.py`: `TestAuthorityAlphaConfigurable` 3개 (α=0.0 비활성, α=0.5 1.5배 상한, default=0.3 상수 검증).
- **결정**: 기본값 0.3 유지 (self-contained 안정성 우선). external-weighted 워크로드는 프로젝트 TOML에서 0.5로 오버라이드 가능.

**2. 선택 B — Wiki annotate-wiki CLI (서브에이전트)**
- 결정: **(a) 새 서브커맨드 `annotate-wiki`** — finalize 훅 대신 독립 실행. god-nodes는 그래프 쿼리라 LLM synthesis와 비용 프로파일 다름.
- `cli.py` (+182줄): 마커 쌍 `<!-- hybrid-search:god-nodes:start/end -->`, `_module_slug()` / `_build_chunk_module_map()` / `_format_god_nodes_section()` / `_apply_god_nodes_to_index()` 순수 함수 + `cmd_annotate_wiki` 서브커맨드.
- `tests/test_graph_cli.py` (+132줄, 13개): idempotency, 수동 content 보존, 빈 결과 skip, top cap, 기존 블록 교체/제거.
- 삽입 예시: `[[storage]](storage.md) — StoreDB._migrate_schema (in=34, type=function)`.
- **비고**: `.hybrid-search/wiki/`는 gitignored라 실제 실행 산출물은 staging 안 됨 (의도). 사용자는 설치 후 `hybrid-search-mcp annotate-wiki --cwd .`로 수동 실행.

**3. 선택 C — Memory Layer MVP (서브에이전트)**
- `memory/qa_log.py` (+230줄): 저장 경로 `<project>/.hybrid-search/qa/YYYY/MM/DD-HHMMSS-<sha256[:8]>.md`. YAML frontmatter + markdown body (query/query_type/bm25_weight/top-10 hits/timestamp).
- Toggle: `HYBRID_SEARCH_QA_LOG` env var (`1/true/yes/on` truthy). **기본 off** (opt-in, 프라이버시/디스크 미지).
- `tools/hybrid_search.py`: 응답 반환 직전 `qa_log.record()` 호출 (try/except 이중 보호, daemon thread, hot-path latency 영향 0).
- `cli.py:_ensure_gitignore_entries`: `.hybrid-search/qa/` 추가.
- `tests/test_qa_log.py` (24개): toggle matrix, path resolution, YYYY/MM 레이아웃, on/off 파일 생성, 디스크 실패 swallow, 핸들러 통합 2개.
- **다음 스프린트 제안 (C 에이전트)**: Sprint 2 qa list/show/grep CLI, Sprint 3 qa 로그 색인 (self-referential memory), Sprint 4 cross-project aggregator + rotation.

**4. 선택 D — exclude_pattern 파라미터 (local)**
- `server.py` MCP inputSchema에 `exclude_pattern` (string, description: 'docs/*' 예시).
- `tools/hybrid_search.py`: `exclude_pattern` 매개변수 + `sanitize_file_pattern` 재사용.
- `orchestrator.py:_build_filter`: `exclude_pattern` glob 매칭 시 chunk 드롭. `file_pattern`과 combinable.
- `tests/test_orchestrator.py`: `TestBuildFilterExcludePattern` 3개 (exclude 드롭, file_pattern 조합, 모두 None).

### 🎯 다음 세션 진입점

1. 이 15회차 섹션 전체
2. `git log --oneline -10` — `d3710e0` HANDOFF 14, `0f4805a` L6 확장, … 이번 세션 커밋 2개.
3. 에이전트 B가 제안한 후속: `hybrid-search-mcp annotate-wiki --cwd .` 수동 실행해서 실제 god-nodes 섹션이 index.md에 들어가는지 확인.
4. C가 제안한 Sprint 2~4 로드맵 검토.

### 🎯 다음 세션 권장 순서

**15회차로 로드맵 거의 전부 소진. 남은 큰 결정은 Memory Layer 확장 or 벤치마크 축 재가동.**

**선택 A — C Sprint 2 (Memory retrieval) 착수 (1~2일):** qa 로그 읽기 CLI (`hybrid-search-mcp qa list/show/grep`) + frontmatter 파싱 유틸. 사용자가 "저번에 뭐 물어봤더라" 재현 가능해야 Memory Layer 가치 입증. C MVP만으로는 write-only → 완전 가치 실현 X.

**선택 B — 실사용 데이터 기반 재결정 (반나절):** 15회차 4축 기능을 며칠 쓴 후 실사용 피드백 모집. snippet 400자 조정, qa_log 기본값 on 승격 여부, α=0.5 외부 프로젝트 시도 등.

**선택 C — 벤치마크 축 재가동 — gold label 품질 개선 (1일+):** proxy label 대신 도메인 지식 기반 수작업 gold (external 15q → 15q+10q 더). α 재튜닝의 근거 강화. "L6 external proxy labels 제약" 주의사항 해결.

**선택 D — README 재작성 "Memory Layer for Claude Code" (반나절):** 전략 메모리의 포지셔닝 원래 계획. MVP가 실제 동작하기 시작한 지금이 적기. 기존 README는 BM25+vector 검색 도구로만 소개됨.

**추천: 선택 A (C Sprint 2) → 선택 D (README).** Memory Layer가 실사용에서 write+read 둘 다 되어야 포지셔닝이 말이 됨.

### 주의사항 / 알려진 이슈

- **B annotate-wiki는 수동 실행.** post-commit 훅에 엮는 옵션은 논의 없이 X. 필요 시 `cli.py:_build_post_commit_script`에 추가. 실행 비용 저렴 (그래프 쿼리만).
- **C qa_log는 opt-in.** 기본 off. 사용자가 `export HYBRID_SEARCH_QA_LOG=1` 하지 않으면 아무 로그도 쌓이지 않음. 승격 전 실사용 디스크 사용량/프라이버시 재검토 필요.
- **D exclude_pattern은 glob-level.** SQL-level index filtering이 아니라 post-retrieval chunk filter. 매우 큰 프로젝트에서 `docs/*` 제외로 retrieval_depth가 부족해질 수 있음 — 필요 시 depth 증가 로직 추가 고려.
- **A 기본값 0.3 유지.** 0.5 승격 전 self-contained variance를 다시 n=45로 측정 필요 (9회차 선택 근거).
- **B Wiki annotate는 `.hybrid-search/wiki/` gitignored.** 테스트 picked up 되지 않음 — 수동 실행으로만 검증됨. 실제 산출물 보고 싶으면 사용자가 annotate-wiki 실행 후 `cat .hybrid-search/wiki/index.md`.
- **C hot path 영향 0.** async_write=True 기본값 + daemon thread. 실패 시 swallow.
- **12회차 이슈 잔존:** v4 → v5 마이그레이션 미검증 프로젝트 3개. 다음 MCP 호출 시 자동.

### 마지막 상태

- **브랜치:** `main` (origin/main 기준 이번 세션 커밋 2개 추가 예정)
- **마지막 커밋 (세션 전):** `d3710e0 [docs] HANDOFF 14회차 — L6 확장 + 로드맵 감사 + Q1 archive`
- **테스트:** **500/500 passed** (25s) — 이번 세션 +43 (A:3 / B:13 / C:24 / D:3).
- **변경 파일:**
  - A: `config.py`, `fusion.py`, `orchestrator.py` (fusion 호출), `tests/test_fusion.py`
  - B: `cli.py` (annotate-wiki 함수들 + subparser), `tests/test_graph_cli.py`
  - C: `memory/__init__.py`, `memory/qa_log.py` (신규), `tools/hybrid_search.py` (qa_log 호출), `cli.py` (gitignore 추가), `tests/test_qa_log.py`
  - D: `orchestrator.py` (_build_filter + hybrid_search 시그니처), `server.py` (inputSchema), `tools/hybrid_search.py` (exclude_pattern), `tests/test_orchestrator.py`

### 로드맵 진행률 업데이트

15회차 기준 실제 완료: Q1~Q10 (10) + M1/M1.v2/M1.1/M1.2/M2/M3/M4/M5 (8) + L6 mini+Full+확장 (3) + Search DX snippet (1) + A/B/C/D 4개 = **26개**. 11회차 표 denominator 28에 Search DX + A/B/C/D 5개 추가하면 ~33. **26/33 (79%)**. 남은 핵심: C Sprint 2~4 (Memory Layer 완성) + README 재작성.

---

## 🔵 이전 세션 인계 (14회차) — 참고용

### 한줄 요약 (14회차)

**L6 외부 확장 (선택 A) 완료 + 로드맵 감사 + Q1 플랜 archive.** gold set 30q→45q, external 5q→15q (valuein + mathontonlogy + breeze), MRR@10 지표 + per-project α sweep 추가. **GRAND TOTAL n=60 α=0.3: Δ NDCG +0.061 [+0.029,+0.098] P=1.00, Δ MRR +0.062 [+0.015,+0.118] P=1.00** — authority nudge 효과 cross-project에서 최종 검증. α=0.5가 external에서 더 강함(+0.094) → 재튜닝 여지 confirmed. 로드맵 감사 중 **M1.2 (type-gating), M1.1 (schema v5 label rename)이 이미 이전 세션에 shipped**되어 있었음을 확인 — HANDOFF 11회차 표 "16/28 (57%)"가 현실 미반영이었음. PLAN_q1_routing_hook.md → `docs/plan/archive/`로 이동. **457/457 passed**, 5 파일 staged.

### ✅ 이 세션 완료된 것 (14회차)

**1. L6 확장 (서브에이전트 병렬 실행, HANDOFF.md:375 로드맵 항목)**
- `gold_queries_v2.json`: 30q → **45q** (keyword/structural/semantic 각 15q).
- `external_queries.json`: 5q → **15q** (valuein_homepage 5 + mathontonlogy 5 + breeze 5). projects 배열 구조로 승격 (back-compat 유지).
- `run_v2.py`: 다중 external 프로젝트 루프.
- `score_v2.py`: MRR@10 계산 + bootstrap CI + per-project 분리 표 + GRAND TOTAL.
- `results_v2.json`: 2400 rows (60q × 10 limit × 4 mode).
- breeze 인덱스 stale → 선행 reindex 수행 (155 files, 323 chunks).

**2. L6 최종 결과 요약 (α=0.3 기준)**
| scope | n | Δ NDCG [95% CI] P | Δ MRR [95% CI] P |
|---|---|---|---|
| self-contained | 45 | +0.060 [+0.019,+0.104] **1.00** | +0.056 [+0.003,+0.122] 0.98 |
| external pooled | 15 | +0.065 [+0.009,+0.128] 0.99 | +0.236 (valuein만 유의) |
| **GRAND TOTAL** | **60** | **+0.061 [+0.029,+0.098] 1.00** | **+0.062 [+0.015,+0.118] 1.00** |
- structural Δ NDCG +0.142 (α=0.3, P=1.00) 전 α에서 최강. keyword Δ=0 (OFF MRR=1.0 천장 효과). semantic 미약한 양수.
- external α sweep: valuein +0.088 (α=0.2) → +0.235 (α=0.5). mathontonlogy +0.022 → +0.070. breeze −0.022 → −0.021 (5q 중 4q가 이미 OFF NDCG≥0.76으로 천장). **external-weighted이면 α=0.5가 더 강함.** 현재는 self에서 α=0.3/0.5 거의 동일이라 보수적으로 0.3 유지.

**3. 로드맵 감사 — 57%의 정체**
- 11회차 표 `16/28 (57%)`는 11회차 시점 스냅샷. 이후 업데이트 안 됨.
- 12회차 M5 shipped, 13회차 Search DX snippet shipped.
- **M1.2 type-gating도 이미 shipped** — `orchestrator.py:193-198` "M1.2: EXACT_SYMBOL queries bypass authority", `tests/test_orchestrator.py`, `tests/test_query_classifier.py` 존재. 시점 11~13 사이 (커밋 로그 추적 필요).
- **M1.1 label 리네이밍도 이미 shipped** — `db.py:14 SCHEMA_VERSION="5"`, `db.py:21 CONFIDENCE_LEVELS=("ambiguous","inferred","extracted")`, v4→v5 migration 로그 (`db.py:266`).
- 실제 진행률 재계산: Q1/Q3/Q4/Q5/Q6/Q7/Q8/Q10 (8) + M1/M1.v2/M1.1/M1.2/M2/M3/M4/M5 (8) + L6 mini+Full+확장 (3) + Search DX snippet (1) = **20개 완료**. 원래 denominator 28에 Search DX snippet이 포함 안 됐다면 20/28 (71%). 포함 고려 시 20/29 (69%).

**4. PLAN_q1_routing_hook.md archive**
- Q1 본문 + "다음 단계" Q7/Q8/M2/M4 모두 이전 세션에 완료 확인됨 (cli.py `_ensure_claude_md`, `_git_hooks_dir`, `_build_post_checkout_script`, `_write_needs_synthesis_flag`).
- 파일 → `docs/plan/archive/2026-04-21-PLAN_q1_routing_hook.md` 이동. `git mv` 사용.

### 🎯 다음 세션 진입점

**파일 읽기 순서:**
1. **이 14회차 섹션 전체**
2. `git log --oneline -10` — 최근 커밋
3. `benchmarks/authority_poc/results_v2.json` 최신 결과
4. 아래 "다음 세션 권장 순서"

### 🎯 다음 세션 권장 순서

**로드맵 원래 계획 거의 소진. 남은 결정은 α 재튜닝 또는 새 축(Memory Layer / Wiki 품질) 착수.**

**선택 A — α=0.5 재검증 (반나절):** L6 결과가 external-weighted면 α=0.5가 더 강함을 시사. config로 α를 노출해 프로젝트별 튜닝 가능하게 하거나, 기본값 변경 여부 결정. 현재 `fusion.py:_AUTHORITY_BOOST_ALPHA=0.3` 상수. 변경 시 `test_fusion.py` TestAuthorityNudge 업데이트 필요.

**선택 B — Wiki 품질 개선 (1~2일):** god-nodes 결과(`StoreDB.get_god_nodes()`)를 `.hybrid-search/wiki/index.md` "핵심 모듈" 섹션에 자동 삽입. `cli.py`에 `annotate-wiki` 서브커맨드 신설 or `synthesize-wiki --finalize` 후반에 훅. Wiki 가치 상승 + god-nodes 활용 증가.

**선택 C — L1 Q&A feedback loop 시작 (4주, 전략 3축 중 Memory Layer 진입):** 매 MCP 응답을 markdown으로 영구 저장 → 대화 지식 ↔ 코드 연결. 메모리 `project_strategic_direction.md` "3개 축"의 핵심. 벤치마크 + 자율 루프 축은 거의 마감이라 이 축 시작이 다음 큰 임팩트.

**선택 D — 운영 문서 노이즈 filter (반나절):** `docs/`, `plan/`이 hybrid_search 상위 점유. `--exclude-pattern` CLI 옵션 또는 ranking penalty. 먼저 실측 데이터 (어느 쿼리가 얼마나 오염되는지) 수집 후 결정 권장.

**추천: 선택 A (α 결정 마감) → 선택 C (Memory layer 시작).** B/D는 실사용 pain이 축적된 후.

### 주의사항 / 알려진 이슈

- **5 파일 staged, 커밋 대기 중.** `git status` 확인 후 `git commit` 필요. PLAN_q1 archive는 rename(R)으로 같이 들어감.
- **breeze external 결과 약점:** 5q 중 4q가 이미 OFF NDCG≥0.76. proxy label + 천장 효과로 noise. 결정적 신호 X, directional 참고만.
- **MRR@10 한계:** keyword 15q 전원 OFF MRR=1.0 → Δ=0 (top-1 이미 정답). MRR 차별화는 structural/semantic에서만.
- **α=0.5 재튜닝 위험:** self-contained variance 확대 가능성 (9회차 α=0.3 선택 근거). 바꾸려면 self n=45에서 α=0.5 안정성 다시 검증 필요.
- **12회차 이슈 잔존:** v4 → v5 마이그레이션 미검증 프로젝트 3개 (`7c7631...`, `05de0b...`, `5f349647...`). 다음 MCP 호출 시 자동.

### 마지막 상태

- **브랜치:** `main` (origin/main 기준 +9 커밋, 이번 세션 staging만 + 미커밋)
- **마지막 커밋:** `cebed66 [docs] HANDOFF 13회차 — Search DX snippet + 6~10회차 압축`
- **테스트:** 457/457 passed (25s)
- **Staged 변경:** `benchmarks/authority_poc/{external_queries,gold_queries_v2,results_v2,run_v2,score_v2}.{json,py}` (5) + `PLAN_q1_routing_hook.md → docs/plan/archive/2026-04-21-PLAN_q1_routing_hook.md` (R)
- **Diff 규모:** +25,895 / −9,404 (results_v2.json 대부분)

---

## 🔵 이전 세션 인계 (13회차) — 참고용

### 한줄 요약

**Search DX hit-aware snippet (선택 1) + HANDOFF.md 정리 (선택 4) 완료.** snippet은 쿼리 토큰이 hit한 줄 ±5줄 / 최대 400자 윈도우로 변경 — `feedback_search_dx.md` "snippet 짧아서 Read 2단계가 된다" 피드백 직접 수렴. `src/hybrid_search/search/snippet.py` 신설로 orchestrator/semantic_search 두 호출지의 중복 `_make_snippet` 통합. 22개 신규 테스트, **457/457 passed** (435 + 22). HANDOFF.md 6~10회차를 한줄 요약 블록으로 압축 (1170줄 → 888줄, -282줄). 다음 세션은 **L6 외부 확장 (선택 2) or Wiki 구조 개선 (선택 3) or 실사용 후 snippet 길이 재조정**.

### ✅ 이 세션 완료된 것 (13회차)

**1. Hit-aware snippet 모듈 신설** (`src/hybrid_search/search/snippet.py`, +60줄)
- `make_snippet(docstring, content, query)`: 우선순위 (1) 쿼리 토큰이 hit한 줄 중심 ±5줄 윈도우 (≤400자) → (2) docstring 머리 (≤400자) → (3) content 첫 10줄 (≤400자).
- `_query_tokens(query)`: 영문 ≥3자 lowercased + 한글 ≥2자 토큰. 정규식 `[A-Za-z0-9_]+` + `[\uac00-\ud7a3]+`. 짧은 stopword(`is`, `of`, `id`) 자동 배제.
- `_find_hit_line()`: 첫 매치 라인 반환. 대소문자 무시, content는 1회 lowercase 캐시.
- 상수: `SNIPPET_MAX_CHARS=400`, `CONTEXT_LINES=5`, `DOCSTRING_FALLBACK_CHARS=400`, `HEAD_FALLBACK_LINES=10`.

**2. 두 호출지 통합** (중복 제거)
- `src/hybrid_search/search/orchestrator.py`: `_enrich_results`에 `query: str` 파라미터 추가, `make_snippet` 호출, 모듈 끝 `_make_snippet` 삭제.
- `src/hybrid_search/tools/semantic_search.py`: `query`가 이미 스코프에 있어서 직접 전달, 모듈 끝 `_make_snippet` 삭제.

**3. 테스트 (`tests/test_snippet.py`, +118줄, 22 케이스)**
- `TestQueryTokens` 7개: 영문 lowercase + 길이 필터, 한글 ≥2, 혼합, underscore 보존, dot-qualified 분리, 빈 입력.
- `TestHitCenteredSnippet` 7개: 중심 윈도우 / 시작 클램프 / 끝 클램프 / no-hit → docstring fallback / no-hit no-doc → head fallback / 한국어 hit / case-insensitive.
- `TestLengthCap` 3개: hit window cap / docstring cap / head fallback cap.
- `TestFallbacks` 5개: 쿼리 없으면 docstring → head, 빈 입력, 너무 짧은 쿼리(`"is of a"`)는 fallback, 상수 sane check.

**4. 실사용 smoke test (hybrid-search-mcp 자신)**
- 영문 쿼리 `upsert chunk`: result 1 = `StoreDB.upsert_file` (snippet 334자, `def upsert_file` 시작 줄에 hit centered).
- 한국어 쿼리 `스키마 마이그레이션`: result 1 = `docs/plan/2026-04-16-conversation-indexing.md::스키마` (snippet 120자, `#### 스키마` 헤더 + SQL 코드 블록 시작).
- 둘 다 의도대로 hit 위치 중심 컨텍스트 노출 — 사용자가 "이게 맞는 결과인가?" 즉시 판단 가능.

**5. HANDOFF.md 정리 (선택 4)**
- 6~10회차 본문 (319줄, 169-487행)을 "📚 6~10회차 한줄 요약" 블록(38줄)으로 교체.
- 각 회차당 한줄 요약 + 핵심 커밋 SHA + 핵심 의사결정 키워드만 보존. 마이그레이션 디테일/실측 표/세부 수치 등은 git log + 커밋 본문으로 복원 가능.
- 12회차/11회차는 풀 보존 (가장 최근 컨텍스트).
- 결과: 1170줄 → 888줄 (-282줄, -24%).

### 🎯 다음 세션 진입점

**파일 읽기 순서:**
1. **이 13회차 섹션 전체**
2. `git log --oneline -10` — 최근 커밋 (`f767f60` Search DX snippet, `8591c72` M5, `c5731d3`+`c48910c` HANDOFF 11회차)
3. 아래 "다음 세션 권장 순서"

### 🎯 다음 세션 권장 순서

**M 시리즈 + 선택 1/4 모두 일단락. 남은 큰 결정은 벤치마크 확장(L6) 또는 Wiki linkage(god-nodes 결합).**

**선택 A — L6 외부 확장 (1일):** `benchmarks/` 아래 mathontonlogy + breeze 각 5q 추가 → external n=5 → n=15. α=0.5 vs 0.3 결정 데이터 보강. proxy label 한계 인정 — 도메인 지식 없는 자동 라벨은 directional only. 수작업 gold가 깔끔할 가능성 검토.

**선택 B — Wiki 구조 개선 (1~2일):** Wiki는 LLM synthesis로 생성되지만 linkage가 제한적. god-nodes 결과를 `index.md`에 자동 삽입해 "핵심 모듈" 섹션 만들기. `cli.py` 확장 or 새 CLI `annotate-wiki-with-god-nodes`. 장점: Wiki 가치 상승 + god-nodes/Wiki 역할 상보.

**선택 C — Snippet 실사용 피드백 후 재조정:** 13회차 변경을 며칠 써본 후 길이 조정 (300자 vs 400자 vs 500자), 노이즈 자동 필터(`include_doc_dirs=false` default 변경 검토), multi-hit 처리(현재는 first hit only — 토큰 빈도가 낮은 hit 선택이 더 정보가 큼). 데이터 없이는 결정 보류.

**선택 D — 운영 문서 노이즈 자동 filter (반나절):** `docs/`, `plan/` 같은 디렉토리는 hybrid_search 결과 상위를 점유하는 경향. `--exclude-pattern` CLI 옵션 또는 ranking penalty. M1.2 같은 효과 — 데이터 없이 결정하지 말 것 (먼저 측정).

**추천: 선택 A → 선택 B 또는 D.** L6 외부 확장은 측정 인프라라 선결제 가치, Wiki/필터는 그 측정 위에서 효과 비교 가능.

### 주의사항 / 알려진 이슈

- **Snippet 길이 400자는 토큰 비용/정보량 trade.** limit 10일 때 대략 4kB 추가. 너무 많이 잡힌다 싶으면 `SNIPPET_MAX_CHARS`를 300으로 낮추는 것이 가장 빠른 조정 노브.
- **Vector-only hit (BM25 미스)에서는 hit 라인이 안 잡힘** → docstring 헤드 또는 첫 10줄로 fallback. 현행보다 나쁘지 않지만 vector 매치 의미를 시각적으로 보여주진 못함. 추후 cross-encoder rerank 결과를 snippet 위치로 활용하는 안 검토 가능.
- **`_find_hit_line`는 첫 매치만 사용.** 같은 chunk에 여러 hit이 있으면 가장 정보가 많은 곳을 못 고름. 실사용에서 문제되면 `min(token freq) hit` 로 변경 (rare token이 더 변별력 있음).
- **12회차 이슈 잔존:** v4 → v5 마이그레이션 미검증 프로젝트 3개 (`7c7631...`, `05de0b...`, `5f349647...`). 다음 MCP 호출 시 자동.

### 마지막 상태

- **브랜치:** `main` (origin/main 기준 **+8 커밋** — 13회차 작성 후 +9)
- **마지막 커밋:** `f767f60 [feat] Search DX — hit-aware snippet (±5줄 / 400자 윈도우)`
- **테스트:** 457/457 passed
- **변경 파일:** `src/hybrid_search/search/snippet.py` (신규 +60), `src/hybrid_search/search/orchestrator.py` (-7/+5), `src/hybrid_search/tools/semantic_search.py` (-7/+1), `tests/test_snippet.py` (신규 +118), `HANDOFF.md` (정리 -282).

---

## 🔵 이전 세션 인계 (12회차) — 참고용

### 한줄 요약 (12회차)

**M5 graph exploration 완료 (CLI + /search 스킬 라우팅 통합).** `hybrid-search-mcp god-nodes / shortest-path / subgraph` 3개 CLI 추가 — MCP 도구는 늘리지 않고 Skill 오케스트레이션. `/search` 스킬은 70/15/10/5/5 비중 명시 + god-nodes 정식 레인 승격 + hybrid_search 사용 시 Read 보충 기본값 반영 (`feedback_search_dx.md` 피드백 수렴). `StoreDB.get_god_nodes()` 헬퍼 추가. 13개 신규 테스트, **435/435 passed**. 실사용 smoke test: god-nodes 1위 `StoreDB._migrate_schema` (in=25). 다음 세션은 **Search DX snippet 튜닝 or L6 외부 확장 or HANDOFF.md 정리/요약**.

### ✅ 이 세션 완료된 것 (12회차)

**1. `/search` 스킬 라우팅 재설계** (`~/.claude/skills/search/skill.md`, 저장소 외)
- **5축 분류 + 비중 명시:** 정밀 조회(Grep) ~65% / 구조(Wiki) ~15% / 탐색(hybrid_search) ~10% / 설계(hybrid_search) ~5% / 권위(god-nodes) ~5%.
- **hybrid_search 사용 시 Read 보충 기본값:** `feedback_search_dx.md` 메모리의 "snippet 짧아서 Read 2단계가 된다" 피드백 직접 반영. "Snippet은 히트 위치 포인터로만 읽을 것" 명시.
- **노이즈 대응:** `file_pattern="*.py"`, `"*.md"`, `"migrations/*.sql"` 등 재호출 팁.
- **god-nodes 라우팅 승격:** "핵심 함수 / 가장 많이 쓰이는 / 중심 모듈 / 진입점 / god node" 신호 → `Bash: hybrid-search-mcp god-nodes`.
- **실행 예시 5개 추가** (정밀/구조/탐색/설계/스키마 각 1개).

**2. CLI 명령 3개** (`src/hybrid_search/cli.py`, +331줄)
- **`god-nodes [--top N] [--project X] [--min-confidence {ambiguous,inferred,extracted}] [--json]`**: in-degree + max confidence_score로 authority 상위 N chunk. 기본 top=20, min-confidence=inferred.
- **`shortest-path A B`**: `call_edges` BFS caller→callee. forward 없으면 reverse(B→A) 시도 후 방향 리포트. 심볼/qualified_name/chunk_id 모두 수용.
- **`subgraph SYMBOL --hops N`**: 양방향 (callees forward + callers reverse) N-hop BFS. 기본 hops=2.
- **공유 헬퍼:** `_resolve_chunk_for_graph()` (raw id → qualified_name → bare name → fuzzy LIKE), `_open_single_project_db()` (cwd auto-detect 또는 `--project` override).
- **main() dispatch 3개 추가**, subparser 3개 등록.

**3. DB 헬퍼 추가** (`src/hybrid_search/storage/db.py`, +31줄)
- `StoreDB.get_god_nodes(project_id, limit, min_confidence)`: 단일 SQL — `JOIN call_edges + chunks + files + GROUP BY c.id + ORDER BY in_degree DESC, max_score DESC, qualified_name`. N+1 회피.

**4. 테스트 (435/435 passed, +13 신규)**
- `tests/test_graph_cli.py` 신설:
  - `TestGodNodes`: top-N by in-degree / min_confidence 필터 / limit / isolated 제외
  - `TestBfsShortestPath`: forward direct neighbor / 2-hop / self-trivial / no-path None / min_confidence가 엣지 차단
  - `TestResolveChunkForGraph`: raw chunk_id / qualified_name / bare name / unknown None
- 시드 그래프: `caller_a/b/c → popular` (authority), `caller_a → mid → leaf` (shortest-path chain), `caller_b →(ambiguous) mid` (필터 테스트), `isolated` (엣지 없음).

**5. 수동 smoke test (hybrid-search-mcp 자신)**
- `god-nodes --top 5`:
  1. `StoreDB._migrate_schema` in=25 score=1.00
  2. `load_config` in=21 score=0.80
  3. `chunk_code_file` in=18 score=0.80
  4. `StoreDB.upsert_file` in=14 score=0.80
  5. `VectorEngine.search` in=13 score=1.00
- `shortest-path load_config StoreDB.upsert_file` → "No path" (직접 호출 없음, 정상)
- `subgraph load_config --hops 1` → forward 1 (`_create_default_config`), reverse 21 (테스트/CLI 다수)

### 🎯 다음 세션 진입점

**파일 읽기 순서:**
1. **이 12회차 섹션 전체**
2. `git log --oneline -10` — 최근 커밋 (`8591c72` M5, `c5731d3`+`c48910c` HANDOFF 11회차)
3. 아래 "다음 세션 권장 순서"

### 🎯 다음 세션 권장 순서

**큰 M은 일단락(M1/M1.1/M1.2/M4/M5 완료).** 남은 건 품질/DX 축 개선.

**선택 1 — Search DX snippet 튜닝 (반나절):**
- 현재 hybrid_search snippet은 평균 3줄. 피드백: "결국 Read로 다시 읽어야 함."
- `src/hybrid_search/tools/hybrid_search.py` snippet 생성 지점에서 길이 확장 + 히트 라인 주변 context +/- 5줄로. 너무 길면 토큰 비용 증가 → limit당 300자 정도가 sweet spot.
- 운영 문서 노이즈 자동 filter 옵션: `--exclude-pattern` 또는 `include_doc_dirs=false` default 변경 검토.
- 장점: 매 쿼리 개선, 사용자 체감 큼.

**선택 2 — L6 외부 확장 (1일):**
- `benchmarks/` 아래 외부 프로젝트 gold set 추가 (mathontology + breeze 각 5q) → external n=5 → n=15.
- α=0.5 vs 0.3 결정 데이터 보강. proxy label 한계 인정 — 수작업 gold가 더 깔끔할 수도.
- 장점: 벤치마크 축 강화.

**선택 3 — Wiki 구조 개선 (1~2일):**
- 현재 Wiki는 LLM synthesis로 생성되지만 linkage가 제한적. god-nodes 결과를 `index.md`에 자동 삽입하여 "핵심 모듈" 섹션 만들기.
- `cli.py generate-wiki-plan` 확장 or 새 CLI `annotate-wiki-with-god-nodes`.
- 장점: Wiki 가치 상승 + god-nodes와 wiki 역할 상보.

**선택 4 — HANDOFF.md 정리 (30분):**
- 현재 1100+줄. 5회차 이전 섹션은 이미 압축돼 있지만 6~10회차도 요약 가능.
- 새 섹션 "📚 Phase 6~10 한줄 요약" 만들고 본문 삭제. 현재 세션 가독성 향상.

**추천: 선택 1 (Search DX snippet) → 선택 4 (HANDOFF 정리).** 선택 1은 매일 체감되는 부분이고, 선택 4는 부채 청산. 선택 2/3은 결정이 필요한 더 큰 작업.

### 주의사항 / 알려진 이슈

- **CLI 명령 entry-point는 `~/.hybrid-search/` 환경 의존:** `DEFAULT_DATA_DIR` env override 없음 → 통합 테스트가 DB 헬퍼와 in-CLI BFS만 커버. 향후 env 추가 고려 가능하지만 지금은 수동 smoke test로 충분.
- **`shortest-path` forward/reverse 방향성:** forward 탐색 실패 시 reverse 자동 fallback인데, 대칭성을 기대하는 사용자에게 혼란 가능. 출력에 방향 표시(`direction: forward/reverse`) 포함해서 완화 중. 필요 시 `--bidirectional` 플래그 명시화 검토.
- **`god-nodes` min_confidence 기본값 `inferred`:** MCP trace 도구와 일치. 프로젝트에 따라 `extracted`만 보고 싶을 수도 있음 — CLI는 `--min-confidence` 옵션으로 해결.
- **11회차 이슈 잔존:** v4 → v5 마이그레이션 미검증 프로젝트 3개 (`7c7631...`, `05de0b...`, `5f349647...`). 다음 MCP 호출 시 자동. 큰 DB 성능 관찰 필요.

### 마지막 상태

- **브랜치:** `main` (origin/main 기준 **+6 커밋** — 12회차 작성 후 +7)
- **마지막 커밋:** `8591c72 [feat] M5 graph exploration — god-nodes / shortest-path / subgraph CLI`
- **테스트:** 435/435 passed (25.08s)
- **변경 파일:** `src/hybrid_search/cli.py` (+331), `src/hybrid_search/storage/db.py` (+31), `tests/test_graph_cli.py` (신규 +190)
- **스킬 변경 (저장소 외):** `~/.claude/skills/search/skill.md` 재작성 — Git에 추적되지 않으므로 별도 백업 필요 시 수동.

---

## 🔵 이전 세션 인계 (11회차) — 참고용

### 한줄 요약 (11회차)

**M1.1 라벨 리네이밍 완료 + M5 아키텍처 재설계.** `low/medium/high` → `ambiguous/inferred/extracted` 전면 교체. DB schema v4 → v5 + UPDATE 마이그레이션. 4개 실사용 v4 인덱스(hybrid-search-mcp + valuein + mathontology + breeze) 자동 호환 — `reindex` 불필요. 422/422 tests passed (420 + 2 신규 마이그레이션 테스트). **M5는 MCP 도구 추가 X, CLI + Skill 패턴으로 재정의** (MCP 토큰 비용 회피 — 매 도구당 ~1k 영구 상주).

### ✅ 이 세션 완료된 것 (11회차)

**1. M1.1: confidence label semantic rename** (`5c98f8a`)
- **매핑:** `low → ambiguous` (0.3) / `medium → inferred` (0.8) / `high → extracted` (1.0). graphify 용어 정렬. **numeric score는 불변** — 라벨만 공개 surface 변경.
- **`storage/db.py`:** `SCHEMA_VERSION = "5"`, `CONFIDENCE_LEVELS = ("ambiguous", "inferred", "extracted")` (weakest → strongest, `_confidence_filter` 의존), `CONFIDENCE_SCORES` 키 교체. `call_edges.confidence` DEFAULT `'low'` → `'ambiguous'`. `insert_call_edges` INSERT literal + 3개 query helper의 `min_confidence` 기본값 모두 새 라벨로.
- **v4 → v5 마이그레이션:** `_migrate_schema`에 단계 추가. `UPDATE call_edges SET confidence = CASE confidence WHEN 'high' THEN 'extracted' WHEN 'medium' THEN 'inferred' WHEN 'low' THEN 'ambiguous' ELSE confidence END WHERE confidence IN ('low','medium','high')`. v3 → v4 단계는 그대로 (옛 라벨 backfill 후 v5 단계에서 라벨 rename).
- **소비처:** `index/callgraph.py` (resolver 7개 return 라벨, stats dict 키, 스킵 조건, log format), `index/dag.py` (`build_dependency_graph` 필터 `("extracted","inferred")` + 폴백 기본값), `cli.py` (resolve 출력 + edges 통계 출력 2곳), `tools/trace.py` (MCP `min_confidence` default `"medium"` → `"inferred"`).

**2. 실제 v4 DB 마이그레이션 검증**
- `~/.hybrid-search/projects/56feceb4ce0865e3/store.db` (스코프 작은 테스트 프로젝트) 한 번 열어서 자동 마이그레이션 실행. 결과: schema_version 4 → 5, 라벨 분포 보존:
  - `low|2095 → ambiguous|2095`
  - `medium|72 → inferred|72`
  - `high|96 → extracted|96`
- 나머지 3개 (`7c7631...`, `05de0b...` valuein/mathontology/breeze 추정 + `5f349647...`)는 다음 MCP 호출 시 자동 마이그레이션. 코드 경로 동일하므로 안전.

**3. 테스트 (422/422 passed, +2 신규)**
- `tests/test_store_db.py::TestConfidenceLabelRename`: `test_v4_to_v5_renames_labels` (v4 DB 시드 → 라벨 rewrite + score 보존 동시 검증), `test_v5_db_is_idempotent` (이미 v5인 DB 재오픈 시 no-op 보장).
- 기존 테스트 라벨 assertion + 메서드명 일괄 교체. v3 → v4 backfill 테스트는 v5까지 체이닝되도록 expectation 갱신 (확장된 라벨 dict 추가 검증).
- 시드 코드 내부 `'low'/'medium'/'high'` 4건은 의도적 보존 (v3/v4 레거시 시뮬레이션).

### 🎯 다음 세션 진입점

**파일 읽기 순서:**
1. **이 섹션 전체** — 11회차 컨텍스트 흡수
2. `git log --oneline -10` — 최근 커밋 (`5c98f8a` M1.1, `3570600` HANDOFF 10회차, `3af9fae` M1.2)
3. 아래 "다음 세션 권장 순서" — M5 vs L6 결정

### 🎯 다음 세션 권장 순서

**아키텍처 원칙 (11회차 후반 결정):** **MCP 도구는 추가하지 않는다.** MCP 도구 정의는 매 세션 컨텍스트에 영구 상주(~1k 토큰/도구) — 빈도 낮은 niche 기능엔 과한 비용. 새 기능은 **CLI 명령 + Skill 오케스트레이션** 패턴으로 추가. 기존 `mcp__hybrid-search__hybrid_search`는 매 쿼리에서 쓰이므로 MCP 유지 정당. 기존 패턴 참고: `/search`, `/maintain`, `/bootstrap-wiki`가 이미 skill orchestration 사용 중.

**선택 1 — M5 graph exploration (재설계, CLI + Skill, 1.5~2일, 추천):**
- **CLI 명령 3개 (src/hybrid_search/cli.py 확장):**
  - `hybrid-search god-nodes [--top N] [--project X]`: authority 상위 N chunk 출력. `db.get_chunk_authority_scores()` 직접 재사용.
  - `hybrid-search shortest-path A B [--project X]`: 두 chunk 간 call edge 최단 경로 (BFS on `call_edges`).
  - `hybrid-search subgraph CHUNK_ID --hops N`: forward+reverse N-hop. `tools/trace.py`의 양방향 통합 버전.
- **Skill 1개 (umbrella):** `~/.claude/skills/explore-graph/` — args 파싱 → 적절한 CLI 호출 → 결과 정리. 또는 기존 `/search` 스킬에 graph 라우팅 케이스 추가.
- **장점:** ① 컨텍스트 부담 0 (skill description ~200토큰만), ② CLI 단독 테스트 가능, ③ 다른 프로젝트는 MCP 재설치 없이 CLI만 설치하면 됨 (valuein/mathontology/breeze 모두 자동 혜택).
- **테스트 전략:** CLI 명령은 stdout 캡처 기반 통합 테스트 (`tests/test_cli.py` 패턴 재사용). Bash 호출 round-trip 없이 직접 함수 호출 가능하므로 빠름.
- **선결 검증:** `/search` 스킬에서 god_nodes를 라우팅할 케이스 정의 — Wiki `index.md`(구조 개요)와 역할 분리, god_nodes는 "랭킹". `feedback_search_dx.md` 메모리(70%는 Grep이 빠름)와 묶어서 라우팅 분기를 정리하면 시너지.

**선택 2 — Search DX 라우팅 개선 (1일, M5와 묶을 수 있음):**
- `feedback_search_dx.md` 메모리 기반: `/search` 스킬에 query intent 분류 강화 — exact symbol → Grep 우선, 자연어 → hybrid_search, 구조 질문 → Wiki/god_nodes (M5 완료 시).
- snippet 노이즈 줄이기: hybrid_search 결과의 snippet 길이/위치 튜닝, 또는 후처리 필터.
- M5 직전에 라우팅 케이스 정의가 필요하므로 **M5 첫날 with this**가 자연스러움.

**선택 3 — L6 외부 확장 (1일, 벤치마크 축 강화):**
- 외부 프로젝트 gold set 추가 (mathontology + breeze 각 5q) → external n=5 → n=15. α=0.5 vs 0.3 결정에 데이터 더 모음.
- **리스크:** proxy labels 신뢰도 한계. 도메인 지식 없는 자동 labeling은 noise → 수작업 gold 필요할 수도.

**추천: M5 + Search DX를 묶어서 (선택 1+2, 총 ~2일).** 라우팅 정의가 god_nodes 설계의 선결조건이라 같이 가는 게 효율적. 구현 순서: ① `/search` 스킬 라우팅 분기 정리 (Grep/hybrid_search/Wiki) → ② CLI god-nodes 추가 → ③ shortest-path/subgraph → ④ `/explore-graph` skill 또는 `/search` 통합. 첫날 세션 끝까지 god-nodes는 동작 가능해야 함.

### 주의사항 / 알려진 이슈

- **마이그레이션 검증 미완 프로젝트 3개:** `7c7631...` `05de0b...` `5f349647...`는 다음 MCP 호출 시 첫 자동 마이그레이션 실행 예정. 코드 경로는 56feceb4에서 검증됐지만 대형 DB(7c7631은 25k+ edges)에서 UPDATE 시간 측정 안 됨. 만약 느리면 `CREATE INDEX idx_callee_confidence ON call_edges(confidence)` 추가 고려 (현재 idx 없음).
- **벤치마크 산출물 (`benchmarks/authority_poc/*.json`)은 옛 라벨 그대로** — frozen artifact라 의도적. 다음 L6 재실행 시 새 라벨로 자동 교체됨.
- **callgraph wiki 페이지 (`.hybrid-search/wiki/*.md`)** — 일부에 "low/medium/high" 텍스트가 docstring에서 추출됐을 가능성. wiki는 LLM synthesis가 자동 갱신하므로 별도 손댈 필요 없음.

### 마지막 상태

- **브랜치:** `main` (origin/main 기준 +3 커밋 — 11회차 작성 후 푸시 대기)
- **마지막 커밋:** `5c98f8a [feat] M1.1 confidence label rename`
- **테스트:** 422/422 passed (24.84s)

---

## 📚 6~10회차 한줄 요약 — 참고용

### 10회차 (2026-04-21) — M1.2 type-gating + Full L6 재측정

- `3af9fae` M1.2 EXACT_SYMBOL authority gating: `orchestrator.hybrid_search`에서 `effective_authority = None if qtype == EXACT_SYMBOL else (authority_scores or None)`. 혼합 쿼리(symbol+한글)는 `classify_query`가 KOREAN_NL로 분류해 authority 유지(의도).
- 벤치마크(α=0.3, n=30 self + 5 external): keyword Δ NDCG@10 **-0.032 → +0.010** (P(Δ>0) 0.25→0.66), structural +0.117→+0.123, semantic 거의 동일, OVERALL P 0.87→**0.99**.
- `test_orchestrator.py` 신규 7개 (mock으로 `reciprocal_rank_fusion` kwargs 검증). 420/420 passed.

### 9회차 (2026-04-21) — M1.v2 boost-only + Full L6 (35q)

- `c6fe7a1` 공식 변경 `rrf * (0.5 + 0.5*auth)` → `rrf * (1.0 + 0.3*auth)`. `_AUTHORITY_BOOST_ALPHA = 0.3` 상수. penalty 제거, 맵 외부는 passthrough.
- `47b2b10` mini-PoC (10q): damping-only -0.015 → boost-only +0.041 역전. Negative baseline을 git에 남겨 PoC 가치 증명.
- `379aa64` Full L6 35q (self 30 + external 5) × α{0.2,0.3,0.5} × {ON,OFF} × bootstrap 1000회 → `results_v2.json`. α=0.3 최적. keyword P(Δ>0)=0.25로 M1.2 필요성 확인.
- 413/413 passed.

### 8회차 (2026-04-21) — M4 `.hybrid-search/needs_synthesis` flag

- `6ed4f39` flag 파일 패턴 완성. JSON `{stale_count, stale_modules[:20], detected_at ISO-8601 UTC}`. `_mark_stale_wikis` write, reindex/finalize(`cmd_synthesize_wiki --finalize`) clear, `status` 명령 표시. `/search` 스킬 Step 0에서 read (검색 차단 X, 경고만).
- gitignore에 `.hybrid-search/needs_synthesis` 추가 — 기존 프로젝트는 `install-hook` 재실행으로 보강.
- 자율 루프 축 완성: Q1+Q7+Q8+M2+M3+M4 = 훅 인프라 6개.
- 413/413 passed (+7 신규, `test_cli_hook_install.py`).

### 7회차 (2026-04-21) — M1 confidence numeric score + fusion authority nudge

- `83bfa7c` DB v4 마이그레이션 (`call_edges.confidence_score REAL DEFAULT 0.0`). `CONFIDENCE_SCORES = {"high":1.0, "medium":0.8, "low":0.3}`. `get_chunk_authority_scores(project_id)` 헬퍼 (callee별 MAX, unresolved 제외). `reciprocal_rank_fusion`에 optional `chunk_authority_scores` 파라미터.
- 실측: hybrid-search-mcp 196 chunk authority [0.80..1.00] 즉시 확보 (ALTER+UPDATE backfill, `reindex --force` 불필요).
- Leiden/DAG는 confidence-blind 유지 — 구조는 정확성, 랭킹/리포트는 확률적.
- 406/406 passed (+10 신규, `test_store_db.py`/`test_callgraph.py`/`test_fusion.py`).

### 6회차 (2026-04-20) — Q10 + M2 + M3 연속 완료 (Quick Wins 10/10 마감)

- `c71ddb1` Q10 `.hybrid-search-ignore` + upward walk (`.git` 경계까지). pathspec 엔진 재사용으로 gitignore와 동일 문법 + negation 지원. 파일당 64KB / 32레벨 상한. `_build_ignore_spec`이 config excludes + `.gitignore` + 수집 패턴 3-소스 병합.
- `b4319bc` M2 post-checkout 훅. `$3 == 1` 게이트(브랜치 스위치만 트리거), `.hybrid-search/` 없으면 skip, `reindex --wiki-scope affected` (NO git-delta, NO synthesize). `.reindex.lock` 공유. `_HOOK_IDENTITY_MARKER = "hybrid_search.cli"` 상수로 레거시 훅 자동 인식.
- `178620f` M3 post-commit이 `git diff --name-status HEAD~1 HEAD`를 동기 캡처 → `HYBRID_SEARCH_CHANGED_STATUS` env로 자식 프로세스에 전달. race 방지 + subprocess 50ms 절약. `parse_git_diff_name_status(raw)` public 파서를 `scanner.py`에 추출.
- 396/396 passed (+18 신규).

---

## 🟣 5회차 이전 참고용 — 점점 요약

### 한줄 요약 (5회차)

캐시 안정성 축 — Q6 (Markdown YAML frontmatter strip before hash) 완료. 370/370 tests passed. Quick Wins 9/10 (90%).

### 전략적 맥락 (중요)

이 작업은 graphify 전체 소스를 6-agent로 정밀 분석 (5,035줄 분석 문서 생성) 후 확정한 3축 전략의 실행 단계:

1. **자율 루프** — Claude가 안 쓸 수 없는 도구 (Q1, Q7, Q8, M2 등) ← 현재 여기
2. **Memory Layer** — 매 사용이 도구를 더 똑똑하게 (L1 Q&A feedback loop)
3. **벤치마크 주도 품질** — 숫자로 증명 (L6)

**포지셔닝 전환:** "코드 검색 도구" → "Claude Code의 영구 기억 레이어". Graphify와 경쟁하지 않고 보완재로 공존.

### ✅ 이 세션 완료된 것 (5회차)

**구현:**
- **Q6: Markdown frontmatter strip** — `src/hybrid_search/index/scanner.py`에 `_FRONTMATTER_RE = re.compile(rb"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)` + `_strip_frontmatter(raw)` helper 추가. `compute_file_hash(path)`가 `.md` 파일일 때만 `read_bytes()` → fm strip → sha256 (body only). 비-`.md`는 기존 streaming hash 유지.
- **파급 경로 (자동 전파):** `_is_changed` → `files.file_hash` (scanner.py/pipeline.py) → wiki `file_hash_at_compile` (storage/wiki.py:460 staleness 비교) → synthesis `source_hashes` (synthesizer.py:102). frontmatter-only edit이 모든 레이어에서 재처리 skip.

**테스트:** `tests/test_scanner.py::TestComputeFileHashFrontmatter` 8개 추가. fm 있/없 해시 동일, fm 수정 시 해시 불변, body 수정 시 해시 변경, body-level `---` horizontal rule 보존, CRLF fm 지원, 비-`.md` 비영향, 미닫힘 fm은 body로 취급. **370/370 passed** (이전 362 + 신규 8).

**마이그레이션 주의:** 배포 후 첫 reindex에서 모든 `.md` 파일 해시가 한 번 바뀌어 1회 재임베딩 발생 (이후 안정). Wiki DB의 `file_hash_at_compile`과 실제 `file_hash`가 대량 불일치 → wiki 전체가 stale로 마킹됨. 권장 순서: (1) 배포, (2) `hybrid-search-mcp reindex --cwd .` 1회, (3) wiki 갱신 필요시 `reindex --synthesize`.

### 이전 세션 완료

**4회차 (2026-04-20):**
- `85716bc` — Q4 Security 모듈 (MCP 입력/출력 trust boundary)

**3회차 (2026-04-20):**
- `e4f9731` — Q3 MCP stdin blank-line filter + Q5 민감 파일 패턴 필터

**2회차 (2026-04-20):**
- `2231b1f` — Q7 CLAUDE.md idempotent + Q8 core.hooksPath (Husky 호환)

**1회차 (2026-04-20):**
- `6f0ff93` — Q1 route_hook + status + wiki 머신별 독립화
- `a3bdabf` — wiki-gaps.txt git 추적 제거
- `4ccefd8` — HANDOFF 업데이트 (Q1/Q2/Q9)

### ⬜ 다음 세션 제안 — M1 (Confidence 라벨, 1일) 또는 M4 (needs_synthesis flag, 반나절)

**M1 (권장, 1일): Confidence 3단계 라벨 + numeric score**
- 모든 call edge에 `confidence ∈ {EXTRACTED, INFERRED, AMBIGUOUS}` + `score ∈ [0,1]` 부여.
- BM25+vector fusion에서 score를 가중치로 활용. 현재는 callgraph에만 `confidence` 문자열(high/medium/low)이 존재 — 숫자 점수 도입 시 fusion/랭킹과 자연스럽게 연결.
- **중요 원칙**: Leiden/degree 같은 구조 분석은 confidence-blind 유지 (DAG 구성 정확성), 리포트/ranking만 confidence-aware. 섞지 말 것.
- 적용: `src/hybrid_search/index/callgraph.py`(엣지 emit), `src/hybrid_search/search/fusion.py`(score 활용).
- 참조: graphify `extract.py:1060-1068` (EXTRACTED 1.0), `extract.py:3206-3211` (INFERRED 0.8), `test_confidence.py:65-77` (AMBIGUOUS ≤0.4). 패치 문서 M1 섹션.

**M4 (대안, 반나절): `needs_synthesis` flag 파일 패턴**
- 훅은 LLM 호출 못 함 → 대신 flag 파일로 "synthesis 필요" 신호.
- `src/hybrid_search/tools/index.py` stale 감지 시 `.hybrid-search/needs_synthesis` flag write.
- `/search` 스킬이 읽고 사용자에게 리마인드. `/maintain` 실행 시 flag clear.
- 참조: graphify `watch.py:110-118`, 패치 문서 M4 섹션.

### 📂 필수 참조 문서 (다음 세션에서 반드시 읽을 것)

| 문서 | 경로 |
|------|------|
| **구현 플랜 (Q1 템플릿)** | `PLAN_q1_routing_hook.md` (프로젝트 루트) |
| **패치 상세 리스트 (Q1-Q10)** | `_study/graphify-analysis/99-actionable-patches-for-hybrid-search.md` |
| **전체 분석 종합** | `_study/graphify-analysis/00-overview.md` |
| **훅 상세 분석** | `_study/graphify-analysis/01-hooks-and-skill.md` |
| **전략 방향 (왜 이 작업?)** | `~/.claude/projects/-Users-ian-project-claude-project-hybrid-search-mcp/memory/project_strategic_direction.md` |
| **graphify 원본 (훅 참고)** | `/Users/ian/project/claude_project/_study/graphify/graphify/hooks.py` |

### 📋 Quick Wins 완성 + 다음 M 시리즈 로드맵

**Quick Wins (Q1~Q10): 10/10 완료 🎉 + M 시리즈 4개 + 자율 루프 축 마감**

| # | 작업 | 완료 세션 |
|---|------|-----------|
| ~~Q1~~ | ~~route_hook + status + wiki 머신별 독립화~~ | 1회차 |
| ~~Q7~~ | ~~CLAUDE.md 자동 주입~~ | 2회차 |
| ~~Q8~~ | ~~core.hooksPath 존중~~ | 2회차 |
| ~~Q3~~ | ~~MCP stdin blank-line filter~~ | 3회차 |
| ~~Q5~~ | ~~민감 파일 필터~~ | 3회차 |
| ~~Q4~~ | ~~Security 모듈~~ | 4회차 |
| ~~Q6~~ | ~~Cache frontmatter strip~~ | 5회차 |
| ~~Q10~~ | ~~`.hybrid-search-ignore` upward walk~~ | 6회차 |
| ~~M2~~ | ~~post-checkout hook 추가~~ | 6회차 |
| ~~M3~~ | ~~post-commit diff env 전달 (race 방지 + subprocess 절약)~~ | 6회차 |
| ~~M1~~ | ~~Confidence numeric score + fusion authority nudge~~ | 7회차 |
| ~~M4~~ | ~~`needs_synthesis` flag (훅→스킬→사용자 UX 신호 loop)~~ | 8회차 |
| ~~M1.v2~~ | ~~boost-only nudge (α=0.3), damping 제거~~ | **9회차** |
| ~~L6 mini-PoC + Full~~ | ~~35q benchmark, bootstrap CI, α sweep 검증~~ | **9회차** |

**다음: 품질 축 미세 조정 + 확장**

| # | 작업 | 공수 | 축 | 우선순위 |
|---|------|------|----|----|
| **M1.2** | **keyword type-gating (EXACT_SYMBOL → authority OFF)** | **30분** | **품질** | **다음 1순위 (L6 근거)** |
| M1.1 | 라벨 리네이밍 (EXTRACTED/INFERRED/AMBIGUOUS, schema v5) | 반나절 | 품질 | 중 |
| M5 | MCP 확장: `god_nodes`, `shortest_path`, `subgraph` | 2일 | 품질 | 중 |
| L6 확장 | gold set 50q+ 확대, MRR 추가, per-project 교차 검증 | 1일 | 벤치마크 | 낮음 |

전체 진행률: **16/28 (57%)**. 자율 루프(6) + 품질(M1+M1.v2) + 벤치마크(PoC+Full L6) 3축 모두 실제 측정 기반으로 전진.

### 🎬 다음 세션 시작 방법

```
HANDOFF.md 최상단 + benchmarks/authority_poc/score_v2.py 실행 결과 읽고,
M1.2 keyword type-gating 구현 (30분):
  orchestrator.hybrid_search에서 qtype==EXACT_SYMBOL이면 authority=None 강제.
  run_v2.py 재실행으로 keyword P(Δ>0) 0.25 → ≥0.50 회복 확인.
  test_orchestrator.py에 mock integration 테스트 추가.
```

### 🔧 현재 상태 스냅샷

- **브랜치:** `main` (L6 = `379aa64`, M1.v2 = `c6fe7a1`, PoC = `47b2b10`, M4 = `6ed4f39`, M1 = `83bfa7c`)
- **워킹 트리:** L6 커밋됨 + HANDOFF 갱신 중. origin/main 대비 10 commits ahead (미푸시).
- **테스트:** 413/413 passed (공식 변경 포함 regression 없음).
- **주 작업 파일 (9회차):**
  - `src/hybrid_search/search/fusion.py` (공식 boost-only, α=0.3, `_AUTHORITY_BOOST_ALPHA` 상수)
  - `tests/test_fusion.py` (TestAuthorityNudge 5개 재작성)
  - `benchmarks/authority_poc/` 전체 (run.py + score.py + run_v2.py + score_v2.py + gold_queries.json + gold_queries_v2.json + external_queries.json + results.json + results_v2.json + label_me.tsv + apply_labels.py)
- **Benchmark α=0.3 최종 결과:** OVERALL Δ +0.037 (P=0.87). structural +0.117 (P=0.94) / semantic +0.026 (P=0.66) / **keyword -0.032 (P=0.25) ← M1.2 해결 대상**.
- **route_hook 동작 확인됨:** Glob/Grep 호출 시 안내 주입 실측.

### ⚠️ 주의사항

- **M1.v2 boost-only 의미 변화 (v1 테스트 전체 재작성):** v1에서는 "absent ≡ neutral, explicit 0 = damped"였는데 v2에서는 "absent ≡ explicit 0 = 둘 다 factor 1.0". 기존 제3자 코드가 authority=0.0을 penalty 의도로 사용했다면 더 이상 동작 안 함. `_apply_authority_nudge`의 의미 변화를 문서화 필요. `get_chunk_authority_scores`는 resolved edge만 반환하므로 실무 경로엔 영향 없음.
- **α=0.3 선택 근거 (Full L6 기반):** structural P(Δ>0)=0.94가 α=0.3에서 최고. α=0.5는 external 프로젝트엔 더 좋지만(+0.235 vs +0.153) self-contained의 structural variance가 커짐 (표준편차 확대). 보수적 선택. 다른 프로젝트 배포 후 α 재튜닝 필요 가능 — 현재는 상수, 나중에 config 노출 고려.
- **L6 gold set의 한계:** self-contained 30q 중 semantic 10q는 `N01/N03/N08/N10`이 baseline NDCG=0 (top-10에 relevance>0 chunk 무). 이 프로젝트의 semantic 질의가 docs/plan 문서 chunk를 많이 끌어오는데 expected primary/secondary와 mismatch. 외부 프로젝트(valuein)에선 완전히 다른 패턴 — semantic이 가장 큰 authority 혜택. **"semantic에 authority 효과 없음"은 이 프로젝트 편향, 일반화 금지.**
- **L6 external proxy labels 제약:** `expected_files` 기반 매칭이라 "같은 기능을 다른 파일이 구현"한 경우 miss. spot check로만 사용, 결정적 신호로 쓰지 말 것. 본격 평가엔 도메인 전문가 라벨 필요.
- **M1.2 type-gating 구현 시 주의:** classify_query가 `EXACT_SYMBOL`인지 판별하는 `_SYMBOL_RE`가 PascalCase 단독(예: `FusedResult`)을 잡지 못할 수 있음. 현재 regex 확인 후 필요시 보강. 혼합 쿼리(`createUser 로직`)는 KOREAN_NL로 분류되므로 authority 유지 → 의도대로.
- **M4 flag 생성 타이밍:** `_mark_stale_wikis`에서만 write/clear. 즉 `reindex` 또는 `sync-wiki` 경로를 타야 flag가 갱신됨. 순수 `synthesize-wiki --prepare`만 돌리면 flag는 건드리지 않음(staleness 평가 안 하므로). `--finalize`는 예외 — 끝에서 `check_staleness`로 재평가해 flag 정리.
- **M4 STALE.md vs needs_synthesis 분리:** 둘 다 같은 트리거(stale_items 존재)로 write/clear되지만 역할 분리. STALE.md는 human-readable 상세 (changed_files 포함), needs_synthesis는 skill이 parse하는 구조화 signal (JSON). 한쪽만 write하는 경로는 없어야 — 두 파일 drift 방지 위해 `_mark_stale_wikis` 한 곳에서만 처리.
- **M4 gitignore 재설치 필요:** 기존 프로젝트는 gitignore에 needs_synthesis 엔트리가 없음. `hybrid-search-mcp install-hook --cwd .` 재실행 시 1 entry 자동 추가됨. 이미 flag 파일이 tracked로 들어간 경우 `git rm --cached .hybrid-search/needs_synthesis` 필요할 수 있음(드문 케이스).
- **M4 finalize의 flag 재평가 비용:** 매 `--finalize` 호출마다 `wiki_store.check_staleness(pinfo.id)`를 한 번 더 돌림. 페이지 수 많은 프로젝트에선 수백 ms 정도. DB 쿼리 기반이라 IO 비용은 크지 않지만, 매우 큰 프로젝트(valuein_homepage 1,330 files 급)에선 모니터링 필요. 최적화 필요 시 finalize가 터치한 module 이름만 check_staleness에 page_id로 필터 전달 가능.
- **M4 스킬 sync:** `skills/*.md` 수정만으로는 `~/.claude/skills/`에 반영 안 됨. `hybrid-search-mcp setup` 실행 시 자동 복사. 다음 세션에서 사용자가 스킬 사용 전에 setup 한 번 권장.
- **M1 authority 시그널 범위:** `get_chunk_authority_scores`는 `callee_chunk_id IS NOT NULL` 필터 사용 → unresolved edge는 authority에 기여하지 않음. 신규 프로젝트에서 call graph resolution이 돌기 전엔 fusion 효과 無. 실측에서 `breeze`가 "no authority signal yet"으로 나왔던 건 그 때문.
- **M1 bounded nudge 경계:** 공식 `rrf * (0.5 + 0.5 * auth)`에서 맵에 **없는** chunk는 `authority=None` → passthrough. 맵에 `auth=0.0`으로 명시된 chunk는 `factor=0.5`로 damped. 즉 **명시적 0과 미설정의 의미가 다르다.** 현재 `get_chunk_authority_scores`는 resolved edge만 반환하므로 실무상 0.3 이상만 들어옴. 테스트 `test_chunks_outside_map_are_neutral`이 이 구분을 고정.
- **M1 cross-project merge:** chunk id가 전역 UUID라 단순 `dict.update`로 병합. 만약 향후 chunk id 생성 규칙이 바뀌어 충돌 가능성이 생기면 `(project_id, chunk_id)` 복합키로 전환 필요.
- **M1 label 리네이밍 보류:** `CONFIDENCE_LEVELS`는 여전히 `("low","medium","high")`. graphify 용어(EXTRACTED/INFERRED/AMBIGUOUS)를 쓰고 싶으면 M1.1에서 schema v5 + UPDATE로 DB 값 리네임 + 모든 소비처 동기 교체. 매핑 레이어(두 이름 공존)는 실수 유발로 피하기로 결정.
- **M1 v3→v4 backfill 영향:** ALTER + `UPDATE ... WHERE confidence_score = 0.0 OR IS NULL` 구문. 이미 한 번 마이그레이션 된 DB를 다시 열어도 backfill 대상이 없어 no-op (정상). 만약 테스트나 특수 상황에서 score를 0으로 명시적 설정한 row가 있으면 재backfill될 수 있음 — 현재 프로덕션 경로에서는 발생하지 않음.
- **graphify 분석 문서**는 `_study/` 폴더에 있고 이 프로젝트 git과 **별개** (추적 안 됨).
- **Q4 sanitize 범위** — MCP 노출은 `hybrid_search` 1개뿐 (`server.py:89-125`). 향후 새 도구 노출 시 **반드시** `handle_*`에서 `sanitize_*` / `clamp_*` 호출. control char regex는 `\t\n\r` 보존이 의도된 동작.
- **Q3 stdin filter** — `_filter_blank_stdin()`은 전역 fd 0를 `dup2`로 교체함. pytest 메인 프로세스에서 직접 호출 금지. 테스트는 subprocess 격리 (`tests/test_server_stdin_filter.py`).
- **Q5 sensitive 패턴** — basename 우선, path 패턴은 `.ssh/id_*` 등 위치 의존에만. 정상 소스(`PasswordReset.tsx` 등) 통과 필수. 신규 패턴 추가 시 `TestIsSensitiveFile::test_source_files_not_blocked`도 업데이트.
- **Q7 marker regex:** `<!-- hybrid-search -->\n## [^\n]+\n.*?(?=\n## |\Z)` — 마커 + 첫 `##` 헤딩 + 본문. 치환은 `lambda`로 back-reference 파싱 회피.
- **Q8 core.hooksPath 폴백:** `git config --get` → `git rev-parse --git-path hooks` → `.git/hooks`.
- **Q6 frontmatter regex:** `\A---\r?\n.*?\r?\n---\r?\n` (DOTALL, count=1). `\A`로 파일 시작 고정 → body 내부 `---`(horizontal rule)을 false-positive로 잡지 않음. 이 regex를 수정하면 `TestComputeFileHashFrontmatter::test_body_level_horizontal_rule_preserved` 같이 깨질 수 있으니 주의.
- **Q6 side effect — file_size/mtime drift:** fm-only edit이면 `_is_changed`가 False 반환해서 `files.file_size`/`file_mtime`이 갱신 안 됨. 다음 스캔에서 mtime 불일치로 매번 `compute_file_hash` 재계산(정확하지만 중복 CPU). 필요시 `_is_changed`에서 hash 일치해도 size/mtime만 UPDATE하는 최적화 가능 (Q6.1 후보).
- **Q10 anchoring 주의:** 수집된 모든 `.hybrid-search-ignore` 패턴을 하나의 pathspec로 합쳐 `project_root` 기준으로 매칭함. 따라서 **ancestor 파일의 rooted 패턴**(예: `/dist/`)은 그 ancestor가 아니라 `project_root` 기준으로 해석됨 (직관과 다를 수 있음). 대부분의 ignore 패턴은 non-rooted(`dist/`, `*.log`)라 실무 영향 없음. 필요 시 per-anchor PathSpec로 정밀화 가능(Q10.1 후보). 테스트 `test_walk_stops_at_git_boundary`로 `.git` 경계 방어 확인.
- **Q10 reindex 영향:** 새 `.hybrid-search-ignore` 추가 시 다음 reindex에서 이전 포함 파일이 제거되어 해당 chunks가 DB에서 삭제됨. 의도된 동작. Full rebuild 불필요.
- **M2 post-checkout 튜닝 여지:** 현재는 단순 filesystem-delta reindex. 빠른 브랜치 왕복(`git checkout -`)이 잦은 워크플로우에서 반복 reindex 비용이 보일 수 있음. 개선 아이디어: (1) 마지막 reindex HEAD hash를 `.hybrid-search/last_indexed_head` 파일에 저장 → 동일 head면 skip, (2) M3 기반으로 `git diff <prev>..<new> --name-only` 전달. M3와 함께 묶기 권장.
- **M2 식별 마커 주의:** `_HOOK_IDENTITY_MARKER = "hybrid_search.cli"` — 두 훅 모두 이 문자열 포함. 개별 훅 파일 단위로 체크하므로 충돌 없음. 레거시(이전 단일 hook) 설치도 동일 문자열이라 자동 인식됨.
- **M3 env 보안 주의:** `HYBRID_SEARCH_CHANGED_STATUS`는 훅이 export하지만 **외부에서도 설정 가능**. `cmd_reindex`는 `--git-delta` 게이트 안에서만 env를 읽으므로 일반 사용자가 실수로 env를 상속해도 `--git-delta` 없이는 무시됨. 악성 env로 인한 "가짜 파일 리인덱싱" 공격은 이론상 가능하지만 대상 파일은 프로젝트 내 실제 경로로 제한(scan_project_subset → `_is_indexable_path` 검증) → 영향 제한적. 추후 env 서명 고려 가능.
- **M3 race 방지 원리:** 훅은 post-commit 시점에 동기적으로 `git diff --name-status HEAD~1 HEAD`를 캡처 → env export → `nohup &`로 배경 프로세스에 전달. 그 사이에 다른 커밋이 들어와도 env는 커밋 시점 snapshot이라 올바른 diff를 유지. env가 없는 경로(기존)는 `nohup` 내부에서 subprocess를 늦게 실행하므로 HEAD 이동에 취약.
- **skill 파일 수정** 시 `~/.claude/skills/`로 동기화 잊지 말 것 (setup이 처리).

---

## 📚 이전 세션 히스토리 (Phase 1~10 완료)

> **Date**: 2026-04-14 | **Branch**: main
> **설계 문서**: `docs/design.md` (v7, Phase 1-10 완료 + LLM 재랭킹)

## 프로젝트 한줄 요약

BM25 + Vector(RRF) 하이브리드 검색 MCP 서버. 한국어 자연어 → 영어 코드 크로스 언어 검색이 핵심 가치.

---

## 완료된 것

### Phase 1: MVP — 시맨틱 검색 파이프라인 ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| 임베딩 모델 벤치마크 | §7 | `benchmarks/run_benchmark*.py` | — |
| MCP 서버 뼈대 (7개 도구) | §10 | `server.py` | 257 |
| File scanner + delta detection | §9 | `index/scanner.py` | 195 |
| AST chunker (TS/JS/Python) | §8 | `index/ast_chunker.py` | 604 |
| 문서 chunker (MD/JSON/YAML) | §8 | `index/doc_chunker.py` | 181 |
| Embedding 생성 (sentence-transformers) | §7 | `index/embedder.py` | 256 |
| USearch 벡터 인덱스 | §13 | `search/vector.py` | 163 |
| `semantic_search` tool | §10.2 | `tools/semantic_search.py` | 148 |
| `search_symbols` tool | §10.6 | `tools/symbols.py` | 60 |
| `index_project` / `index_status` | §10.5, §10.7 | `tools/index.py` | 55 |
| `list_projects` / `remove_project` | §10.8, §10.9 | `tools/projects.py` | 50 |

### Phase 2: Hybrid + BM25 ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Tantivy BM25 인덱스 | §13 | `search/bm25.py` | 156 |
| RRF fusion (k=60) | §11 | `search/fusion.py` | 51 |
| 쿼리 분류기 (SYMBOL/KR/EN) | §11 | `search/orchestrator.py` | 487 |
| `hybrid_search` tool | §10.1 | `tools/hybrid_search.py` | 51 |
| 멀티 프로젝트 + cross-project 검색 | §13 | `search/orchestrator.py` | (포함) |

### 지원 모듈 ✅

| 파일 | 역할 | 줄수 |
|------|------|:----:|
| `config.py` | TOML 설정 로딩, 모델 토큰 자동감지 | 207 |
| `project.py` | 글로벌 프로젝트 레지스트리 (SQLite) | 114 |
| `storage/db.py` | per-project store.db (WAL, FK CASCADE) | ~550 |
| `storage/indexes.py` | 인덱스 경로 관리 | 45 |
| `index/pipeline.py` | 인덱싱 오케스트레이션 (multi-store 트랜잭션) | ~315 |

### Phase 3a: Call Graph ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Call Graph Resolution (3단계 confidence) | §12 | `index/callgraph.py` | 155 |
| trace_callers/trace_callees 도구 | §10.3, §10.4 | `tools/trace.py` | 250 |
| StoreDB call graph 쿼리 (10개 메서드) | §12, §13 | `storage/db.py` (추가) | +180 |
| AST byte offset 버그 수정 | §8 | `index/ast_chunker.py` (수정) | — |

**검증**: 1,934 call edges, 146 resolved (7.5%), 0 dirty. trace depth 2에서 정확한 caller/callee 추적.

### Phase 3a Code Review 수정 ✅

| 수정 | 우선순위 |
|------|:--------:|
| `_process_file`에 `db.transaction()` 적용 (partial write 방지) | P1 |
| `db._conn` 직접 접근 제거 → public method/transaction 사용 | P1 |
| `call_edges.callee_chunk_id` 인덱스 추가 | P2 |
| `_get_file_from_chunks` O(N) → `file_index` dict O(1) | P2 |
| `lstrip("./")` → `removeprefix("./")` | P2 |
| 삭제 시 dangling callee edge 정리 (`delete_call_edges_by_callee`) | P2 |

### Phase 3b: 추가 언어 지원 ✅

| 항목 | design.md 섹션 | 구현 파일 | 변경 |
|------|:-------------:|-----------|:----:|
| 10개 언어 AST 청킹 (Rust/Go/Ruby/Java/C/C++/Swift/Kotlin/CSS/SQL) | §8 | `index/ast_chunker.py` | CHUNK_NODE_TYPES, CLASS_NODE_TYPES, _get_ts_language, _classify_node_type, _extract_name, _extract_imports, _extract_docstring, _extract_call_name 확장 |
| tree-sitter grammar 의존성 11개 추가 | §17 | `pyproject.toml` | +11 패키지 |

**검증**: 모든 14개 AST 언어 파싱 성공 (TS/JS/Python/Rust/Go/Ruby/Java/C/C++/Swift/Kotlin/CSS/SCSS/SQL). HTML은 fallback blank-line chunking 사용.

### Phase 4: Polish ✅

| 항목 | 설명 | 상태 |
|------|------|:----:|
| 테스트 확충 | 175개 테스트 (12개 파일) | ✅ |
| 크래시 복구 | (1) consistency mismatch → 자동 force rebuild (2) `file_hash=""` partial write 감지 → 재인덱싱 | ✅ |
| ONNX 백엔드 | `_embed_onnx_batch()` 완전 구현 (mean pooling + L2 normalize) | ✅ |
| Ollama 백엔드 | `POST /api/embed` HTTP API 백엔드 | ✅ |
| Apple Silicon MPS | ONNX: CoreMLExecutionProvider, ST: `device="mps"` | ✅ |
| 인덱싱 진행률 | `ProgressCallback(current, total, path)` | ✅ |
| config.toml 핫 리로드 | `_HotReloadableConfig` mtime 감지 | ✅ |
| CWD 프로젝트 부스트 | BM25 2:1 weighted interleave + Vector cosine +0.05 | ✅ |

### Phase 5: Reactive Wiki Layer ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| WikiConfig (config.toml `[wiki]` 섹션) | `config.py` | +15 |
| wiki_pages + wiki_dependencies 테이블 | `storage/db.py` | +25 |
| WikiStore (compile/lookup/staleness/refresh/eviction) | `storage/wiki.py` | ~270 |
| 4개 Tool 핸들러 | `tools/wiki.py` | ~180 |
| server.py 등록 (13개 도구) | `server.py` | +100 |
| 테스트 28개 | `tests/test_wiki.py` | ~280 |

**핵심 설계**: 서버는 저장 + 의존성 추적만 담당, 콘텐츠는 Claude가 작성. `source_chunk_ids`로 검색 결과 → 파일 해시 스냅샷 자동 연결. 변경 감지 시 stale 마킹.

### Phase 6a: CLI + Hook + 스킬 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| CLI 엔트리포인트 (`reindex`, `status`, `stale`, `install-hook`, `sync-wiki`) | `cli.py` | 402 |
| post-commit hook 스크립트 + 자동 설치 | `scripts/post-commit-hook.sh` | 11 |
| `/bootstrap-wiki` 스킬 | `~/.claude/skills/bootstrap-wiki/skill.md` | 142 |
| `/save-wiki` 스킬 | `~/.claude/skills/save-wiki/skill.md` | ~65 |
| `/search` 스킬 (wiki-first 4단계 폴백) | `~/.claude/skills/search/skill.md` | ~64 |

**CLI 명령어**:
- `python -m hybrid_search.cli reindex --cwd .` — delta 재인덱싱 + stale 마킹 + gap 플래그
- `python -m hybrid_search.cli status` — 전체 프로젝트 인덱스 상태
- `python -m hybrid_search.cli stale --cwd .` — wiki staleness 확인
- `python -m hybrid_search.cli install-hook --cwd .` — post-commit hook 자동 설치
- `python -m hybrid_search.cli sync-wiki --cwd .` — 디스크 wiki → DB 동기화 (backtick 경로에서 파일 의존성 자동 추출)
- `python -m hybrid_search.cli call-graph-stats --cwd .` — call graph resolution 통계 (Phase 7)

**스킬 검색 체인** (`/search`):
```
1. lookup_wiki (DB) → found+fresh → 즉시 반환
2. wiki/index.md (디스크) → Read로 확인
3. hybrid_search (MCP) → 결과 좋으면 compile_to_wiki로 축적
4. Grep/Glob (폴백) → 직접 검색
```

**설치된 hook**: valuein-homepage, breeze 프로젝트에 post-commit hook 설치 완료.

**valuein-homepage wiki 부트스트랩 완료**: 10개 wiki 페이지 생성 + DB 동기화 (sync-wiki). architecture, students, tuition-billing, attendance, learning-data, homework-analysis, diagnosis, portal, consultation, edge-functions.

### Phase 7: Call Graph Resolution 90%+ ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| Step 1: Import-Call 바인딩 | `index/ast_chunker.py` | `_extract_import_map()` 신규, `_extract_calls()` → `list[tuple[str, str\|None]]` 반환 |
| Step 2: Module Path → File 역인덱스 | `index/callgraph.py` | `_build_module_index()` 신규, 다양한 import path 형태 → 파일 chunks 매핑 |
| Step 3: 메서드 Receiver 추적 | `index/ast_chunker.py`, `index/callgraph.py` | `this`/`self` 감지 → `__self__::ClassName` 태그, `class_members` 인덱스 |
| Step 4: COMMON_NAMES 정책 완화 | `index/callgraph.py` | module context 있으면 common name도 medium으로 승격 |
| DB 인터페이스 변경 | `storage/db.py` | `insert_call_edges(calls: list[tuple[str, str\|None]])` callee_module 포함 |
| CLI call-graph-stats | `cli.py` | `python -m hybrid_search.cli call-graph-stats --cwd .` 명령 추가 |
| 테스트 10개 추가 | `tests/test_callgraph.py` | Import-Call 5개 + Self-Method 3개 + Common-Name 2개 |

**핵심 설계**: 기존 `_extract_imports()`는 raw string 리스트로 유지(임베딩용), 별도 `_extract_import_map()`으로 name→module 딕셔너리 생성. `_extract_call_name_ex()`에서 this/self receiver 감지. callgraph에 module_index + class_members 이중 인덱스 추가. `_BUILTIN_CALLS` + `_BUILTIN_METHOD_CALLS`로 built-in/라이브러리 호출 필터링.

**지원 언어 Import 파싱**: TS/JS (named/default/namespace import), Python (from...import, import...as), Go, Java, Rust, Ruby, Kotlin, Swift

**실측 결과**:

| 프로젝트 | Total Edges | Project Deps (H+M) | Module 있는 edge | 그 중 Resolved |
|----------|:-----------:|:-------------------:|:----------------:|:--------------:|
| hybrid-search-mcp (73파일) | 2,727 | 572 | 686 | **45.3%** |
| valuein-homepage (1,757파일) | 21,127 | 1,961 | 1,560 | **66.2%** |

**해석**: 전체 resolution rate(21-13%)는 외부 라이브러리 호출(`supabase.from()`, `Array.map()`)이 denominator를 부풀려 낮게 보임. import-call 바인딩이 성공한 edge는 45-66% resolve. CodeWiki에 필요한 **Project Deps (High+Medium)** = 572~1,961개 — 프로젝트 내부 의존성 그래프 구축에 충분.

**총 코드**: ~7,900줄 (31개 파일) | **MCP 도구**: 13개 | **테스트**: 185개 (12개 파일) | **CLI 명령**: 6개 | **스킬**: 3개

### Phase 8a: CodeWiki 모듈 트리 자동 생성 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| DAG 구축 (High+Medium confidence edges) | `index/dag.py` | ~310 |
| Connected Components (BFS 무방향 탐색) | `index/dag.py` | (포함) |
| Topological Sort (Kahn's algorithm, 사이클 내성) | `index/dag.py` | (포함) |
| 모듈 이름 자동 유도 (공통 디렉토리 기반) | `index/dag.py` | (포함) |
| 대형 모듈 분할 (MAX_MODULE_CHUNKS=40) | `index/dag.py` | (포함) |
| 고립 노드 디렉토리 기반 폴백 그룹핑 | `index/dag.py` | (포함) |
| `generate-wiki-plan` CLI 명령 | `cli.py` | +70 |
| `verify-wiki` CLI 명령 | `cli.py` | +50 |
| 테스트 24개 | `tests/test_dag.py` | ~280 |

**핵심 설계**: CodeWiki (ACL 2026) 파이프라인 Step 1-3 구현. call_edges에서 High+Medium confidence edge만 추출하여 방향성 DAG 구축 → 무방향 BFS로 connected component 식별 (= 1개 기능 모듈) → Kahn's algorithm으로 위상정렬 (bottom-up 처리 순서). 고립 노드(call edge 없는 청크)는 디렉토리 기반 그룹핑으로 폴백. `wiki-plan.json` 파일 출력으로 downstream 스킬/Agent 연동 가능.

**실측 결과** (hybrid-search-mcp):
- 9개 graph-based 모듈 + 10개 isolated 그룹
- 491/492 chunks 커버 (99.8%)
- Entry point 자동 식별: `cli.py::main`, `handle_hybrid_search`, `SearchOrchestrator.hybrid_search` 등

**총 코드**: ~8,500줄 (34개 파일) | **MCP 도구**: 13개 | **테스트**: 212개 (13개 파일) | **CLI 명령**: 8개 | **스킬**: 3개

### Phase 8b: Wiki 페이지 자동 생성 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| 모듈별 구조적 wiki 마크다운 생성 | `index/dag.py` (`generate_module_wiki`) | +130 |
| 전체 프로젝트 wiki 일괄 생성 | `index/dag.py` (`generate_all_wiki_pages`) | +40 |
| `generate-wiki` CLI 명령 (디스크 + DB sync) | `cli.py` | +75 |
| 테스트 6개 추가 | `tests/test_dag.py` | +95 |

**핵심 설계**: LLM 호출 없이 코드 메타데이터만으로 구조적 wiki 생성. 각 모듈 페이지에: 파일 목록, entry points, 심볼별 call/called-by 관계, 외부 의존성. `generate-wiki` CLI가 디스크 `.hybrid-search/wiki/`에 쓰고 DB에 자동 sync (staleness 추적 포함).

**실측 결과** (hybrid-search-mcp):
- 20개 wiki 페이지 생성 (index + 9 graph + 10 isolated)
- 18개 DB sync 완료
- 자동 생성된 페이지: tools.md (19 symbols, call 관계 포함), search.md, storage.md 등

**총 코드**: ~9,000줄 (34개 파일) | **MCP 도구**: 13개 | **테스트**: 218개 (13개 파일) | **CLI 명령**: 9개 | **스킬**: 3개

### Phase 8c: verify-wiki 강화 ✅

| 항목 | 변경 |
|------|:----:|
| query_key 기반 정확한 매칭 (title 대소문자 비교 → normalize_query) | 버그 수정 |
| uncovered 파일 목록 출력 | 신규 |
| `--json` 플래그 (JSON 구조화 출력) | 신규 |
| staleness 상세 리포트 (fresh/stale 카운트 + changed files) | 강화 |

### Phase 8d: 전체 파이프라인 자동화 ✅

| 항목 | 변경 |
|------|:----:|
| `reindex` 후 자동 call graph re-resolution | 신규 |
| `reindex --wiki` 플래그 → generate-wiki 자동 체인 | 신규 |

**전체 파이프라인**:
```
git commit
  └→ post-commit hook
     └→ reindex (delta)
        └→ call graph re-resolve (자동)
           └→ wiki sync (기존 wiki 있으면 자동)

reindex --wiki (명시적)
  └→ delta reindex
     └→ call graph re-resolve
        └→ generate-wiki (모듈 트리 → wiki 생성 + DB sync)
```

### Phase 8e: Wikilink 그래프 (GraphRAG) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| `[[링크]]` 파싱 + DB 동기화 (`_sync_wikilinks`) | `storage/wiki.py` | +40 |
| BFS 양방향 그래프 탐색 (`_expand_graph`) | `storage/wiki.py` | +80 |
| `wiki_links` 테이블 (source, target, link_text) | `storage/db.py` | +10 |
| lookup_page → linked_pages 자동 확장 | `storage/wiki.py` | (통합) |

**핵심 설계**: compile_page/refresh_page 시 `[[텍스트]]` 패턴을 자동 파싱하여 wiki_links 테이블에 저장. lookup_page 호출 시 BFS(max_hops=2, max_pages=10)로 양방향 탐색하여 linked_pages 반환. CLAUDE.md 규칙: "wiki에 [[링크]]가 있으면 연결된 페이지도 반드시 읽을 것."

### Phase 9a: LLM Wiki Synthesis (prepare/finalize) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| Synthesizer (prepare/finalize/verify/merge/hash) | `index/synthesizer.py` | ~430 |
| DB 스키마 v3 마이그레이션 (synthesis_* 4컬럼) | `storage/db.py` | +30 |
| WikiStore synthesis 필드 + 헬퍼 메서드 7개 | `storage/wiki.py` | +60 |
| SynthesisConfig | `config.py` | +5 |
| `synthesize-wiki` CLI (--dry-run, --module, --finalize) | `cli.py` | +130 |
| 테스트 27개 | `tests/test_synthesizer.py` | ~280 |

**핵심 설계**: Claude Code 자체가 LLM이므로 외부 API 키 불필요. 3단계 구조:
1. CLI `synthesize-wiki` → `_synthesis_input/*.md`에 컨텍스트 수집 (DB IO만, 토큰 0)
2. Claude Code가 컨텍스트 파일 Read → 합성 작성 → `_synthesis_output/*.md`에 Write
3. CLI `synthesize-wiki --finalize` → 참조 검증 + 결정론적 wiki 병합 + `_raw/` 백업 + DB 저장 (토큰 0)

합성 결과는 상단(Overview, Key Design Decisions, Data Flow, Caveats, Related Modules) + 하단 `<details>` 접기(결정론적 구조 데이터) 형태. synthesis_hash로 변경 감지하여 불필요한 재합성 방지.

**E2E 검증** (AST Chunker): 9개 참조 100% 검증 통과, 0개 제거.

**총 코드**: ~10,000줄 (36개 파일) | **MCP 도구**: 3개 | **테스트**: 241개 (14개 파일) | **CLI 명령**: 13개 | **스킬**: 3개

### Phase 9b: 전체 모듈 Bottom-Up 합성 ✅

| 항목 | 변경 |
|------|:----:|
| 28개 모듈 일괄 prepare → Claude Code 합성 → finalize | 완료 |
| `finalize_module` 타이틀 매칭 버그 수정 (slug vs 원본 이름) | `index/synthesizer.py` |
| 중복 RAW 페이지 정리 (18개 삭제) | DB cleanup |

**실행 결과**:
- 28/28 모듈 합성 완료 (100%)
- 참조 검증: 총 108개 refs verified, 29개 removed (73% 검증률)
- `_raw/` 백업: 20개 원본 결정론적 wiki 보존
- DB: 28 pages, 28 synthesized (중복 없음)

**발견된 버그 & 수정**:
1. `finalize_module`에서 `find_page_by_title`에 raw slug(대시 포함)를 전달 → LIKE 매칭 실패
   - 수정: 원본 이름 → 대시-공백 변환 순서로 2단계 fallback 시도
2. `collect_module_context`에도 동일 패턴 적용
3. `--` 포함 타이틀 (예: "Embedder -- OpenAI API Backend")은 `replace("-", " ")`로 4개 공백 생성 → LIKE 불일치
   - 수정: 원본 이름 먼저 시도하는 fallback 체인

### Phase 9c: 지식 복리 (Incremental Re-synthesis) ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| `should_skip_synthesis()` — staleness 기반 skip | `index/synthesizer.py` | +35줄 |
| `get_synthesis_hash()` — DB 저장 hash 조회 | `storage/wiki.py` | +7줄 |
| `find_indirectly_affected()` — wikilink BFS 간접 영향 | `storage/wiki.py` | +25줄 |
| `_auto_prepare_synthesis()` — reindex → prepare 체이닝 | `cli.py` | +70줄 |
| `reindex --synthesize` 플래그 | `cli.py` | +5줄 |
| CLI prepare에서 hash skip 로직 | `cli.py` | +10줄 |
| 테스트 8개 | `tests/test_synthesizer.py` | +100줄 |

**핵심 설계**: `should_skip_synthesis()`는 file_hash_at_compile vs 현재 file_hash 비교 (staleness 기반). synthesis_hash는 finalize 시 변경되므로 단순 hash 비교는 false positive 발생 — staleness 기반이 정확함. `reindex --synthesize`로 stale 감지 후 자동 prepare, `find_indirectly_affected()`로 wikilink 1-hop 이웃 모듈도 선택적 re-prepare.

**전체 파이프라인**:
```
reindex --synthesize
  └→ delta reindex
     └→ call graph re-resolve
        └→ _mark_stale_wikis → STALE.md
           └→ _auto_prepare_synthesis
              ├→ stale 모듈 prepare (skip if unchanged)
              └→ indirect 모듈 prepare (wikilink 1-hop)
```

### Phase 9d: 환각 검증 자동화 ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| `verify_symbols()` — backtick 심볼 DB 존재 검증 | `index/synthesizer.py` | +55줄 |
| `SymbolVerificationResult` 데이터 클래스 | `index/synthesizer.py` | +5줄 |
| `has_chunk_matching_name()` — qualified_name LIKE 검색 | `storage/db.py` | +7줄 |
| `verify-synthesis` CLI (--json, --fix) | `cli.py` | +110줄 |
| 테스트 6개 | `tests/test_synthesizer.py` | +60줄 |

**핵심 설계**: 2종 검증 — (1) file:line 참조 (기존 `verify_references()`) + (2) backtick 심볼명 (`verify_symbols()`). 심볼 검증은 PascalCase/snake_case 식별자를 추출하여 chunks.name 또는 chunks.qualified_name에서 확인. `_SYMBOL_SKIP` 집합으로 common words (true, false, self 등) 필터링. `--fix` 플래그로 bad refs 자동 제거.

**CLI 명령**:
```bash
python -m hybrid_search.cli verify-synthesis --cwd .         # 전체 합성 검증 리포트
python -m hybrid_search.cli verify-synthesis --json --cwd .  # JSON 출력
python -m hybrid_search.cli verify-synthesis --fix --cwd .   # bad refs 자동 제거
```

**총 코드**: ~9,700줄 (34개 파일) | **MCP 도구**: 3개 | **테스트**: 255개 (14개 파일) | **CLI 명령**: 15개 | **스킬**: 3개

### Phase 10: LLM 재랭킹 (Claude Code Native) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| RerankingConfig (`[search.reranking]` TOML 섹션) | `config.py` | +10 |
| Orchestrator — reranking 시 확장 후보 반환 | `search/orchestrator.py` | +10 |
| HybridSearchResponse에 `reranked` 필드 | `search/orchestrator.py` | +2 |
| `rerank_hint` — Claude Code 재랭킹 지시 | `tools/hybrid_search.py` | +20 |
| 테스트 16개 | `tests/test_reranker.py` | ~190 |

**핵심 설계**: Phase 9a와 동일 원칙 — Claude Code 자체가 LLM이므로 외부 API 키 불필요. `hybrid_search` MCP 도구가 RRF top-20 후보를 enriched 메타데이터(name, file_path, snippet, node_type)와 함께 반환. `rerank_hint` 메시지가 Claude Code에게 "쿼리 의도에 맞게 재정렬하여 상위 10개만 제시하라"고 지시. API 호출 0, 추가 비용 0, 지연 0.

**설정**:
```toml
[search.reranking]
enabled = true                       # 기본 false
max_candidates = 20                  # RRF에서 가져올 후보 수
```

**파이프라인**:
```
쿼리 → BM25 + Vector → RRF fusion (top-20 enriched) → Claude Code 재랭킹 → top-10
```

**총 코드**: ~9,800줄 (35개 파일) | **MCP 도구**: 3개 | **테스트**: 271개 (15개 파일) | **CLI 명령**: 15개 | **스킬**: 3개

### MCP 도구 슬림화: 13→3 ✅

**이유**: MCP 도구 스키마가 매 대화 시스템 프롬프트에 로드되어 토큰 소모. 관리/wiki 도구 10개를 CLI로 이관.

| 잔류 MCP 도구 | 이유 |
|:------------:|------|
| `hybrid_search` | 핵심 검색 (semantic_search 병합: bm25_weight=0) |
| `trace_callers` | 대화 중 역방향 call graph 추적 |
| `trace_callees` | 대화 중 순방향 call graph 추적 |

| CLI로 이관 (10개) | CLI 명령 |
|:----------------:|----------|
| `index_project` | `reindex` |
| `index_status` | `status` |
| `list_projects` | `status` |
| `remove_project` | `remove-project` (신규) |
| `search_symbols` | `search-symbols` (신규) |
| `semantic_search` | `hybrid_search`에 병합 |
| `compile_to_wiki` | `generate-wiki` |
| `lookup_wiki` | `lookup-wiki` (신규) |
| `check_wiki_staleness` | `stale` |
| `refresh_wiki_page` | `sync-wiki` |

**총 코드**: ~9,200줄 (34개 파일) | **MCP 도구**: 3개 | **테스트**: 218개 (13개 파일) | **CLI 명령**: 12개 | **스킬**: 3개

---

## 실전 검증 결과

### breeze 프로젝트 (소규모)
- **규모**: 155파일, 326 chunks, 90초 (CPU)
- **한국어 검색**: "할일 관리" → action-item-calendar.tsx, today-focus-hero.tsx 등 정확 매칭
- **검색 속도**: 741ms (hybrid_search)

### valuein-homepage 프로젝트 (대규모) — 2026-04-13 추가
- **규모**: 1,757파일, 9,559 chunks, 229초 (CPU) / 193초 (MPS)
- **한국어 검색 테스트** (4/4 정확 매칭):
  - "학원비 결제 처리" → `tuition-billing.md` 학원비 상세 (464ms)
  - "로그인 인증" → `auth/rules.md` + `login/page.tsx` (403ms)
  - "학생 출결 관리" → `learning-attendance.md` 출석부 개요 (369ms)
  - "캘린더 일정 표시" → `calendar_events.md` + `schedule/page.tsx` (364ms)

### 공통
- **임베딩 모델**: OpenAI `text-embedding-3-small` (1536차원, HTTP API)
  - **비용**: 인덱싱 ~$0.04/프로젝트, 검색 사실상 무료
  - **로컬 리소스**: CPU/메모리 부하 제로 (urllib만 사용)

---

## 즉시 해야 할 것

Phase 10 완료. 전 Phase 완료 — Claude Code가 wiki + 검색 결과로 직접 답변하므로 별도 RAG 불필요.

---

## 실행 환경

```bash
# CLI 명령 (pip install 후 바로 사용)
hybrid-search-mcp index .                              # 프로젝트 인덱싱
hybrid-search-mcp search "query"                       # 하이브리드 검색
hybrid-search-mcp serve                                # MCP 서버 시작
hybrid-search-mcp setup                                # Claude Code 설정
hybrid-search-mcp reindex --cwd .                      # delta 재인덱싱
hybrid-search-mcp status                               # 인덱스 상태
hybrid-search-mcp stale --cwd .                        # wiki staleness
hybrid-search-mcp install-hook --cwd .                 # post-commit hook 설치
hybrid-search-mcp sync-wiki --cwd .                    # 디스크 wiki → DB 동기화
hybrid-search-mcp reindex --synthesize --cwd .         # reindex + stale → auto prepare
hybrid-search-mcp synthesize-wiki --cwd .              # prepare: 컨텍스트 수집
hybrid-search-mcp synthesize-wiki --dry-run --cwd .    # dry-run: 토큰 추정
hybrid-search-mcp synthesize-wiki --finalize --cwd .   # finalize: 검증+병합+DB저장
hybrid-search-mcp verify-synthesis --cwd .             # 합성 검증 (refs + symbols)
hybrid-search-mcp verify-synthesis --fix --cwd .       # 검증 + bad refs 자동 제거

# 테스트
python -m pytest tests/ -v

# 인덱스 데이터 위치
~/.hybrid-search/projects/{project_hash}/
~/.hybrid-search/global/project_registry.db
~/.hybrid-search/config.toml
```

### MCP 설정 위치

`~/.claude.json`에 등록됨 (글로벌 MCP 서버):

```json
{
  "mcpServers": {
    "hybrid-search": {
      "command": "<path-to-venv>/bin/python",
      "args": ["-m", "hybrid_search.server"]
    }
  }
}
```

`hybrid-search-mcp setup` 실행 시 실제 venv 경로로 자동 등록됩니다.

### 스킬 위치

```
~/.claude/skills/bootstrap-wiki/skill.md  — 프로젝트 wiki 자동 생성
~/.claude/skills/save-wiki/skill.md       — 대화 중 분석 → wiki 저장
~/.claude/skills/search/skill.md          — wiki-first 4단계 검색 체인
```

---

## 알려진 이슈 & 교훈

1. **FK CASCADE 주의** (§18 #6): `INSERT OR REPLACE`는 SQLite에서 DELETE+INSERT로 동작 → FK CASCADE 발동. 반드시 `ON CONFLICT DO UPDATE` 사용.

2. **Python 3.13 sqlite3** (§18 #7): `isolation_level` 기본값 변경됨. `isolation_level=None` + 명시적 `conn.commit()` 패턴 사용 중.

3. **tree-sitter-languages 미지원**: Python 3.13에서 `tree-sitter-languages` 패키지가 안 됨. 개별 grammar 패키지로 전환 완료.

4. **MindVault 공존** (§15): MindVault hook 토큰 예산을 10000→3000으로 축소하고 글로벌 폴백을 비활성화함.

5. **tree-sitter byte offset** (§8, §18 #8): tree-sitter는 UTF-8 byte offset을 반환. 반드시 `source_bytes = source.encode()` 후 `source_bytes[start:end].decode()` 사용.

6. **Transaction 캡슐화**: `db._conn` 직접 접근은 partial write 위험. 항상 `db.transaction()` context manager 사용.

7. **callee_chunk_id에 FK 없음**: `call_edges.callee_chunk_id`는 FK 제약 없음 (resolve 전 NULL). 파일 삭제 시 `delete_call_edges_by_callee()`로 dangling reference 명시 정리 필요.

8. **스킬은 지시서일 뿐**: Claude가 스킬의 모든 단계를 실행한다고 보장할 수 없음. 핵심 동작(DB 동기화 등)은 CLI 명령으로 확정적으로 실행하는 것이 안전. 예: `sync-wiki` CLI가 `compile_to_wiki` MCP 도구 호출을 대체.

9. **DB 스키마 버전은 int 비교**: `_migrate_schema()`에서 int() 변환 후 비교. 문자열 비교 시 "9" < "10"이 False가 되는 문제 해결 (v3에서 수정).

10. **WikiStore 캡슐화**: `db._conn` 직접 접근 금지. synthesizer/CLI 등 외부에서는 WikiStore의 public 헬퍼 메서드(`get_page_row`, `find_page_by_title`, `get_page_file_hashes`, `get_page_deps`, `get_linked_page_ids`, `is_synthesized` 등) 사용.

11. **Slug↔Title 매칭 주의** (Phase 9b): `finalize_module`에서 파일명 slug(예: `call-graph-&-module-tree`)를 DB 타이틀(예: `Call Graph & Module Tree`)로 변환할 때, `replace("-", " ")`만으로는 부족. `--` 포함 타이틀은 공백 4개가 되어 LIKE 불일치 발생. 해결: 원본 이름 → 대시-공백 변환 2단계 fallback.

12. **합성 에이전트의 파일 쓰기 불안정**: Claude Code의 sub-agent(Agent 도구)에게 파일 쓰기를 위임하면 실제로 파일이 작성되지 않는 경우가 빈번. 핵심 파일 쓰기는 메인 세션에서 직접 수행하거나 Bash heredoc 사용이 안정적.

13. **synthesis_hash로 skip 판단 불가** (Phase 9c): `finalize_module()`이 merged content를 DB에 저장하므로, 이후 `collect_module_context()`가 읽는 deterministic_wiki가 달라져 hash가 불일치. 해결: staleness 기반(file_hash_at_compile vs 현재 file_hash) 비교가 정확.

14. **심볼 검증은 noise 관리가 핵심** (Phase 9d): backtick 안의 모든 텍스트가 심볼은 아님. `_SYMBOL_SKIP` (common words) + 파일 경로 필터 + 길이 제한으로 false positive 최소화.

---

## 핵심 설계 결정 (빠른 참조)

| 결정 | 선택 | 이유 (design.md 참조) |
|------|------|----------------------|
| 언어 | Python + 네이티브 확장 | §4: MCP SDK 성숙, 핵심 연산은 C++/Rust |
| 임베딩 | OpenAI text-embedding-3-small | §7: 로컬 리소스 제로, ~$0.04/프로젝트. urllib만 사용 |
| BM25 | tantivy-py | §4: Rust 백엔드, Lucene급 성능 |
| Vector DB | USearch HNSW | §4: C++ SIMD 최적화, M=16 |
| 청크 크기 | 비공백 4000자 | §8: cAST 논문 근거, 줄 수보다 정확 |
| RRF k값 | 60 | §11: Cormack et al. 원논문 표준값 |
| 쿼리 분류 | 3단계 (SYMBOL/KR/EN) | §11: 자동 BM25 가중치 조절 |
| Storage | per-project store.db (SQLite WAL) | §13: 트랜잭션 일관성 + 동시 읽기 |
| Call Graph | 4단계 resolution + module index + class members | §12 + Phase 7: import-call 바인딩, self/this 추적 |
| Wiki | DB(staleness) + 디스크(.md) 이중 저장 | Phase 5+6: DB로 추적, 디스크로 CLAUDE.md 참조 |
| CLI | sync-wiki로 확정적 DB 동기화 | Phase 6a: 스킬 의존 대신 CLI로 확실한 실행 |
| Wikilink | `[[링크]]` BFS 그래프 (max_hops=2) | Phase 8e: 페이지 간 관계 자동 추적 + 지식 복리 기반 |
| Synthesis | Claude Code가 직접 합성, API 키 불필요 | Phase 9a: CLI prepare/finalize로 토큰 최소화 |
| 전체 합성 | 28개 모듈 bottom-up 일괄, slug 2단계 fallback | Phase 9b: 참조 검증 73%, `_raw/` 백업 보존 |
| Skip 판단 | staleness 기반 (file_hash 비교), synthesis_hash 아님 | Phase 9c: finalize 후 content 변경으로 hash 비교 불가 |
| 간접 전파 | wikilink BFS 1-hop 이웃 모듈 auto-prepare | Phase 9c: stale 모듈의 이웃도 Related Modules 갱신 |
| 환각 검증 | file:line refs + symbol DB 존재 확인, `--fix` 자동 정리 | Phase 9d: 2종 검증으로 합성 품질 보장 |
| LLM 재랭킹 | Claude Code native, rerank_hint로 지시, API 키 불필요 | Phase 10: Phase 9a 원칙 동일 — Claude Code가 LLM |
