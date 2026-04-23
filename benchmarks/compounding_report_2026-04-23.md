# Compounding benchmark — valuein_homepage

- Date: 2026-04-23
- Pairs: 20  (non-leaky: 15, leaky: 5)
- Cold qa chunks: 0
- Warm qa chunks: 20  (+20)

## Track A: identity re-query (user asks the same question again)

Upper bound for memory recall — no wording variation.

## Identity (Q1a repeated)

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 80.00% | 90.00% | +10.00% |
| memory_primary_rate | 0.00% | 80.00% | +80.00% |
| primary_hit_rate | 95.00% | 95.00% | +0.00% |
| primary_top1 | 30.00% | 20.00% | -10.00% |
| primary_top5 | 85.00% | 85.00% | +0.00% |
| recall_at_10_mean | 0.656 | 0.639 | -0.017 |
| mrr_mean | 0.459 | 0.375 | -0.084 |

## Track B: paraphrased follow-up (same topic, different wording)

Realistic follow-up scenario. Measures whether the memory boost can surface a past Q&A when the new query keeps the principal noun phrases but rewords the rest.

## Paraphrase — overall

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 75.00% | 85.00% | +10.00% |
| memory_primary_rate | 0.00% | 50.00% | +50.00% |
| primary_hit_rate | 80.00% | 85.00% | +5.00% |
| primary_top1 | 35.00% | 25.00% | -10.00% |
| primary_top5 | 70.00% | 75.00% | +5.00% |
| recall_at_10_mean | 0.654 | 0.596 | -0.058 |
| mrr_mean | 0.486 | 0.434 | -0.052 |

## Paraphrase — non-leaky subset

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 73.33% | 86.67% | +13.33% |
| memory_primary_rate | 0.00% | 46.67% | +46.67% |
| primary_hit_rate | 80.00% | 86.67% | +6.67% |
| primary_top1 | 26.67% | 26.67% | +0.00% |
| primary_top5 | 73.33% | 80.00% | +6.67% |
| recall_at_10_mean | 0.606 | 0.528 | -0.078 |
| mrr_mean | 0.439 | 0.436 | -0.003 |

## Paraphrase — leaky subset (transparency)

| metric | cold | warm | Δ |
|---|---:|---:|---:|
| answer_found_rate | 80.00% | 80.00% | +0.00% |
| memory_primary_rate | 0.00% | 60.00% | +60.00% |
| primary_hit_rate | 80.00% | 80.00% | +0.00% |
| primary_top1 | 60.00% | 20.00% | -40.00% |
| primary_top5 | 60.00% | 60.00% | +0.00% |
| recall_at_10_mean | 0.800 | 0.800 | 0.000 |
| mrr_mean | 0.629 | 0.429 | -0.200 |

## Per-pair details

| id | leakage | cold rank | warm rank | Δ rank | cold R@10 | warm R@10 | qa top-10 |
|---|---|---:|---:|---:|---:|---:|---:|
| S1 | low | 5 | 5 | +0 | 0.00 | 0.00 | 0 |
| S2 | low | 1 | 1 | +0 | 0.25 | 0.25 | 0 |
| S3 | low | — | 8 | new@8 | 0.00 | 0.00 | 0 |
| S4 | low | — | — |  | 0.00 | 0.00 | 0 |
| S5 | low | 4 | 4 | +0 | 0.67 | 0.67 | 0 |
| F1 | low | 3 | 3 | +0 | 1.00 | 0.50 | 0 |
| F2 | low | 1 | 1 | +0 | 0.67 | 0.33 | 0 |
| F3 | low | 3 | 3 | +0 | 0.50 | 0.50 | 0 |
| F4 | low | 6 | 5 | +1 | 1.00 | 0.67 | 0 |
| F5 | low | 1 | 1 | +0 | 1.00 | 1.00 | 0 |
| P1 | high | 7 | 7 | +0 | 1.00 | 1.00 | 0 |
| P2 | high | 1 | 2 | -1 | 1.00 | 1.00 | 1 |
| P3 | high | 1 | 1 | +0 | 1.00 | 1.00 | 0 |
| P4 | high | 1 | 2 | -1 | 1.00 | 1.00 | 1 |
| P5 | high | — | — |  | 0.00 | 0.00 | 0 |
| R1 | low | 1 | 1 | +0 | 1.00 | 1.00 | 1 |
| R2 | low | 4 | 4 | +0 | 1.00 | 1.00 | 0 |
| R3 | low | — | — |  | 0.00 | 0.00 | 0 |
| R4 | low | 3 | 4 | -1 | 1.00 | 1.00 | 1 |
| R5 | low | 3 | 3 | +0 | 1.00 | 1.00 | 0 |
