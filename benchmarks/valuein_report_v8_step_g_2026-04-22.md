# Phase 5 Step G — cross-tree file attachment

**Date:** 2026-04-22
**Changes:** Files living in generic bucket directories
(`database/migrations/`, `supabase/migrations/`, etc.) are
re-projected onto the feature-module catalog by filename-token match.
A migration like `create_academy_monthly_stats.sql` now also attaches
to the `stats` module at weight 0.3, not just the catch-all
`migrations` module.

**Hypothesis:** Step F's remaining F2 gold miss traced back to "the
right file exists but lives in a module whose card content is about
something else." If the stats-related SQL migration becomes a member
of the `stats` module, the module's card text (and its file listing)
should carry enough monthly-stats signal to rank for the F2 Korean
NL query.

## What landed

### Discovery (`index/modules.py`)
- `_crosstree_filename_tokens(rel_path)` — splits filename on
  `[-_]`, drops leading date prefixes (`\d{6,14}_?`), drops the
  `_CROSSTREE_STOPWORDS` set (create/update/delete/alter/…), filters
  length ≥ `_CROSSTREE_MIN_TOKEN_LEN = 3`.
- `_module_name_tokens(name)` — leaf-name tokens + naive singular
  fallback (`admissions` → `{admissions, admission}`) so SQL files
  like `admission_results.sql` reach plural-named modules.
- `_BUCKET_DIR_LEAVES = {migrations, seed, seeds, schema}` — list of
  dir leaves that trigger the cross-tree pass.
- Attachment pass runs after the main discovery loop. For each file
  in a bucket dir, finds all modules whose name-tokens intersect the
  filename tokens, picks the top-scoring one (overlap count,
  tie-break by module-name length = more specific wins), and emits
  a `file_modules` row with `_WEIGHT_CROSSTREE = 0.3`.
- Cap: `_MAX_CROSSTREE_PER_MODULE = 4` attachments per module.
- Touched modules carry the `crosstree_attached` signal and their
  `member_hash` folds the attachment set in so synth re-runs pick up
  the new members (the `_compose_summary` file-list grows to include
  the SQL filenames, which helps token-overlap match on follow-up
  queries).

## Real-world results

`valuein_homepage`, 20 gold queries, hybrid track @ limit=10.

### Attachments produced (sample)

| Module          | Cross-tree attachment                                                |
|-----------------|----------------------------------------------------------------------|
| `stats`         | `database/migrations/create_academy_monthly_stats.sql`               |
| `admissions`    | `database/migrations/20260327_create_admission_results.sql`          |
| `consultations` | `database/migrations/create_consultations_table.sql`                 |
| `entrance-tests`| `supabase/migrations/20260331_create_entrance_test_applications.sql` |
| `workspace`     | `database/migrations/20250827_create_workspace_tables.sql`           |
| `calendar`      | `database/migrations/add_google_calendar_id.sql`                     |
| `tuition-fees`  | `supabase/migrations/20260421130000_l2_1_tuition_fees_ledger…sql`    |
| `ledger`        | `supabase/migrations/20260421115956_ledger_phase_l1_enum…sql`        |
| `cron`          | `database/migrations/create_monthly_snapshot_cron.sql`               |
| `mathflat-homework` | `supabase/migrations/20260130_mathflat_homework_cron.sql`        |
| `pending-approval` | `database/migrations/create_pending_registrations.sql`            |
| `pending-users` | `supabase/migrations/20260227_cleanup_stale_users_cron.sql`          |
| `01-core`       | `supabase/migrations/20260421120042_ledger_phase_l1_core.sql`        |

13 modules with cross-tree attachments. The placement is correct:
migrations flow into the feature module named by their dominant token.

### Overall (Step F → Step G)

| Metric       | Step F | Step G | delta  |
|--------------|--------|--------|--------|
| top-1        | 0.45   | 0.45   | 0      |
| top-5        | 0.85   | 0.85   | 0      |
| recall@10    | 0.79   | 0.79   | 0      |
| reads/query  | 2.65   | 2.65   | 0      |

**Zero regression, zero headline movement.** The attachment catalog
is correct, but none of the 20 gold queries move in primary rank.

### Why F2 didn't close

The F2 query `월별 학원 통계는 어떻게 집계되나` should have landed the
`stats` module (which now holds `create_academy_monthly_stats.sql`).
Instead, running `search_modules` directly for this query shows:

```
월별 학원 통계는 어떻게 집계되나
  score=10.0  briefs      ← false positive from "학원" in body
  score=10.0  블로그        ← false positive from "학원" in body
  score=4.0   attendance  ← "학원" in body
  …
  (stats not in top 10)
```

But running the same query WITHOUT the trailing particle `는`:

```
월별 학원 통계
  score=14.0  stats       ← 10 name + 4 body ✓
  score=10.0  briefs
  …
```

The `stats` module has a clean name-hit on `통계 → stats`. But
because the NL query spells it `통계는` (with the topic particle 는),
and the alias map only has `통계` as a key, the lookup misses. The
module drops off the candidate list entirely.

**This means F2's remaining gap is a Korean-NLP problem, not a
catalog problem.** Step G's attachment is correct; it's waiting on
a particle-aware tokenizer to be usable.

### Attempted — and reverted — particle stripping

A naive fix (strip common 조사 on Korean tokens before alias lookup)
regressed the suite:

| Metric       | G + strip | delta vs G |
|--------------|-----------|------------|
| top-5        | 0.80      | −0.05      |
| recall@10    | 0.74      | −0.05      |
| reads/query  | 3.10      | +0.45      |

The failure mode: stripping is broad. `학생이 → 학생 → student` is
technically correct, but every Korean NL query has a subject noun
like "학생". Injecting `student` as an alias then promotes
student-adjacent modules for queries that are topically about
something else (F1 homework-analysis dropped from rank 1 → rank 10;
F4 consultations recall 0.67 → 0.33).

The strip rule as written doesn't distinguish specific domain nouns
(통계, 출결, 원격) from generic subject nouns (학생, 교사, 수업). A
safer version would need either (a) a rarity-weighted alias boost,
(b) a blacklist of "too-generic" stems, or (c) real morphological
analysis. All three are work beyond Step G's scope, so the strip was
removed and committed as a separate decision.

Tests for the particle-strip helper (`_strip_korean_particle` +
`test_expand_with_aliases_hits_particle_stripped_form`) were also
removed as part of the revert.

## Tests

- 690 → **694** (+4): all Step G discovery tests.
  `test_crosstree_sql_attaches_to_feature_module`,
  `test_crosstree_attach_respects_singular_plural`,
  `test_crosstree_attach_skips_stopword_only_filenames`,
  `test_crosstree_attach_caps_per_module`.
- 694/694 passing, zero regressions across the existing suite.

## Status

Step G ships cross-tree attachment correctly but doesn't improve
gold numbers. The remaining F2 gap is gated on Korean-NLP tooling
(particle-aware tokenization) that has to be done without the
collateral damage seen in the quick strip attempt. Real levers
remaining:

1. **Particle-aware tokenizer with rarity-weighted expansion** —
   only inject an alias if the stem is specific enough (e.g., appears
   in ≤ 3 module names, or has a single unique alias in the map).
2. **Query-aware summary excerpts** — F2's `_collect_doc_excerpts`
   picks the longest section; picking the section that lexically
   overlaps the query would let the portal-v3 card surface the
   parent-student paragraph rather than the section-flags one (also
   relevant to S2 / S5).
3. **Chunk re-rank with module signals** — when two chunks tie, prefer
   the one whose file is a primary member (not cross-tree) of a
   module whose name matches a query token. Reduces the SQL-file-at-
   rank-6 pattern seen in P1.

None of these three are implemented in Step G.
