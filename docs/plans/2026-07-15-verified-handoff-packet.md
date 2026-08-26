# Verified Handoff Packet — 다음 사이클 P0 스펙

**Status:** DESIGN CONDITIONALLY APPROVED — 2026-07-15 (Codex 설계 리뷰,
완성도 85% 판정 → 아래 §7 필수 조건 7건 반영으로 구현 착수 가능.
PR #5 라운드 2~4와 병렬 설계, 구현은 PR #5 머지 후 착수)
**승격 근거:** 2026-07-15 실사용 — Codex가 "가장 최근 작업" 재구성에 검색 3콜을
쓰고도 stale한 "Request changes" 상태를 답함. 검색은 freshness는 고칠 수 있어도
(762c94f) state는 못 고침: "R1-02가 해결됐는가"는 확률적 랭킹이 아니라 장부
조회의 문제. 코덱스 평가 원문: "여러 에이전트가 같은 검색 인덱스를 본다"가
아니라 **"하나의 현재 작업 상태와 검증된 인수인계 계약을 공유한다"**.

---

## 1. 모델: append-only journal + current projection

```
.hybrid-search/handoff/
├── <workstream_id>/
│   └── journal.jsonl        # append-only 이벤트 (감사 로그, 불변)
└── current/
    └── <workstream_id>.yaml # 최신 projection (journal에서 결정론적 재계산 가능)
```

- **workstream** = repo + branch + 작업 단위 (예: `trust-layer-pr5`).
  최신성은 repo 전체가 아니라 workstream 기준.
- journal 이벤트: `plan_published | review_posted | finding_resolved |
  implementation_posted | approval | phase_changed`. 각 이벤트는
  agent / commit SHA / evidence / resolves[] / timestamp를 가짐.
- projection(코덱스 초안 스키마 채택): workstream_id, phase, current_commit,
  plan{path, **content_hash**, version}, latest_action{agent, summary,
  evidence[]}, open_findings[], resolved_findings[], next_action{agent,
  action, target_commit, review_round}.
- 저장은 local-first (.hybrid-search/ 하위, gitignore 정책 동일).

## 2. 표면 — 신규 MCP 도구 0개 (기존 원칙 준수)

코덱스 제안(`handoff_current`/`handoff_publish` MCP 2종) 대비 수정:

- **읽기 = SessionStart 훅 자동 주입 (0콜).** 양쪽 에이전트에 이미 설치된
  SessionStart 훅이 cwd의 active workstream projection을 주입. 합격 기준
  "MCP 1회"를 "0회"로 초과 달성. projection ≤ 2,000 tokens 강제(필드 캡).
- **쓰기 = CLI.** `hybrid-search-mcp handoff publish --phase ... --commit ...
  --resolves R1-01,R1-02 --evidence tests:1296/1296,ci:green` +
  `handoff current` (수동 조회) + `handoff open <workstream>` / `close`.
  두 에이전트 모두 Bash 보유 — 명시적 발행이 감사에도 유리.
- **자동 파생 evidence**: post-commit 훅이 commit SHA를, CI 상태·테스트 카운트는
  publish 시 검증 가능한 형식(`tests:<n>/<n>`, `commit:<sha>`)으로만 수용.

## 3. 라우팅 계약 (CLAUDE.md / AGENTS.md 템플릿 개정)

현재 작업 상태 질문의 순서 — **semantic search는 3순위**:

1. 주입된 handoff projection (0콜)
2. `recent_activity` fast path (timestamp 역순 결정론 조회 — 함께 P0 승격,
   기존 P2 recency fast path가 이것)
3. hybrid_search (배경·왜 질문에만)

## 4. 불변조건 (코덱스 목록 채택 + 추가)

- 리뷰는 항상 특정 SHA에 귀속. plan은 path + content hash.
- unresolved finding만 다음 에이전트에 전달. 완료 주장은 evidence 필수.
- 이전 phase 이벤트는 현재 projection을 덮어쓸 수 없음 (event ordering).
- 현재 작업 조회에 semantic search 사용 금지 (라우팅 계약으로 강제).
- **[추가] publish 누락 안전망**: Stop 훅이 "phase 전이 신호(커밋/리뷰 패턴)
  감지 + 미발행" 상태를 다음 SessionStart 주입에 경고로 표시.
- **[추가] journal ↔ typed memory 통합**: handoff 이벤트는 P1-1 스키마의
  최상위 등급 레코드(decision/accepted, review_finding, task_state/verified)로
  qa 인덱스에도 흘림 — 장부(정확한 상태)와 검색(맥락)이 한 데이터의 두 뷰.
  별도 두 시스템 금지.

## 5. 합격 기준 (코덱스 기준 채택, 1건 강화)

- Claude↔Codex 5회 왕복 phase 정확도 100%, open/resolved finding 누락 0,
  잘못된 SHA 리뷰 0
- handoff 로딩 **0콜** (SessionStart 주입), 초기 payload ≤ 2,000 tokens
- raw conversation/search 호출 없이 다음 행동 결정 가능
- **Agent Handoff Trust Bench의 시나리오 S1~S6이 이 packet 위에서 실행** —
  벤치와 제품이 같은 계약 사용

## 6. 설계 리뷰 필수 조건 (2026-07-15 조건부 승인, 전부 채택)

1. **주입은 SessionStart만으로 부족** — 창을 켜둔 채 왕복하면 SessionStart가
   재발화하지 않음. `UserPromptSubmit`에서 projection version 변경 감지 시
   **delta만 재주입** (SessionStart: 전체 compact / UserPromptSubmit: delta).
2. **동시 publish 충돌 방지 (CAS)** — timestamp 순서 금지. 이벤트마다
   `event_id`, monotonic `sequence`, `previous_event_id`,
   `expected_projection_version`. 오래된 세션의 publish는 CAS 실패로 거부 —
   해결된 finding 재오픈/이전 phase의 현재 덮어쓰기 차단.
3. **type ≠ verification** — `review_finding`이라는 type이 신뢰도를 함의하지
   않음. decision/accepted는 실제 사용자 승인, task_state/verified는 시스템이
   commit/test evidence를 **실제 검증**했을 때만. `tests:1296/1296` 같은
   형식 통과는 구문 검사일 뿐 → 실행 로그 미확인 evidence는
   **`claimed`** 등급으로 저장 (verification 값에 claimed 추가:
   verified > accepted > claimed > inferred). claimed는 strong 앵커 불가.
4. **projection drift 명시 탐지** — 주입 시점마다 HEAD↔current_commit,
   plan 실제 hash↔plan.content_hash, 현재 branch↔workstream branch 대조.
   불일치 시 정상 주입 금지, `state: possibly_stale` + drift 사유
   (`plan_changed_without_publish`, `head_ahead_of_projection`)로 주입.
5. **publish 누락 = 신뢰도 강등** — Stop 훅이 phase 전이 신호 감지 + 미발행이면
   경고 텍스트가 아니라 `publish_required` **상태**로 전이. 다음 에이전트가
   낡은 projection을 현재 사실처럼 쓸 수 없게.
6. **`recent_activity` 표면 확정** — 기존 reader(`memory/reader.py`)를
   UserPromptSubmit 훅 내부에서 결정론적으로 실행(timestamp 역순), 수동
   fallback은 기존 `qa-list`/`qa-show` CLI. 이름만 있고 표면이 없으면
   에이전트가 다시 semantic search로 이탈.
7. **projection 토큰: 2,000은 상한, 목표는 400~800.** 주입 우선순위:
   phase/current SHA → open findings → next action → plan path/hash →
   latest evidence → resolved findings는 개수만.

## 7. 우선순위 (코덱스 제안 대비 순서 조정)

1. **PR #5 라운드 2~4 완주 (동결 스코프)** — 리뷰 베이스라인 보호.
   본 문서 설계는 병렬 진행.
2. **다음 PR P0: Handoff Packet + workstream** (본 문서)
3. **P0: `recent_activity` fast path** (구 P2 recency — 승격)
4. **P0: Stop→recall freshness 보장** (기록 지연 창 축소: publish 이벤트는
   즉시 projection 반영이므로 packet이 큰 부분을 해소)
5. 3차 frozen holdout (R1/ADV3 — PR #5 머지 후, handoff와 독립)
