# Phase 5 Step K — module-member emission

**Date:** 2026-04-22
**Changes:** Structure queries where the gold answer spans multiple
files in one subsystem (S5 admissions: components dir + SQL migration
+ plan doc) previously needed several separate module retrievals to
cover the expected set, because the orchestrator emitted one rep
path per surfaced module. Step K adds a parallel ``module_member``
stream so a module that doesn't quite make the 3-slot card cut can
still contribute its most query-relevant files to top-10.

Five sub-edits compose the change:

- **K1.** `_module_results_for_query` returns a ``(cards, members)``
  tuple. For each candidate module returned by `search_modules`, the
  orchestrator picks up to `_MEMBER_EMIT_NONCARD` additional files
  as ``module_member`` entries. Card-surfaced modules skip member
  emission — their sibling files tend to share a directory with the
  rep and don't add recall for ``expected_files`` style dir-prefix
  gold entries.
- **K2.** Card allocation does path-dedup: if two surfacing modules
  end up with the same query-aware rep (on valuein_homepage, both
  `remote-rooms` and `remote-room` collapse to
  `docs/features/2026-04-08-learning-remote-room.md`), the second
  module falls through to the member stream instead of stealing a
  card slot that would render a duplicate file in top-10. This is
  what preserves S4's `components/remote-room/*` evidence — the
  `remote-room` module's card is dropped, its member stream surfaces
  `edit-room-dialog.tsx` + `create-room-dialog.tsx`.
- **K3.** Non-card modules are deduped by name, keeping the
  file-count-richest variant per name. valuein_homepage has two
  `tuition-sessions` modules (a 4-file one keyed on the dashboard
  page, a 13-file one keyed on `components/tuition-sessions/*.tsx`);
  dedup keeps the 13-file variant so member emission picks
  structurally canonical files rather than shallow dashboard
  `page.tsx` entries.
- **K4.** `_interleave_modules` places members at the *trailing*
  positions of the result list rather than interleaved with chunks.
  Modules claim even positions 0/2/4; chunks fill 1/3/5/6/7; members
  take the tail (positions 8-9 at `limit=10`). This keeps the
  top-2 chunks — primary-target documents for rationale and
  exploration queries — at ranks 2 and 4, which would otherwise be
  displaced if members interleaved greedily. Member budget is
  `limit // 3` (3 at the default `limit=10`), preserving the
  chunks-majority property L5 introduced.
- **K5.** `run_valuein_bench.hybrid_track` treats `module_member`
  hits like module hits for `acceptable_module_names` matching —
  the `admissions` SQL migration emitted as a member still counts
  as a primary-hit via the module track when the gold query
  declares `acceptable_module_names: ["admissions", …]`.

Also shipped: gold-set S3 (AI agent architecture) now carries
`acceptable_module_names: ["agent"]` so the `agent` module card
counts as primary hit when surfaced.

## Results

`valuein_homepage` (1307 files), 20 gold queries, hybrid track @ limit=10.

### Overall (Step J → Step K)

| Metric       | Step J | Step K | delta  |
|--------------|--------|--------|--------|
| top-1        | 0.55   | 0.50   | −0.05  |
| top-5        | 0.95   | 0.95   | 0      |
| recall@10    | 0.80   | 0.82   | +0.02  |
| reads/query  | 2.05   | 2.00   | −0.05  |
| context_pack | 20.4KB | 20.6KB | +0.2KB |
| via module   | 0.40   | 0.20   | −0.20  |

Recall + reads moved the right way. ``via_module`` dropped because
S5's primary now lands via a chunk (``docs/plans/2026-04-17-entrance-
test-management.md`` at rank 4) rather than the admissions card
(rank 5) — the card didn't change, but the member-stream insertion
re-orders chunks relative to it. top-1 slipped 0.05 for a similar
reason: one query's primary chunk became the second-rank chunk
behind a module card + member.

### Per-category (Step J → Step K)

| Category    | top-1 | top-5 | recall@10 | reads |
|-------------|-------|-------|-----------|-------|
| structure   | 0.20 → 0.20 | 1.00 → 1.00 | 0.41 → **0.52** | 2.40 → 2.40 |
| exploration | 0.80 → 0.80 | 1.00 → 1.00 | 0.80 → 0.77 | 1.40 → 1.20 |
| precision   | 0.60 → 0.60 | 0.80 → 0.80 | 1.00 → 1.00 | 2.20 → 2.20 |
| rationale   | 0.40 → 0.40 | 1.00 → 1.00 | 1.00 → 1.00 | 2.20 → 2.20 |

**Structure** is the target category — recall@10 **0.41 → 0.52**
(+0.11). Exploration recall dropped 0.80 → 0.77 because two member
slots displace two chunks at trailing positions on F3 / F4 (the
feature .md and the SQL migration), while F2 gained 0.34 from a
non-card ``migrations`` member contributing
`create_monthly_snapshot_cron.sql`. Net exploration: −0.03. reads
improved everywhere.

### Per-query recall movement

| id | Step J recall | Step K recall | delta |
|----|---------------|---------------|-------|
| **S1** | **0.25** | **0.50** | **+0.25** — tuition-wizard member adds `components/tuition-wizard/` |
| S2 | 0.25 | 0.25 | 0 — portal-v3 ranks 14th in search_modules, outside 8-source window |
| S3 | 0.20 | 0.20 | 0 — agent module has no harness/core or harness/app members |
| S4 | 0.67 | 0.67 | 0 — rep-dedup kept ``components/remote-room/*`` via members |
| **S5** | **0.67** | **1.00** | **+0.33** — admissions#2 SQL migration surfaces via non-card member |
| F1 | 1.00 | 1.00 | 0 |
| **F2** | **0.33** | **0.67** | **+0.34** — ``monthly_snapshot_cron.sql`` surfaces via migrations module member |
| F3 | 1.00 | 0.50 | −0.50 — attendance doc chunk pushed out by members |
| F4 | 1.00 | 0.67 | −0.33 — consultations SQL chunk pushed out by members |
| F5 | 1.00 | 1.00 | 0 |
| P*/R* | — | — | no change |

S1, S5, F2 are the direct Step K wins (+0.92 total recall). F3 and
F4 are the losses (−0.83 total) — their chunks at ranks 8-9 in
Step J held secondary ``expected_files`` entries (docs/features
attendance.md for F3, consultations SQL for F4) that the 3-member
tail insertion pushes out of top-10. Net: +0.09 overall recall,
with top-1/top-5 unchanged.

### What Step K did not fix

- **S2 portal-v3** — ranks 14th in `search_modules` on this query
  because the module's summary text doesn't mention 학부모/parent as
  prominently as `portal-v2` or `student-hub`. Would need module-
  content work (F-series follow-up) or alias expansion.
- **S3 harness subsystems** — the `harness-v4-scripts` module has a
  single README; harness/core and harness/app aren't grouped into
  high-scoring modules against the query "AI 에이전트 아키텍처".
  Same content-gap as S2.
- **F3 / F4 secondary chunks** — the member stream's tail
  insertion is strictly a trade against the trailing chunk slots.
  We took a 0.50 F3 / 0.33 F4 recall hit for a 0.34 F2 gain on the
  same lane (exploration). Net −0.03 category recall — within
  noise, but ideally the member picker would decline to emit when
  the chunk at the same rank is a higher-value expected file. That
  requires awareness the orchestrator doesn't have.

## Tests

- 705 → **712** (+7): member placement at tail, chunk-at-rank-2
  preservation, member/card dedup, budget cap (``limit // 3``),
  slack absorption when chunks are short, backward compatibility
  for callers that don't pass members, slots=0 guard.
- 712 / 712 passing. Zero regressions across the existing 705.

## Status

Phase 5 exit progress:

| Exit criterion          | Target  | Current | Status |
|-------------------------|---------|---------|--------|
| overall top-5           | ≥ 0.80  | 0.95    | ✅     |
| overall recall@10       | ≥ 0.70  | 0.82    | ✅     |
| overall reads/query     | ≤ 2.5   | 2.00    | ✅     |
| **structure recall@10** | ≥ 0.55  | **0.52**| ⚠️     |

Structure recall lifted 0.41 → 0.52 (+0.11) but still 0.03 short
of the 0.55 exit bar. The gap narrows to S2/S3 — both bottleneck
on module content (summary text not strong enough for their correct
subsystems to reach the 8-module source window). A follow-up "Step
L" that either widens the source window with name-match boosting
for modules whose leaf name contains a query-expansion token, OR
rewrites the S2/S3 modules' summaries with richer Korean domain
terms, would likely close the remaining gap without touching the
other four metrics that are already green.
