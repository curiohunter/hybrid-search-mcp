# Source-metric gate — 2026-07-11

Baseline `6842f5d` (pre-quality-changes worktree) vs head `5ac3a35` (`release-readiness-audit`).
Same corpus (valuein_homepage, 20 pairs), same day, `run_compounding_bench.py` both sides.

| metric | track | baseline | head | delta |
|---|---|---:|---:|---:|
| answer_found_rate | identity | 0.950 | 0.950 | +0.000 |
| primary_hit_rate | identity | 0.650 | 0.650 | +0.000 |
| primary_top1 | identity | 0.300 | 0.350 | +0.050 |
| primary_top5 | identity | 0.600 | 0.650 | +0.050 |
| mrr_mean | identity | 0.435 | 0.462 | +0.027 |
| recall_at_10_mean | identity | 0.500 | 0.500 | +0.000 |
| memory_primary_rate | identity | 0.750 | 0.700 | -0.050 |
| answer_found_rate | paraphrase | 0.900 | 0.900 | +0.000 |
| primary_hit_rate | paraphrase | 0.600 | 0.600 | +0.000 |
| primary_top1 | paraphrase | 0.300 | 0.300 | +0.000 |
| primary_top5 | paraphrase | 0.550 | 0.550 | +0.000 |
| mrr_mean | paraphrase | 0.377 | 0.377 | +0.000 |
| recall_at_10_mean | paraphrase | 0.475 | 0.450 | -0.025 |
| memory_primary_rate | paraphrase | 0.700 | 0.700 | +0.000 |

**Gate (warm primary_top5 and mrr_mean must not drop more than 2pp/0.02 vs baseline): PASS**

Known disclosed cost: paraphrase recall@10 dips (one top-10 slot goes to the ambient memory lane's guaranteed Q&A).
