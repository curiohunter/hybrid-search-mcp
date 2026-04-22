# Changelog

All notable changes to hybrid-search-mcp. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions are [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-04-22

**Memory Layer, evidenced.** v0.1 introduced the qa_log compounding loop
but made no measurable claim. v0.2 closes that gap with a benchmark, disk
retention, and automatic memory consultation hooks — the three pieces that
turn "it works on my machine" into a shippable product.

### Added

- **Compounding benchmark** (`benchmarks/run_compounding_bench.py`,
  `benchmarks/compounding_pairs.json`). LongMemEval / LoCoMo-inspired
  session-separated Q1a → Q1b methodology with leakage split. Two tracks:
  identity re-query (upper bound) and paraphrased follow-up (realistic
  case). Reports memory surface rate, answer-found rate, gold recall@10
  regression guard, with cold/warm deltas per category. Safe to run on
  a live project — the script backs up and restores `.hybrid-search/qa/`
  around the experiment.
  - First run on valuein_homepage (20 pairs, 1,307 files):
    - Identity: answer_found **80% → 95%** (+15pp), memory surface
      **0% → 80%** (+80pp), gold recall@10 flat within noise (−1.7pp).
    - Paraphrase: answer_found **75% → 95%** (+20pp), memory surface
      **0% → 65%** (+65pp).
    - Non-leaky subset (15 pairs): answer_found **73% → 100%**.
- **Auto-prune on reindex** (`[memory]` config section, default on).
  journald-style two-ceiling retention (`retention_days=90`,
  `max_files=2000`). First activation is a dry-run — the user confirms
  via `qa-prune --confirm-first-run` before the policy actually deletes
  anything. Opt-out via `auto_prune=false` in `config.toml`.
  - New reader primitives: `reader.select_over_count`,
    `reader.prune_keep_latest`, `reader.auto_prune`.
- **PreToolUse + SessionStart memory hook** (`hybrid-search qa-hook`,
  `hybrid-search install-memory-hook`). Matches Graphify's pattern: a
  one-time SessionStart summary + per-Grep/Read contextual nudges, both
  returning the v2.1.9 `hookSpecificOutput.additionalContext` JSON so
  Claude sees the hint in-context before running the tool. Hooks are
  silent when they have nothing to say (noise patterns, no matches),
  capped at 800 chars per injection. `install-memory-hook` merges into
  `.claude/settings.local.json` non-destructively — existing hooks are
  preserved, re-running is idempotent.
- **`MemoryConfig`** in `hybrid_search.config` — `auto_prune`,
  `retention_days`, `max_files`, `require_first_run_confirm`.
- Default `config.toml` template now includes a `[memory]` block with
  inline documentation.
- Tests: 37 new (`test_memory_hook.py`, `test_qa_reader::TestPruneKeepLatest`,
  `::TestAutoPrune`). Full suite: **783 passing**.

### Changed

- `README.md` — hero claim ("search quality compounds with usage") now
  cites measured numbers instead of asserting. New sections:
  "Compounding benchmark (2026-04-22)", "Retention — Memory doesn't
  balloon your disk", "Automatic memory consultation — PreToolUse +
  SessionStart hooks". Package description in `pyproject.toml` updated
  to lead with "Memory Layer MCP for Claude Code".
- `cmd_reindex` gains a final-step call to `_run_auto_prune` (no-op when
  `[memory].auto_prune = false` or the qa dir is absent).

### Notes

- No breaking changes. Existing `.hybrid-search/qa/` directories are
  untouched on first reindex (dry-run gate); users must opt in once per
  project.
- Paraphrase track's non-leaky subset achieves 100% answer_found at the
  cost of a modest top-1 redistribution (gold code/doc sometimes drops
  one rank when a qa_log chunk surfaces). This is the expected trade-off
  for memory: the user gets both signals.

## [0.1.0] — 2026-04-21

Initial Memory Layer release. `qa_log` persistence default-on, 30-day
half-life decay, memory-intent boost (지난번에 / previously), secret
filter, per-project scoping. Details in `HANDOFF.md`.
