# Source-metric gate — 2026-07-14 (clean same-day A/B)

This supersedes `source_metric_gate_2026-07-13.md`, which had two
defects the PR review caught: (1) it compared against the previous
day's artifact instead of re-measuring the base the same day, and
(2) it claimed "every metric identical" while only checking the five
gate metrics — recall@10 had in fact moved.

Setup: **both sides measured 2026-07-14 from clean trees.**
Base = git worktree at `ae26c94` (main), head = PR branch working tree
(clean; the previously-uncommitted vector.py f32/lock fix is now commit
`fe0f9a2`, so head SHA fully describes the executed code). Base code
forced via `PYTHONPATH=<worktree>/src` for the bench process and its
reindex subprocesses. Corpus: valuein_homepage @ `d65e4fa`, 20 pairs,
`run_compounding_bench.py`. Raw JSON: `compounding_base_clean_2026-07-14.json`
/ `compounding_head_clean_2026-07-14.json`.

| metric | track | base (clean) | head (clean) | delta |
|---|---|---:|---:|---:|
| answer_found_rate | identity | 0.950 | 0.950 | +0.000 |
| primary_top1 | identity | 0.350 | 0.350 | +0.000 |
| primary_top5 | identity | 0.650 | 0.650 | +0.000 |
| mrr_mean | identity | 0.462 | 0.462 | +0.000 |
| recall_at_10_mean | identity | 0.483 | 0.500 | **+0.017** |
| memory_primary_rate | identity | 0.800 | 0.800 | +0.000 |
| answer_found_rate | paraphrase | 0.900 | 0.900 | +0.000 |
| primary_top1 | paraphrase | 0.350 | 0.350 | +0.000 |
| primary_top5 | paraphrase | 0.600 | 0.600 | +0.000 |
| mrr_mean | paraphrase | 0.403 | 0.403 | +0.000 |
| recall_at_10_mean | paraphrase | 0.433 | 0.450 | **+0.017** |
| memory_primary_rate | paraphrase | 0.850 | 0.850 | +0.000 |
| recall_at_10 (non-leaky warm) | paraphrase | 0.311 | 0.333 | **+0.022** |

**Gate (primary_top5 drop ≤ 2pp, mrr_mean drop ≤ 0.02): PASS.** The five
gate metrics are identical between clean base and clean head. Recall@10
moves +1.7–2.2pp — an improvement, present in the same-day A/B on both
the identity and paraphrase tracks, consistent with the supersession
reorder occasionally lifting a gold qa hit into the top-10 window.
(Note: paraphrase mrr_mean is 0.403 on BOTH sides today vs 0.437 in the
07-12 artifact — day-to-day corpus/embedding variance, which is exactly
why a same-day A/B, not a stale-artifact comparison, is the valid gate.)

## httpx headline, clean-tree reproduction (same day)

| side | newer_first | adversarial | absent s/m/w | present s/m/w |
|---|---:|---:|---:|---:|
| base `ae26c94` clean | 1/6 | 1/3 | 0/0/9 | 0/4/0 |
| head clean | **6/6** | 2/3 | 0/0/9 | 0/4/0 |

The 1/6 → 6/6 claim reproduces exactly from clean checkouts
(`memory_bench_v2_httpx_{base,head}_clean_2026-07-14.json`).

## ripgrep holdout, clean-tree reproduction (same day)

`memory_bench_v2_ripgrep_cleanrepro_2026-07-14.{json,md}` vs the
original 2026-07-13 run: **every update and adversarial row identical
down to the individual ranks** (9/11 newer-first, R1/R5 misses at the
same positions, adversarial 2/3, both_found 0/3), absent 9/9 weak
identical. One divergence: present control P4 ("유니코드 매칭은 어떻게
처리돼?", the Korean cross-language probe) read `mixed` in the original
run and `weak` here — a borderline confidence case flipping on
embedding nondeterminism, consistent with cross-language retrieval
already being a documented follow-up. All English present controls
stayed `mixed` in both runs.
