# Memory Layer v2 Benchmark — 2026-04-28

## Scope

- Project: `hybrid-search-mcp`
- Gold: `benchmarks/memory_gold.json`
- Corpus: local `.hybrid-search/qa/` compacted into `.hybrid-search/memory/cards/`
- Command: `.venv/bin/python benchmarks/run_valuein_bench.py --gold benchmarks/memory_gold.json --out benchmarks/memory_layer_v2_2026-04-28.json --limit 10`

## Corpus Prep

- `memory compact --cwd . --verbose`: 5 cards created from 5 candidate QA logs.
- `memory procedural review --cwd .`: procedural candidate file generated.
- `memory facts export --cwd .`: facts export generated.
- `reindex --cwd .`: indexed the generated memory card corpus.

## Result

| Metric | Hybrid | Grep |
| --- | ---: | ---: |
| Queries | 5 | 5 |
| primary_top1 | 1.00 | 0.00 |
| primary_top5 | 1.00 | 0.00 |
| recall@10 | 1.00 | 0.00 |
| memory_hit_rate@3 | 1.00 | 0.00 |
| card_vs_raw_ratio | 3.00 | 0.00 |
| read_count_estimate | 1.00 | 11.00 |
| context_pack_bytes | 3,836 | 0 |
| mean latency | 416 ms | 54 ms |

## Notes

- The first failed run showed `memory_card` chunks were indexed but absent from top results for recall queries.
- Fixes applied before the passing run:
  - expanded Korean memory-intent detection for recall wording such as `뭐였지`, `결정했지`, and `메모리`;
  - added an explicit memory lane for recall queries over `memory_card` and `qa_log`;
  - disabled module injection for recall queries so memory cards are not hidden by module cards;
  - widened filtered BM25 candidate depth so small filtered corpora like `memory_card` are searchable.
- `benchmarks/valuein_gold.json` now also carries the 5 memory gold queries with per-query `project` and `project_path` overrides. `run_valuein_bench.py` supports those overrides while retaining the original ValueIn gold set.

## Status

P6 is closed for the self-hosting memory benchmark: card corpus exists, the benchmark records `memory_hit_rate@3`, `card_vs_raw_ratio`, and `context_pack_bytes`, and all 5 memory gold queries hit a `memory_card` in rank 1.
