# Phase 5 Step A — Rationale intent routing

**Date:** 2026-04-22
**Change:** `_module_slots_for` now returns 0 for queries carrying a rationale signal ("이유", "배경", "목적", "의도", "동기", "취지", "왜", "rationale", "why", "reason", "motivation", "purpose", "intent", "background").
**Hypothesis:** Module injection on "why"-class queries buries the right answer — a single plan/design doc — behind subsystem cards. Routing those queries back to chunk-only retrieval should restore `rationale` top-1 and `read_count`.

## Delta vs Phase 5 Step 4 (module-first, all queries get modules)

Per-category, hybrid track (20 gold queries × limit=10):

| Category     | top-1        | top-5        | recall@10    | reads        | pack (bytes) |
|--------------|--------------|--------------|--------------|--------------|--------------|
| structure    | 0.00 → 0.00  | 0.60 → 0.60  | 0.41 → 0.41  | 5.00 → 5.00  | 12015 → 12015 |
| exploration  | 0.20 → 0.20  | 0.40 → 0.40  | 0.67 → 0.67  | 6.60 → 6.60  | 10415 → 10415 |
| precision    | 0.20 → 0.20  | 0.80 → 0.80  | 1.00 → 1.00  | 2.80 → 2.80  | 6650 → 6650  |
| **rationale**| **0.00 → 0.40** | 0.80 → 0.80 | 1.00 → 1.00 | **4.00 → 2.40** | 45164 → 45292 |

**Overall:** top-1 **0.10 → 0.20**, top-5 0.65 → 0.65, recall@10 0.77 → 0.77, reads **4.60 → 4.20**, pack 18561 → 18593 bytes.

## Target readback

| Target (from HANDOFF Step A)       | goal         | actual       | verdict |
|------------------------------------|--------------|--------------|---------|
| rationale reads 4.00 → ~2.40       | ~2.40        | **2.40**     | ✅ exact match |
| structure/exploration/precision unchanged | identical | identical  | ✅ no regression |
| rationale recall preserved         | 1.00         | **1.00**     | ✅ |
| Overall reads pull-back            | < 4.60       | **4.20**     | ✅ −0.40 |

Bonus: rationale top-1 jumped from 0.00 to **0.40** (2/5 rationale queries now hit at rank 1 — R1 portal-v3 and R2 ledger-abc).

## Why it worked

For the five rationale gold queries:

| id | query | signal token | slot pre | slot post | rank before | rank after |
|----|-------|-------------|----------|-----------|-------------|------------|
| R1 | portal v3로 리팩토링하는 **이유**는 무엇인가 | 이유 | 3 | 0 | 2 | **1** |
| R2 | ledger writepath ABC 설계를 택한 **배경**은 | 배경 | 3 | 0 | 2 | **1** |
| R3 | tuition hub를 새로 만드는 **이유** | 이유 | 3 | 0 | 7 | 6 |
| R4 | AI 콘텐츠 팩토리를 만드는 **목적** | 목적 | 3 | 0 | 3 | 2 |
| R5 | entrance test 관리 플랜은 **왜** 세워졌나 | 왜 | 3 | 0 | 3 | 2 |

With modules suppressed, the plan-doc chunk that was already ranked #2 (behind a module card) becomes the #1 result. R3 remains stubborn at rank 6 — the plan doc has weaker keyword overlap with "tuition hub" than the tuition subsystem files. Step B (gold v2 accepting module names as primary) or Step C (module card vector embedding) would likely address R3; Step A alone does not.

## What did not change (by design)

- Structure / exploration categories use module slots exactly as before → structure recall still 0.41, exploration still 0.67. Step A only shrinks the module-injection domain; it does not redesign retrieval.
- Precision was already at reads=2.80 (module slot=0 for `EXACT_SYMBOL`), so precision queries never saw modules. Step A is a pure win for the Korean/English NL rationale subset.

## False-positive analysis

Signal tokens picked deliberately to avoid overlap with non-rationale NL queries. Verified none of the 15 non-rationale gold queries contain `이유`, `배경`, `목적`, `의도`, `동기`, `취지`, `왜`, `rationale`, `why`, `reason`, `motivation`, `purpose`, `intent`, or `background`. Word-boundary match on English tokens ("purpose" vs "multipurpose") covered by unit test.

## Status

- Step A rationale reads target **met exactly** (4.00 → 2.40).
- Overall reads **4.60 → 4.20**, still above the 3.65 Phase-4 baseline. Further progress requires Step B (gold set v2 — module names valid as primary) and Step C (module card vector embedding replacing the 25-alias heuristic).
- Tests: 643 passed (631 + 12 new rationale routing tests), 0 regressions.
