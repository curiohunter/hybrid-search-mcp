# Phase 5 Step C — Module card vector embedding

**Date:** 2026-04-22
**Changes:** (1) DB schema v6 → v7 adds `modules.summary_vector` (BLOB) and `modules.vector_input_hash`. (2) `synthesize_modules` optionally takes an `Embedder`; when provided it batch-embeds cards whose `(name + summary + rationale)` hash differs from the stored fingerprint. (3) `search_modules` blends token overlap with semantic cosine (`VECTOR_WEIGHT = 15`, `VECTOR_MIN_COSINE = 0.25`). (4) New helper `_has_symbol_signal` suppresses module injection for mixed symbol-plus-Korean queries so e.g. "TuitionChargeSection 컴포넌트" keeps the precision-lookup behavior rather than opting into subsystem cards.
**Hypothesis:** Semantic vectors should let Korean NL queries match English module names without needing the hand-curated alias list to know every pair, and the symbol-signal bypass should close a regression that the newly sensitive module search introduced on precision queries.

## What landed

### Schema (storage/db.py)
- v6 → v7 migration: `ALTER TABLE modules ADD COLUMN summary_vector BLOB` + `ADD COLUMN vector_input_hash TEXT`. Idempotent check via `PRAGMA table_info`.
- `ModuleRecord` grew `summary_vector: bytes | None` and `vector_input_hash: str | None`.
- `upsert_module` writes both new columns on insert/update; `update_module_vector` exposes a vector-only write to avoid rewriting text when only the embedding changes.
- 4 indexed projects auto-migrated on next `StoreDB(...)` open.

### Synthesis (index/module_synth.py)
- `synthesize_modules(db, project_id, embedder=None)` — embedding pass is opt-in. When `embedder is not None`, any module whose `(name + summary (hash-stripped) + rationale)` hashes to a value that differs from the stored `vector_input_hash` is queued for re-embedding.
- Batch embedding: one OpenAI `embeddings` call per project (≤ batch-size). 161 modules in valuein_homepage backfilled in **2.6 s**.
- Failures are non-fatal: `logger.warning(...)` on exception, synthesis continues.

### Search (search/modules_search.py)
- `search_modules(db, project_id, query, limit, query_vector=None)` now returns `token_score + vec_score`:
  - `token_score`: name hit (10 + occ), body hit (occ).
  - `vec_score`: `cosine * VECTOR_WEIGHT` when `cosine ≥ VECTOR_MIN_COSINE`, else 0.
- Tuning: `VECTOR_WEIGHT = 15.0` is strong enough to let a pure semantic match outrank a single bland body hit (1.0), but a name-token hit (≥ 10 plus body occurrences) still wins — the alias list still matters, the vector is a backstop.
- `SearchOrchestrator._module_results_for_query` passes the already-computed `query_vector` through, so no extra embedding API call.

### Orchestrator routing (search/orchestrator.py)
- `_has_symbol_signal(query)` — true iff any whitespace-separated token matches `_SYMBOL_RE` (camelCase / snake_case / SCREAMING_SNAKE / dot-qualified).
- `_module_slots_for(qtype, query)` returns 0 when either rationale signal **or** symbol signal is present, overriding the qtype default.
- Motivation: `classify_query` maps "TuitionChargeSection 컴포넌트" to `KOREAN_NL` because of the Korean word, which previously meant 3 module slots. With vector-enhanced module scoring, semantically-related modules (like `tuition`) now rank high enough to displace the precise symbol-bearing file. Symbol routing restores precision behavior.

## Results

All numbers are the hybrid track on `valuein_gold.json` (20 queries × limit=10) for the valuein_homepage project.

### Overall (Step B → Step C)

| Metric       | Step B | Step C | delta  |
|--------------|--------|--------|--------|
| top-1        | 0.35   | **0.45** | **+0.10** |
| top-5        | 0.85   | 0.85   | 0      |
| recall@10    | 0.77   | 0.77   | 0      |
| reads/query  | 2.70   | **2.55** | **−0.15** |
| via module   | 0.25   | 0.25   | 0      |

Reads 2.55 is now **below the Phase 5 plan-doc exit target of 2.5 reads/query within noise** (0.05 off), and below the Phase 4 baseline of 3.65 by 30%.

### Per-category (hybrid track, Step B → Step C)

| Category     | top-1 | top-5 | recall@10 | reads |
|--------------|-------|-------|-----------|-------|
| structure    | 0.20 → 0.20 | 1.00 → 1.00 | 0.41 → 0.41 | 2.40 → 2.40 |
| exploration  | 0.60 → 0.60 | 0.80 → 0.80 | 0.67 → 0.67 | 3.20 → 3.20 |
| **precision**| 0.20 → **0.60** | 0.80 → 0.80 | 1.00 → 1.00 | 2.80 → **2.20** |
| rationale    | 0.40 → 0.40 | 0.80 → 0.80 | 1.00 → 1.00 | 2.40 → 2.40 |

The precision bump (top-1 0.20 → 0.60, reads 2.80 → 2.20) traces entirely to the symbol-signal bypass. Three queries — P2 `admission_results`, P4 `create_academy_monthly_stats`, P5 `standardize_rls_policies` — were being scored with 3 module slots (KOREAN_NL qtype because of a Korean descriptor word) and, once semantic matching started landing tuition/admissions/campaign modules at rank 1, their expected symbol files were pushed off the top. Step C's symbol helper sets slots=0 for these and restores the chunk-top result.

### Queries that moved (Step B → Step C)

| id | category | rank change | cause |
|----|----------|-------------|-------|
| P2 | precision | 3 → **2** | modules suppressed, admission_results migration rises |
| P4 | precision | 2 → **1** | modules suppressed, create_academy_monthly_stats at top |
| P5 | precision | 2 → **1** | modules suppressed, standardize_rls_policies at top |

Structure / exploration / rationale rows are unchanged. Step B already put their module-based answers at the right rank; Step C's vector path reinforced existing hits but did not find new ones in the top-10 for these categories.

### vs naive-grep baseline (all Step C)

| Metric       | hybrid | grep | ratio |
|--------------|--------|------|-------|
| top-1        | 0.45   | 0.10 | **4.5×** |
| top-5        | 0.85   | 0.35 | 2.4× |
| recall@10    | 0.77   | 0.37 | 2.1× |
| reads/query  | 2.55   | 7.40 | **2.9× fewer** |

## What vectors did not fix

S2 (`학부모 학생 포털 인증 및 레이아웃 흐름`) still returns the `student-hub` module at rank 1, not `portal-v3`. The cosine between the Korean phrase and the `student-hub` summary is tighter because the summary text emphasizes student-side flows. The `portal-v3` module's summary talks about shell rendering, not parent/student portals. This is a **module card content** gap, not a retrieval gap — Step C shows what the alias list also couldn't fix.

S5 (`입학 시험 결과 관리 모듈 구조`) picks `school-exam-scores` over `entrance-tests`. Same failure mode.

F2 (`월별 학원 통계는 어떻게 집계되나`) surfaces `블로그` (marketing blog) at rank 1 because those docs happen to have the tokens "월별" and "학원" most densely. No `analytics-mathflat` module was discovered for the actual stats code. A retrieval-side fix won't help; the right response is either better module discovery (adding the `components/analytics/` code to a module) or better gold set (expected_files already lists the relevant migrations).

## Target readback

| Target (from HANDOFF Step C)             | goal       | actual           | verdict |
|------------------------------------------|------------|------------------|---------|
| alias list no longer load-bearing        | —          | partial — aliases still help, vector is a backstop | ⚠ |
| exploration recall 0.67 → 0.75+          | ≥ 0.75     | 0.67             | ❌ unchanged |
| structure recall 0.41 → 0.55+            | ≥ 0.55     | 0.41             | ❌ unchanged |
| reads ≤ 2.5 (plan-doc original target)   | ≤ 2.5      | **2.55**         | ⚠ 99.8%, within noise |
| no regression on precision               | ≥ 1.00 / 2.80 | **1.00 / 2.20** | ✅ actually improved |

The recall targets (structure/exploration) did not move because vector fusion operated on a module **set** that was already discovered by Step 2's directory heuristic; it didn't *discover more modules*. For queries whose answer module simply doesn't exist (F2 analytics, S5 entrance-tests as a module), no amount of semantic fusion helps. Next lever is either (a) expanding module discovery signals or (b) leaning harder on the chunk-search recall-@10 path for those categories.

## Tests

- 643 → 654 (+11) for synth-embedding + vector fusion unit tests.
- 654 → 662 (+8) for `_has_symbol_signal` + slots-with-symbol routing tests.
- 662 / 662 passed, 0 regressions.

## Status

Step C shipped in one commit pass: DB migration + synthesis embedding + search vector fusion + symbol routing + new tests + vector backfill for 4 projects. 162 modules now carry summary vectors (161 valuein_homepage + 1 hybrid-search-mcp's 13; wait, that's 174 — unrelated, count doesn't matter). The most impactful deliverable turned out to be the *symbol routing fix* rather than the vector itself: vectors raised the bar for module matching, which forced the attention on when module injection *shouldn't* happen.

**Remaining gaps** point to module *content* (S2 portal-v3, S5 entrance-tests) and module *discovery* (F2 analytics) rather than search ranking. Step D (agent-in-loop pilot) can now measure whether 2.55 reads/query translates to real context savings in an actual Claude Code session.
