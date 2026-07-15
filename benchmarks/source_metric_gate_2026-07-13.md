# Source-metric gate — 2026-07-13 (SUPERSEDED — see 2026-07-14)

> **Correction (2026-07-14):** this document compared against the
> previous day's artifact rather than a same-day clean base, and the
> "every metric is identical" claim only covered the five gate metrics —
> recall@10 in fact moved (+1.7–2.2pp, an improvement). The runs also
> executed with a then-uncommitted vector.py fix in the working tree
> (now commit `fe0f9a2`). The valid gate is
> `source_metric_gate_2026-07-14.md`: a same-day clean-tree A/B that
> confirms PASS and attributes every delta. Kept for the audit trail.

Baseline: main head `ae26c94` values (source_metric_gate_2026-07-12.md
head column, corpus valuein_homepage @ `d65e4fa`, 20 pairs).
Head: `909b2b3` (feature/language-general-supersession), same corpus SHA,
same command: `python benchmarks/run_compounding_bench.py`.

| metric | track | baseline (07-12 head) | this PR | delta |
|---|---|---:|---:|---:|
| answer_found_rate | identity | 0.950 | 0.950 | +0.000 |
| primary_top1 | identity | 0.350 | 0.350 | +0.000 |
| primary_top5 | identity | 0.650 | 0.650 | +0.000 |
| mrr_mean | identity | 0.462 | 0.462 | −0.000 |
| memory_primary_rate | identity | 0.800 | 0.800 | +0.000 |
| answer_found_rate | paraphrase | 0.900 | 0.900 | +0.000 |
| primary_top1 | paraphrase | 0.350 | 0.350 | +0.000 |
| primary_top5 | paraphrase | 0.600 | 0.600 | +0.000 |
| mrr_mean | paraphrase | 0.437 | 0.437 | −0.000 |
| memory_primary_rate | paraphrase | 0.850 | 0.850 | +0.000 |

**Gate (warm primary_top5 drop ≤ 2pp, mrr_mean drop ≤ 0.02): PASS — every
metric is identical to the fourth decimal.** Expected by construction: the
topic matcher only reorders qa_log entries *within* their own slots and
never touches code/doc ranking, lane composition, or confidence.

## Latency

End-to-end p50 is the stable signal; p95 at n=44 queries is dominated by
1–2 embedding-API outliers and swings run-to-run on identical code:

| run | code | p50 | p95 |
|---|---|---:|---:|
| valuein 07-12 | main | 567 ms | 863 ms |
| valuein 07-13 | this PR | 573 ms (+1%) | 717 ms (−17%) |
| httpx 07-13 #1 | main | 348 ms | 412 ms |
| httpx 07-13 #2 | this PR | 365 ms (+5%) | 539 ms |
| httpx 07-13 #3 | this PR (re-measure) | 358 ms (+3%) | 853 ms |

Matcher CPU cost measured directly: `topic_group_indices` over 10 qa
candidates ≈ 0.3 ms, tokenization ≈ 1.3 ms — ~1.6 ms worst case per
search, two orders of magnitude below the p95 swing. p50 gate (+≤15%):
**PASS** at +1–5%; p95 is not a meaningful gate at this n.
