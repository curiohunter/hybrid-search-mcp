# Phase 5 Step F — Module content improvement

**Date:** 2026-04-22
**Changes:** Five edits to module discovery + synthesis aimed at the
"module content gap" that vector fusion couldn't close in Step C:

- **F1.** Cross-ref doc attachment: a doc whose path-mentions resolve to
  >1 module keys stays with its own docs/features module (the strict-merge
  rule is preserved) but is *also* attached to each mentioned target as a
  low-weight (0.2) member. Synthesis then sees the doc's chunks when it
  composes the target module's card.
- **F2.** `_compose_summary` absorbs related-doc section excerpts:
  the leading paragraph of up to two doc-section chunks gets folded into
  the module summary, alongside the existing best-code-docstring path.
  This is what gets Korean domain vocab (학부모, 월별, 입학) onto cards
  whose primary code identifiers are English.
- **F3.** Sub-threshold promotion: a size-1 code dir is kept as a module
  if either its single file is mentioned by some doc *or* the dir's leaf
  name appears as a token in any doc body. Closes the F2 gold-miss
  symptom (`components/analytics/` had one file and was being dropped).
- **F4** *(plumbing)*: the doc-merge UnionFind argument order was flipped
  so the **code** key wins as root after a strict merge — the merged
  module name is now `analytics` / `portal-v3` / `consultations` instead
  of inheriting the doc directory's name (`features`).
- **F5.** Name-prose cross-ref: when a doc body mentions a module's leaf
  name as a distinct token ≥ 2 times (and the doc isn't a project-level
  manifest like `DESIGN.md` / `CLAUDE.md` / `README.md`), the doc
  cross-refs onto that module. Closes S2 mode where
  `docs/features/2026-04-08-portal-parent-student.md` referenced
  `portal-v3` in prose alongside parenthesized paths
  (`app/(portal)/layout.tsx`) that the path-mention regex can't capture.

**Hypothesis:** S2 / S5 / F2 gold misses traced back to *module content*
(the right module exists or doesn't, but its card text doesn't match the
query) rather than retrieval ranking. Adding doc content into module
cards — both via cross-ref membership (F1, F5) and via summary
composition (F2) — should let token + cosine search find these modules
where Step C's vectors alone fell short.

## What landed

### Discovery (`index/modules.py`)
- `_extract_name_tokens` + `_count_name_tokens` — token-level analysis of
  doc bodies (lowercase alnum/hyphen, length ≥ 3) for F3 + F5.
- `cross_refs` map collected during the doc-mention pass, then resolved
  through union-find roots so cross-refs follow any merges.
- `resolved_crossrefs` capped at `_MAX_CROSSREFS_PER_MODULE = 3` per
  module so a HANDOFF that mentions twenty paths doesn't bloat every
  affected card.
- F5 name-prose cross-ref pass after directory/path collection,
  thresholded at `_MIN_NAME_MENTIONS = 2` and skipping
  `_GENERIC_META_DOCS = {claude.md, design.md, handoff.md, readme.md,
  contributing.md, changelog.md}`.
- F3 singleton promotion guarded by `mentioned_files` set ∪
  `doc_token_set`; promoted modules carry a `doc_promoted` signal flag.
- F4 union-find argument order flipped: `uf.union(only_key, doc_key)`.
- File-module weights as named constants: `_WEIGHT_CODE = 1.0`,
  `_WEIGHT_DOC_PRIMARY = 0.5`, `_WEIGHT_DOC_CROSSREF = 0.2`.
- `member_hash` folds cross-refs into its input so synth re-runs when
  the cross-ref set changes (otherwise F2's doc-excerpt pass would skip
  cards whose primary members didn't move).

### Synthesis (`index/module_synth.py`)
- `_collect_doc_excerpts` picks up to `_DOC_EXCERPT_TOP_N = 2` leading
  paragraphs from doc-section chunks (`node_type ∈ {section, block}`),
  capped at `_DOC_EXCERPT_MAX_CHARS = 320` each. `qa_log` chunks
  excluded — those are search-result artifacts, not feature docs.
- `_compose_summary` now emits a two-stream layout:

  ```
  Module `name` — Docs:
  <doc excerpt 1>
  ---
  <doc excerpt 2>

  Code: <best-code-docstring>

  Members: <file names>
  ```

  Pure-code modules degrade gracefully to `Code:` only; pure-doc
  modules to `Docs:` only; and the original `Members:` fallback still
  fires when neither stream has content.

## Real-world results

`valuein_homepage` (1307 files), 20 gold queries, hybrid track @ limit=10.

### Module discovery growth

| Metric                | Step C | Step F | delta |
|-----------------------|--------|--------|-------|
| total modules         | 161    | **302**| +141  |
| `doc_promoted` modules| n/a    | 141    | +141  |
| `crossref_doc` modules| n/a    | 76+    | new   |

The 141 promoted modules represent dirs that were dropped under the
≥ 2-files rule but had at least one doc-side signal supporting them
(direct path mention or leaf-name in doc prose). Examples surfaced for
the gold queries: `analytics`, `admissions`, `entrance-tests` (now
present as separate modules instead of being dropped or buried inside
`features`).

### Overall (Step C → Step F)

| Metric       | Step C | Step F | delta  |
|--------------|--------|--------|--------|
| top-1        | 0.45   | 0.45   | 0      |
| top-5        | 0.85   | 0.85   | 0      |
| recall@10    | 0.77   | **0.79** | **+0.02** |
| reads/query  | 2.55   | 2.65   | +0.10  |
| via module   | 0.25   | 0.25   | 0      |

Modest aggregate movement: recall up by 0.02, reads up by 0.10. The
recall lift comes from F4 consultations (one extra expected file now
makes it into top-10 thanks to the cross-ref doc attachment); the read
regression comes from S4 `remote-room` (rank 3 → 5) where the now-
discovered noise modules push the answer down.

### Per-category (Step C → Step F)

| Category     | top-1 | top-5 | recall@10 | reads | via_mod |
|--------------|-------|-------|-----------|-------|---------|
| structure    | 0.20 → 0.20 | 1.00 → 1.00 | 0.41 → 0.41 | 2.40 → 2.80 | 0.40 → 0.40 |
| exploration  | 0.60 → 0.60 | 0.80 → 0.80 | 0.67 → **0.73** | 3.20 → 3.20 | 0.60 → 0.60 |
| precision    | 0.60 → 0.60 | 0.80 → 0.80 | 1.00 → 1.00 | 2.20 → 2.20 | 0.00 → 0.00 |
| rationale    | 0.40 → 0.40 | 0.80 → 0.80 | 1.00 → 1.00 | 2.40 → 2.40 | 0.00 → 0.00 |

Exploration recall +0.06 is the headline mover, driven entirely by F4
consultations. Structure recall stays at 0.41 — still under the ≥ 0.55
plan-doc target — and reads ticked up 0.40 in that category.

### Per-query meaningful changes

| id | category    | metric | Step C | Step F | cause |
|----|-------------|--------|--------|--------|-------|
| F4 | exploration | recall | 0.50   | **0.67** | cross-ref doc attached to consultations module |
| S4 | structure   | rank   | 3      | 5      | promoted-module noise displaces target |

All other 18 queries unchanged in primary rank.

## What Step F did not fix

Three gold queries remain stuck:

- **S2** `학부모 학생 포털 인증 및 레이아웃 흐름` — primary at rank 2.
  F5 *did* attach `docs/features/2026-04-08-portal-parent-student.md` to
  the `portal-v3` module (verified via signals + member list), and F2
  *did* fold doc excerpts into the card, but the matched excerpts
  happened to be other docs about "tuition_ledger_entries" and
  "section-flags", not the parent-student content. The card grew but
  not in the right direction. **Module discovery is right; doc-excerpt
  selection isn't.**
- **S5** `입학 시험 결과 관리 모듈 구조` — primary at rank 4. The
  `entrance-tests` module is now split across two records (a 1-file
  promoted page module + the original 6-file code module). Neither lands
  in the top 5 because the matched chunks come from
  `school-exam-scores` / `school-exams` files, which are
  semantically near "시험 결과" without being the right subsystem.
  **Retrieval ranks closer-by-cosine over closer-by-name.**
- **F2** `월별 학원 통계는 어떻게 집계되나` — primary at rank None.
  The `analytics` module is now discovered (F3 promotion via "analytics"
  appearing in doc bodies), but it has only `page.tsx` as its sole
  file, and that file is a thin route stub with no monthly-stats
  content. The actual stats logic lives in
  `database/migrations/create_academy_monthly_stats.sql` — which is
  retrievable by symbol (P4 lands it at rank 1) but doesn't surface
  for the NL query because no module groups SQL migrations by feature.
  **The right answer's module doesn't exist as a coherent unit.**

These three failures all point to a tier of work *beyond* discovery
plumbing: doc-excerpt selection (which paragraph wins?), name-vs-cosine
rank balance, and module discovery for cross-tree subsystems
(SQL migrations + UI dashboards belonging to one feature).

## Tests

- 674 → **690** (+16): F1 (3) + F2 (6) + F3 (3) + F5 (3) + 1 misc.
  All passing. Zero regressions across the existing 674.

## Status

Step F shipped: discovery + synthesis content lift across five edits.
Most of the gain went into making the module *catalog* richer (302 vs
161 modules; cross-ref docs flowing into the right cards) rather than
into headline gold-set numbers. The headline numbers (top-5 0.85,
recall@10 0.79, reads 2.65) are within 0.05 of Step C and the structure
target (≥ 0.55 recall) is still open.

What this run rules out: the remaining structure/exploration gap is
**not** a discovery-plumbing problem. The modules now exist, the docs
are attached, the summaries reach for the right vocab — what fails is
which excerpt of the attached doc the synthesizer picks (F2 chose
"section-flags" over "parent-portal" for the portal-v3 card) and how
chunk-level retrieval ranks two semantically-close modules
(school-exams vs entrance-tests, page.tsx vs SQL migration) when the
query straddles them.

**Next levers** (post-Step-F, ordered by likely yield):

1. *Excerpt selection by query relevance* — pick the doc paragraph
   that lexically overlaps the most likely query, not the longest one.
   Requires a query-aware re-synthesis pass (or just a richer summary
   that emits 4–5 short excerpts instead of 2 long ones).
2. *Cross-tree module discovery* — group `database/migrations/*.sql`
   files into feature modules by name-token clustering or by matching
   migration names against existing module names (`academy_monthly_stats`
   → analytics module; `admission_results` → admissions module).
3. *Re-rank with module signals* — when two chunks tie, prefer the one
   whose file belongs to a module whose name matches a query token.
   Currently chunk ranking is module-blind.
