# Router Replay — 2026-05

**Status:** ✅ MEASURED — 2026-05-13
**Owner:** karw79@gmail.com (manual replay on valuein_homepage)
**Goal:** **G4** — ≥ 90 % first-pick tool correctness with router + v1 marker block enabled vs baseline.
**Outcome:** Phase 4 가치 재정의 — first-pick correctness uplift는 측정 불가 (baseline 이미 100% charitable), Phase 4의 진짜 가치는 **qa_log priming → CLAUDE.md 영구 계약화** + **idempotent migration**.

---

## Setup

- Repo: `/Users/ian/project/claude_project/valuein_homepage`
- hybrid-search-mcp: dirty branch, `pip install -e`로 installed
- Treatment: CLAUDE.md에 `BEGIN/END hybrid-search-mcp routing v1` 페어 + self-justify + confidence contract, `HYBRID_SEARCH_ROUTER` unset (default on)
- Baseline: legacy `<!-- hybrid-search -->` 마커만, self-justify/confidence 룰 없음, `HYBRID_SEARCH_ROUTER=0`
- Toggle helper: `/tmp/g4-toggle.sh {baseline|treatment}`
- 8 sessions total (4 baseline + 4 treatment), 각 세션 새 Claude Code 인스턴스

---

## Source: valuein field report v2 (4 win cases)

Prompts taken verbatim from `~/.claude/projects/.../memory/project_valuein_field_report_v2.md`.

### Case 1: 학생이 숙제 제출하면 어디서 분석되나
**Routing-table expected:** `hybrid_search` (기능 탐색 / NL flow)

| | first tool | first-call evidence | charitable | strict |
|---|---|---|---|---|
| baseline   | hybrid_search | `Called hybrid-search` + self-justify "탐색형 질문이라 ... 먼저 호출합니다" | ✅ | ✅ |
| treatment  | hybrid_search | 동일 self-justify 패턴 | ✅ | ✅ |

### Case 2: portal v3로 리팩토링하는 이유는 무엇인가
**Routing-table expected:** `hybrid_search` (설계/맥락 / 히스토리)

| | first tool | first-call evidence | charitable | strict |
|---|---|---|---|---|
| baseline   | hybrid_search | self-justify + Called hybrid-search; weak fallback으로 plan doc 직접 Read | ✅ | ✅ |
| treatment  | Read          | Pre-fetch가 `docs/plans/completed/2026-04-15-portal-v3-refactoring.md` surface → 즉시 Read | ✅ (pre-fetch 정답 surface) | ❌ |

### Case 3: AI 에이전트 아키텍처 전체 그림
**Routing-table expected:** `hybrid_search` (구조/관계 → Wiki + hybrid_search)

| | first tool | first-call evidence | charitable | strict |
|---|---|---|---|---|
| baseline   | hybrid_search + Wiki 병렬 | self-justify "hybrid_search 먼저 호출하고, 보충으로 Wiki" | ✅ | ✅ |
| treatment  | hybrid_search + Wiki 병렬 | 동일 패턴 | ✅ | ✅ |

### Case 4: admission_results 테이블 스키마
**Routing-table expected:** `hybrid_search (file_pattern *.sql)` (스키마/DB)

| | first tool | first-call evidence | charitable | strict |
|---|---|---|---|---|
| baseline   | Supabase MCP | `Called supabase` — 라이브 DB 스키마 쿼리 | ✅ (실제 DB가 SQL 파일보다 정확) | ❌ |
| treatment  | Supabase MCP | 동일 | ✅ | ❌ |

---

## Summary

| | Baseline | Treatment | Delta |
|---|---|---|---|
| Charitable (현명한 retrieval) | **4/4** | **4/4** | 0 |
| Strict (라우팅표 정확 일치) | **3/4** | **2/4** | -1 |

- **G4 target (≥ 90 % first-pick correctness charitable)**: **✅ 통과**
- 단, **baseline → treatment delta는 0**. router/v1 마커가 first-pick에 측정 가능한 영향을 못 줌.

---

## 측정 오염 — 정직한 분석

Baseline 세션에서도 Claude가 **self-justify 언어**("탐색형 질문이라 ... 먼저 호출합니다")와 **confidence weak fallback 언어**를 그대로 사용함. 이건 baseline CLAUDE.md에 없는 규칙. 가능한 원인:

1. **qa_log priming (가장 그럴듯)** — 같은 날 Treatment 세션에서 self-justify/confidence 언어를 쓴 qa_log가 `.hybrid-search/qa/2026/05/`에 누적됨. Pre-fetch hook이 같은 프롬프트에 해당 qa_log를 surface → Claude가 과거 자신의 패턴을 보고 그대로 따라함. 즉 **Memory Layer가 이미 학습시킨 행동이 CLAUDE.md 텍스트보다 강함**.
2. Global memory layer 효과 — `~/.claude/CLAUDE.md`나 다른 글로벌 컨텍스트의 priming.
3. Codex/Claude 일반 학습 — 흔한 한국어 self-justify 패턴.

→ **Phase 4의 코드 추가가 first-pick correctness에 미친 효과는 깨끗한 측정 불가**. 측정하려면 qa_log 전체 wipe 후 fresh Claude 세션 필요 (사용자 시간 비용 큼, 권장하지 않음).

---

## Phase 4 가치 재정의 (post-measurement)

측정 후 Phase 4의 실제 기여를 정직하게 재정의:

1. **❌ 가설 — "router/v1 마커 → first-pick correctness 향상"**: 측정 차이 없음 (baseline 이미 4/4 charitable).
2. **✅ qa_log 의존 패턴의 영구화** — qa_log는 휘발성 (TTL, prune, 다른 세션). CLAUDE.md에 self-justify/confidence 룰을 박으면 qa_log가 비어도 일관성 유지.
3. **✅ 명시적 계약** — 사용자가 디버깅/감사할 때 "이 규칙이 어디에서 왔는지" 한 줄 grep으로 찾을 수 있음.
4. **✅ Idempotent migration** — `setup` 재실행으로 legacy 단일-마커 → BEGIN/END v1 페어 자동 교체, 운영 부담 ↓.
5. **✅ Versioned sentinel marker** — 향후 routing v2가 필요할 때 명확한 migration path.

이 가치들은 **즉시 측정되지 않지만 시간이 지날수록 누적**됨. ship 추천.

---

## Reproducibility

- Treatment snapshot: `/tmp/g4-treatment-CLAUDE.md` (측정 종료 후 자동 정리됨)
- Baseline snapshot: `/tmp/g4-baseline-CLAUDE.md` (동상)
- Pre-Phase4 CLAUDE_MD_SECTION 원본: `git show 8a596a5:src/hybrid_search/cli.py | sed -n '154,185p'`
- 측정 시점 valuein git state: branch `feature/archive-learning-legacy`
