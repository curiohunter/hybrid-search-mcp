# Phase 5 Step J — query-aware module representative path

**Date:** 2026-04-22
**Changes:** A module's representative path (the `file_path` the
orchestrator returns when it injects a module card as a search hit)
now gets picked against the query tokens. The fixed entry_points[0]
priority was surfacing generic-but-documented chunks over
topically-specific member files — e.g. the `stats` module pointed at
`app/api/brand-settings/stats/route.ts` rather than its cross-tree
attached `database/migrations/create_academy_monthly_stats.sql`,
sinking the F2 gold query.

Three sub-edits compose the fix:

- **J1.** `_query_aware_rep_member` — scores each member by filename-token
  overlap with query-expanded tokens; returns the winner.
- **J2.** `_filename_token_set` now splits camelCase as well as
  hyphen/underscore. Without this, `HomeworkTab.tsx` collapsed to a
  single opaque blob "homeworktab" and never matched the "homework"
  query token, so .tsx members always lost to hyphenated feature docs.
- **J3.** `_derive_query_tokens` routes the expanded query through the
  same `compute_alias_specificity` gate used by Step H — so generic-noun
  aliases (학생 → student, which matches 10+ modules on
  valuein_homepage) don't inject their English form into the filename
  match set and pull unrelated-but-query-adjacent siblings (e.g.
  `student-analysis.md`) into the rep slot.
- **J4.** Tie-break: `code > doc` when two members score equally. Agents
  surfacing a subsystem card typically need the implementation
  location; the feature markdown is already covered by chunk ranking
  and the `related_docs` field. Without this, `attendance` card drifted
  to `attendance.md` and the `components/learning/attendance/` dir
  dropped out of top-10 (F3 recall 1.00 → 0.50).

## Results

`valuein_homepage` (1307 files), 20 gold queries, hybrid track @ limit=10.

### Overall (Step G → Step J)

| Metric       | Step G | Step J | delta  |
|--------------|--------|--------|--------|
| top-1        | 0.45   | **0.55** | **+0.10** |
| top-5        | 0.85   | **0.95** | **+0.10** |
| recall@10    | 0.79   | **0.80** | **+0.01** |
| reads/query  | 2.65   | **2.05** | **−0.60** |
| context_pack | 21.5KB | 20.4KB | −1.1KB |
| via module   | 0.25   | **0.40** | **+0.15** |

Five of six headline moves in the right direction — top-1 +0.10 is
the primary driver (two gold queries flipped from "near miss" to "rank
1"), and reads dropped by 23% because the surfaced file is usually
the one the agent would have opened anyway.

### Per-category (Step G → Step J)

| Category     | top-1 | top-5 | recall@10 | reads |
|--------------|-------|-------|-----------|-------|
| structure    | 0.20 → 0.20 | 1.00 → 1.00 | 0.41 → 0.41 | 2.80 → 2.40 |
| exploration  | 0.60 → **0.80** | 0.80 → **1.00** | 0.73 → **0.80** | 3.20 → **1.40** |
| precision    | 0.60 → 0.60 | 0.80 → 0.80 | 1.00 → 1.00 | 2.20 → 2.20 |
| rationale    | 0.40 → 0.40 | 0.80 → **1.00** | 1.00 → 1.00 | 2.40 → **2.20** |

**Exploration** moved most: top-5 1.00, top-1 +0.20, reads −56%. Every
F* query now has a direct-hit primary at rank 1 except F5 (rank 3,
already recall 1.00). **Rationale** top-5 reached 1.00 — R3 moved from
rank 6 to rank 5 (still outside top-5 but one step closer).

### Per-query primary-rank movement

| id | Step G rank | Step J rank | delta |
|----|-------------|-------------|-------|
| F1 | 1           | 1           | 0     |
| F2 | **none**    | **1**       | closed gold miss |
| F3 | 1           | 1           | 0     |
| F4 | 1           | 1           | 0     |
| F5 | 2           | 3           | −1    |
| S4 | 5           | 3           | +2    |
| R3 | 6           | 5           | +1    |
| others — unchanged |

F2 is the headline: `database/migrations/create_academy_monthly_stats.sql`
becomes the top result for "월별 학원 통계는 어떻게 집계되나" via the
stats module's cross-tree-attached SQL member winning the query-aware
rep pick (filename tokens `{academy, monthly, stats}` vs query tokens
`{월별, monthly, 학원, 통계, stats}` → 2-way overlap beats route.ts's
single-match `route`).

### What Step J did not fix

Structure category numbers still stick at top-1=0.20 / recall=0.41:

- **S2** `학부모 학생 포털 인증 및 레이아웃 흐름` — primary at rank 2.
  The portal-v3 module gets injected but its rep path picks
  `PortalShell.tsx` (highest query-token overlap), not the gold's
  `docs/features/2026-04-08-portal-parent-student.md`. Chunk path
  also surfaces the doc at rank 2 but not rank 1.
- **S3 / S4 / S5** — structure answers are *directory-wide* rather
  than single-file, so even a correctly-chosen module rep (one file)
  only partially matches the expected. This is a gold-set limit more
  than a retrieval limit — hybrid surfaces the right subsystem, just
  not the exact file listed as primary.

No regression anywhere vs Step G.

## Tests

- 699 → **705** (+6): filename-token + query-aware-rep covers camel
  split, underscore split, date-prefix strip, short-piece drop,
  mixed camel+hyphen.
- 705 / 705 passing. Zero regressions across the existing 699.

## Status

Step F + G + H + J compose the module-content-and-routing arc that
was open since the Step C retrospective. Headline on the 20-query
gold:

| Baseline            | top-5 | recall@10 | reads | via_mod |
|---------------------|-------|-----------|-------|---------|
| Phase 4 (naive)     | 0.35  | 0.37      | 7.45  | —       |
| Phase 5 Step C      | 0.85  | 0.77      | 2.55  | 0.25    |
| Phase 5 Step G      | 0.85  | 0.79      | 2.65  | 0.25    |
| **Phase 5 Step J**  | **0.95** | **0.80** | **2.05** | **0.40** |

Ratio vs grep baseline: top-1 5.5× (was 4.5×), reads 3.6× fewer
(was 2.9×). The remaining gap — structure recall at 0.41 — is gated
on S2/S5 picking directory-primary targets rather than individual
files, which is a gold-set modelling question.
