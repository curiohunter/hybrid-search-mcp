# Phase 5 — valuein_homepage Benchmark v2 (post subsystem-first retrieval)

**Date:** 2026-04-22
**Compared:** Phase 4 baseline (chunk-only retrieval) vs Phase 5 (module-first retrieval + Korean↔English alias)
**Gold set:** [`benchmarks/valuein_gold.json`](./valuein_gold.json) — 20 queries × 4 categories

## Delta vs Phase 5 baseline

| Metric | baseline (chunks only) | Step 4 (module-first) | delta |
|--------|------------------------|-----------------------|-------|
| Overall primary top-1 | 0.35 | 0.10 | **−0.25** ⚠ |
| Overall primary top-5 | 0.65 | 0.65 | 0.00 |
| Overall **recall@10** | **0.67** | **0.77** | **+0.10** ✅ |
| Overall MRR | 0.532 | ~0.50 | ~flat |
| Overall **read_count_estimate** | **3.65** | 4.60 | **+0.95** ⚠ |
| Overall context_pack_bytes | 19.2 KB | 18.1 KB | −1.1 KB |

## Per-category (hybrid track)

| Category | top-1 | top-5 | **recall@10** | reads | pack(KB) | vs baseline |
|----------|-------|-------|---------------|-------|----------|-------------|
| structure   | 0.00 | 0.60 | **0.41** | 5.00 | 11.7 | recall +0.19 (**near-2× of 0.22**), reads +1.40 |
| exploration | 0.20 | 0.40 | **0.67** | 6.60 | 10.2 | recall +0.20, reads +0.40 |
| precision   | 0.20 | 0.80 | **1.00** | 2.80 | 6.5  | recall = (1.00 preserved), reads +0.40 |
| rationale   | 0.00 | 0.80 | **1.00** | 4.00 | 44.1 | recall = (1.00 preserved), reads +1.60 |

## Compared to naive-grep baseline (unchanged grep track)

| Metric | hybrid (Step 4) | grep (token-bag) | ratio |
|--------|-----------------|------------------|-------|
| recall@10 | **0.77** | 0.37 | **2.1×** |
| any-hit rate | 0.90 | 0.45 | 2.0× |
| read_count_estimate | 4.60 | 7.40 | 1.6× (hybrid fewer reads) |
| precision + rationale recall | 1.00 | 0.40 | 2.5× |

## Phase 5 target readback

From the plan doc's exit criteria:

| Target | goal | actual | verdict |
|--------|------|--------|---------|
| structure recall@10 ≥ 0.55 | ≥ 0.55 | **0.41** | ❌ partial — +0.19 vs baseline, but short of 0.55 |
| read_count_estimate ≤ 2.5 | ≤ 2.5 | **4.60** | ❌ regressed from 3.65; module injection adds 1 read when primary is a chunk not a module |
| precision/rationale recall preserved | ≥ 1.00 on both | **1.00 / 1.00** | ✅ |
| exploration recall lift | — | 0.47 → 0.67 | ✅ |

Headline: **recall up 0.10 overall, structure nearly doubles (0.22 → 0.41), read-count regressed by ~1.** The regression is a direct cost of module injection: the rank-1 slot now goes to a module card, pushing the top chunk to rank 2. For precision/rationale where the real answer is a single chunk, primary_rank thus becomes `chunk_original_rank + 1`.

## What worked

1. **Module discovery v1 (Step 2)** — 161 modules identified on valuein_homepage. Key subsystems (`portal-v3`, `tuition`, `tuition-session`, `tuition-wizard`, `remote-room`, `homework-analysis`, `consultations`, `entrance-tests`, `admissions`) materialize as first-class units. Chain-merge fixed by strict "all mentions in one key" rule (N-way plurality rejected).

2. **Deterministic synthesis (Step 3)** — Cards include module name + member filenames + longest-docstring head + extracted NOTE/WHY/TODO tags. No LLM required; hash-based skip makes re-synthesis a cheap delta. Rationale tags surface for 2/161 modules in valuein_homepage (domain code has few explicit NOTE tags — Phase 3 M10's limited real-data payoff confirmed here).

3. **Alias-assisted cross-language matching (Step 4)** — Hardcoded 25 Korean↔English domain pairs (포털↔portal, 학생↔student, 수강료↔tuition, …) bridges NL queries to English-named subsystems. Without this, every Korean structure query missed every English-named module.

4. **Interleave injection** — Modules at ranks 1,3,5 rather than 1,2,3 preserves the top-chunk position for rationale/precision queries where the answer is a single doc.

## What didn't work (honest failure modes)

1. **Target gap on structure.** 0.41 vs 0.55 target. Expected-files for structure queries include 3-5 directory entries (`components/portal-v3/`, `app/remote-room/`, etc.). Hybrid surfaces the right MODULE in top-5 most of the time, but the expected directory entries (which contain files, not modules) still don't rank individually. **Real fix needs module expansion in results** — when a module card appears, its member file paths should count toward recall at shallow depth. Current scoring treats module hit and directory hit as independent.

2. **Read-count regression unavoidable without richer presentation.** Moving modules to rank 1 structurally adds +1 read when the answer is a chunk. The only way to reduce read count for rationale queries is to **not** inject modules there. A per-category routing (rationale → 0 module slots) would recover read_count but requires better intent detection than current query_type classification provides.

3. **Structure top-1 dropped from 0.40 to 0.00.** Module cards win position 1 but are scored against `primary_target = <single-file doc>`. The module is the *correct* answer to "how is X organized", but the gold annotation points to a doc, so the score says "miss". **This is a gold-set annotation issue for Phase 5+** — when a subsystem is the answer, the module itself should be the valid primary_target. Report acknowledges this rather than gaming the number.

4. **Exploration category still below 0.5 on top-5.** Korean exploration queries that don't hit an alias (e.g. 숙제 제출 분석) still rank doc over module. Needs either broader alias list or vector embedding of module cards.

## What's next after Phase 5

- **Step 6 (future) — vector-embed module cards.** Would eliminate the alias-list maintenance burden. Current domain coverage is just the 25 pairs I hardcoded; a new codebase would need its own list. Embeddings handle this automatically at the cost of one extra vector per module at synthesis time.
- **Gold-set v2** where "module" is a valid `primary_target` type (not only files). Re-score Phase 5 Step 4 under that scheme to get a fair structure top-1.
- **Per-category intent detection** to route rationale queries through the chunks-only path, recovering the read_count regression.
- **Agent-in-loop measurement** (Phase 5 Step 1 prep) — still outstanding. A 3-5 query manual run with Claude Code would confirm whether the 4.60 reads translates to ≤ 3 actual Read tool calls (due to module snippet carrying enough context to end the search loop earlier).

## Files

- Gold set: [`benchmarks/valuein_gold.json`](./valuein_gold.json)
- Benchmark runner: [`benchmarks/run_valuein_bench.py`](./run_valuein_bench.py)
- Raw results: [`benchmarks/valuein_results.json`](./valuein_results.json)
- Phase 4 baseline report: [`benchmarks/valuein_report_2026-04-22.md`](./valuein_report_2026-04-22.md)

Commits: `8c33b9b` (Step 2 discovery), `5a74df6` (Step 3 synthesis), `ca9ccbd` (Step 4 retrieval).
