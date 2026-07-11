# Compounding benchmark — valuein_homepage

- Date: 2026-07-11
- Pairs: 20  (non-leaky: 15, leaky: 5)
- Cold qa chunks: 0
- Warm qa chunks: 20  (+20)

## Track A: identity re-query (user asks the same question again)

Upper bound for memory recall — no wording variation.

## Identity (Q1a repeated)

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 70.00% | 95.00% | +25.00% |
| memory_primary_rate | 0.00% | 70.00% | +70.00% |
| primary_hit_rate | 70.00% | 65.00% | -5.00% |
| primary_top1 | 45.00% | 35.00% | -10.00% |
| primary_top5 | 70.00% | 65.00% | -5.00% |
| recall_at_10_mean | 0.542 | 0.500 | -0.042 |
| mrr_mean | 0.531 | 0.462 | -0.069 |

## Track B: paraphrased follow-up (same topic, different wording)

Realistic follow-up scenario. Measures whether the memory boost can surface a past Q&A when the new query keeps the principal noun phrases but rewords the rest.

## Paraphrase — overall

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 65.00% | 90.00% | +25.00% |
| memory_primary_rate | 0.00% | 70.00% | +70.00% |
| primary_hit_rate | 65.00% | 60.00% | -5.00% |
| primary_top1 | 40.00% | 30.00% | -10.00% |
| primary_top5 | 60.00% | 55.00% | -5.00% |
| recall_at_10_mean | 0.433 | 0.450 | 0.017 |
| mrr_mean | 0.469 | 0.377 | -0.092 |

## Paraphrase — non-leaky subset

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 60.00% | 86.67% | +26.67% |
| memory_primary_rate | 0.00% | 60.00% | +60.00% |
| primary_hit_rate | 60.00% | 53.33% | -6.67% |
| primary_top1 | 33.33% | 33.33% | +0.00% |
| primary_top5 | 53.33% | 46.67% | -6.67% |
| recall_at_10_mean | 0.311 | 0.333 | 0.022 |
| mrr_mean | 0.392 | 0.336 | -0.056 |

## Paraphrase — leaky subset (transparency)

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 80.00% | 100.00% | +20.00% |
| memory_primary_rate | 0.00% | 100.00% | +100.00% |
| primary_hit_rate | 80.00% | 80.00% | +0.00% |
| primary_top1 | 60.00% | 20.00% | -40.00% |
| primary_top5 | 80.00% | 80.00% | +0.00% |
| recall_at_10_mean | 0.800 | 0.800 | 0.000 |
| mrr_mean | 0.700 | 0.500 | -0.200 |

## Per-pair details

| id | leakage | cold rank | warm rank | Δ rank | cold R@10 | warm R@10 | qa top-10 |
|---|---|---:|---:|---:|---:|---:|---:|
| S1 | low | 1 | 1 | +0 | 0.25 | 0.25 | 1 |
| S2 | low | 3 | 5 | -2 | 0.25 | 0.25 | 1 |
| S3 | low | — | — |  | 0.00 | 0.00 | 1 |
| S4 | low | 7 | 3 | +4 | 0.33 | 0.33 | 1 |
| S5 | low | 5 | — | lost | 0.33 | 0.00 | 0 |
| F1 | low | 1 | 1 | +0 | 1.00 | 1.00 | 1 |
| F2 | low | 1 | 1 | +0 | 0.67 | 0.67 | 1 |
| F3 | low | 1 | 1 | +0 | 0.50 | 0.50 | 2 |
| F4 | low | 1 | 1 | +0 | 0.33 | 1.00 | 0 |
| F5 | low | 5 | 6 | -1 | 1.00 | 1.00 | 1 |
| P1 | high | — | — |  | 0.00 | 0.00 | 1 |
| P2 | high | 2 | 2 | +0 | 1.00 | 1.00 | 1 |
| P3 | high | 1 | 1 | +0 | 1.00 | 1.00 | 0 |
| P4 | high | 1 | 2 | -1 | 1.00 | 1.00 | 2 |
| P5 | high | 1 | 2 | -1 | 1.00 | 1.00 | 1 |
| R1 | low | — | — |  | 0.00 | 0.00 | 1 |
| R2 | low | — | — |  | 0.00 | 0.00 | 1 |
| R3 | low | — | — |  | 0.00 | 0.00 | 0 |
| R4 | low | — | — |  | 0.00 | 0.00 | 1 |
| R5 | low | — | — |  | 0.00 | 0.00 | 0 |
