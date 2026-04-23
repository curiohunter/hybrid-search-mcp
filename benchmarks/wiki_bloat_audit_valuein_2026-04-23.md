# Wiki bloat audit — valuein_homepage

- Total pages: **2584**

## Category breakdown

| category | count | % |
|---|---:|---:|
| healthy | 2469 | 95.5% |
| partial_stale | 2 | 0.1% |
| zombie | 48 | 1.9% |
| empty | 65 | 2.5% |

**Post-cleanup projection**: 2534 healthy pages (≈ 1% reduction from zombie + partial_stale removal).

## Isolation-suffix depth distribution

Counts pages by number of trailing ``-isolated`` tokens (collision markers).

| depth | count | meaning |
|---:|---:|---|
| 0 | 383 | no collision marker (clean name) |
| 1 | 1549 | one collision level |
| 2 | 571 | collision-of-collision |
| 3 | 81 | triple collision |

## Biggest base-name clusters (≥3 variants), top 20

| base | variants |
|---|---:|
| `route` | 180 |
| `page` | 114 |
| `index` | 44 |
| `_hooks` | 31 |
| `route-isolated` | 29 |
| `page-isolated` | 25 |
| `hooks` | 23 |
| `_components` | 22 |
| `services` | 21 |
| `index-isolated` | 14 |
| `layout` | 13 |
| `portal` | 13 |
| `analytics` | 11 |
| `actions` | 10 |
| `tuition-wizard` | 10 |
| `ui` | 10 |
| `_components-isolated` | 9 |
| `calendar` | 9 |
| `ledger` | 9 |
| `lib` | 9 |

## Zombie examples (random 15)

Pages where every referenced source file has been deleted.

- `01_기출-isolated-1.md` → 3 dead refs
  - first ref: `docs/valueinmath_docs/학습/대원여고_고2-1중간/01_기출/2023_중간고사_parsed.json`
- `01_기출-isolated-2.md` → 3 dead refs
  - first ref: `docs/valueinmath_docs/학습/광남고_고1-1중간/01_기출/2023_중간고사_parsed.json`
- `02_교과서-isolated.md` → 3 dead refs
  - first ref: `docs/valueinmath_docs/학습/대원여고_고2-1중간/02_교과서/천재교육_대수_삼각함수_1_parsed.json`
- `03_대원고_참고-isolated.md` → 2 dead refs
  - first ref: `docs/valueinmath_docs/학습/대원여고_고2-1중간/03_대원고_참고/2024_대원고_중간고사_parsed.json`
- `2026-04-07-vala-21-stitch-workflow-isolated.md` → 1 dead refs
  - first ref: `notes/projects/2026-04-07-VALA-21-stitch-workflow.md`
- `2026_재출제_예상_분석-isolated-1.md` → 1 dead refs
  - first ref: `docs/valueinmath_docs/학습/광남고_고1-1중간/04_예상분석/2026_재출제_예상_분석.md`
- `2026_재출제_예상_분석-isolated-2.md` → 1 dead refs
  - first ref: `docs/valueinmath_docs/학습/대원여고_고2-1중간/04_예상분석/2026_재출제_예상_분석.md`
- `22-104510-2da65337-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa.backup-1776860478/2026/04/22-104510-2da65337.md`
- `22-122202-28ca1d3d-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122202-28ca1d3d.md`
- `22-122202-6695f1bd-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122202-6695f1bd.md`
- `22-122203-f40e30fc-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122203-f40e30fc.md`
- `22-122204-60e91ecf-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122204-60e91ecf.md`
- `22-122204-ced3b90b-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122204-ced3b90b.md`
- `22-122204-e7b92049-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122204-e7b92049.md`
- `22-122206-17dcc18b-isolated.md` → 1 dead refs
  - first ref: `.hybrid-search/qa/2026/04/22-122206-17dcc18b.md`

## Partial-stale examples (random 10)

Pages where some referenced files exist, others are gone.

- `dashboard-isolated-1.md` → 10 alive / 1 dead
- `portal-3.md` → 1 alive / 1 dead

## Action recommendations

1. **Zombie pages** — safe to bulk-delete on first cleanup pass.
2. **Partial-stale** — regenerate (drop dead refs, keep live ones).
3. **Collision suffixes (depth ≥ 2)** — rename using content hash;
   collapse duplicates that point at the same set of files.
4. Wire this logic into the reindex pipeline so the cleanup
   happens automatically on every commit going forward.
