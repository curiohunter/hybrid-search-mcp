# Changelog

All notable changes to hybrid-search-mcp. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions are [SemVer](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-04-23

**Deterministic Memory Layer.** v0.2.0 left two leaks: qa_log save only
fires when Claude chooses the MCP tool (stochastic), and wiki pages
accumulate orphans as source files disappear from the index
(gitignore drift, deletions). v0.3.0 closes both with two new hooks and
automated wiki cleanup — every user–Claude turn persists regardless of
tool choice, and every reindex purges orphan wiki pages.

### Added

- **`Stop` hook** (`hybrid_search.hooks._handle_stop`). Fires at the end
  of every Claude turn; parses the transcript JSONL to find the last
  user prompt + assistant activity, writes a qa_log entry tagged
  ``trigger: stop_hook``. Respects ``stop_hook_active`` to avoid
  continuation loops. Dedups against recent MCP-tool saves (5-second
  window + query hash match) so turns that DID call
  ``hybrid_search`` aren't double-persisted.
- **`UserPromptSubmit` hook** (`_handle_user_prompt_submit`). Fires when
  the user submits a prompt; classifies as exploratory via KO/EN
  keyword heuristics (`어떤`/`어떻게`/`구조`/`how`/`explain`/...) plus
  memory-intent markers (`지난번`/`previously`/...) that bypass the
  length gate. Exploratory prompts trigger an on-the-fly
  ``hybrid_search`` call; top-10 results are injected as
  ``hookSpecificOutput.additionalContext`` and saved to qa_log with
  ``trigger: user_prompt_submit``. Slash commands, bash pass-through,
  `@file` references, and short single-token prompts short-circuit
  before any search.
- **Orphan wiki auto-cleanup** (`hybrid_search.wiki_cleanup`). At the
  end of every reindex, compares each wiki page's ``## Files`` refs
  against the store DB's ``relative_path`` set; pages whose references
  are *all* absent from the DB are deleted. Protects against gitignore
  drift (files excluded from the index but still on disk) and actual
  file deletion. Available as a standalone CLI via
  ``hybrid-search-mcp wiki-cleanup [--dry-run] [--verbose]``.
- **qa_log format v2**: optional frontmatter fields ``trigger``,
  ``tools_used``, ``answer_chars``. Backward-compatible — v0.2.x
  records (without these fields) still parse cleanly.
- **Stronger `CLAUDE.md` routing section**: imperative wording
  ("반드시 이 순서로", "예외 없이") plus explicit
  ``mcp__hybrid-search__hybrid_search`` tool name and trigger-word
  tables per question category. Replaces the descriptive v0.2.x
  "의도 기반 라우팅" section on re-setup.
- **`install-memory-hook` upgrade**: now installs all four hook types
  (PreToolUse, SessionStart, UserPromptSubmit, Stop). Existing
  installs with stale paths are refreshed in place. Idempotent.
- **`scripts/wiki_bloat_audit.py`** gains a `--delete` flag with a
  DB-based orphan detector (previously disk-only, which undercounted
  gitignore-drift zombies by an order of magnitude).
- **Plan doc** at `docs/plans/v0.3.0-deterministic-memory.md` with
  goals, design, stages, verification checklist, risk register.
- Tests: 32 new (`test_memory_hook.py` +TestStopHook +TestUserPromptSubmitHook
  +TestExploratoryClassifier, `test_wiki_orphan_cleanup.py`).
  Full suite: **822 passing**.

### Changed

- `qa_log.record()` accepts a ``trigger`` argument (default
  ``"mcp_tool"`` preserves prior behaviour). Adds ``record_turn()``
  for non-MCP saves.
- `install_memory_hook()` returns ``{added, updated, path, status}``
  — ``updated`` counts stale-path refreshes that were rewritten in
  place. CLI output distinguishes installed vs refreshed blocks.

### Fixed

- **valuein_homepage cleanup**: 765 orphan wiki pages removed
  (2,584 → 1,819 pages, ≈30% reduction). Almost all were
  ``mindvault-out/*`` or ``docs/valueinmath_docs/학습/*`` pages that
  had been excluded from the index by `.gitignore` additions but were
  never pruned.
- Stop hook's transcript parser skips local-command stdout,
  command-message envelopes, and system-reminder blocks — only
  genuine user prompts survive the filter.

### Notes

- The bench (`benchmarks/run_compounding_bench.py`) drives the
  orchestrator directly, so it does *not* exercise the v0.3.0
  Stop / UserPromptSubmit guarantees (those only fire inside Claude
  Code). Re-run numbers post-cleanup show Track A identity memory
  surface unchanged at 80 %; Track B paraphrase dropped from 65 % to
  50 % in this environment, attributable to the 30 % fewer wiki
  chunks now in the index. The guaranteed-save property is verified
  by unit tests (``TestStopHook`` suite) rather than the compounding
  bench.
- Non-goals preserved: no MCP tools added; existing qa_log files
  remain readable; module_synth page-generation algorithm untouched.

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
