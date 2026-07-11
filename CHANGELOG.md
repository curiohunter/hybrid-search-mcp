# Changelog

All notable changes to hybrid-search-mcp. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions are [SemVer](https://semver.org/spec/v2.0.0.html).

## 0.7.1 — release-readiness audit

### Fixed

- **Plugin upgrade path**: bootstrap fast-path keyed on git commit SHA
  (content-hash fallback), not `pyproject.toml` diff — source-only
  changes now trigger reinstall. Revision lock written only after a
  fully successful install, so failures retry next session.
- **Bootstrap lock hygiene**: `.installing` always clears via
  EXIT/HUP/INT/TERM trap; stale locks (dead PID or >30 min) are
  reclaimed instead of wedging every future session; concurrent
  SessionStarts no longer double-install.
- **qa ordering**: global newest-first replaced by topic-aware
  supersession — recency reorders only same-topic Q&A groups; an old
  exact-topic answer is no longer displaced by a fresh adjacent-topic
  one.

### Added

- `teardown`: removes the MCP registration, hooks, and skills that
  `setup` installed (plugin uninstall does not run cleanup hooks).
  Ownership-checked: skills are deleted only when their content matches
  the install manifest SHA (a pre-existing user skill of the same name
  is backed up by setup and restored by teardown), and the MCP entry is
  removed only when it points at our server.
- Bench v2: full confidence distribution on present/absent probes
  (incl. strong_on_present), end-to-end latency p50/p95 (includes the
  confidence pipeline), derived embedding-call counts, and an
  adversarial recency track (old exact-topic vs fresh adjacent-topic)
  reported decomposed (both-found / exact-first-given-both).

### Changed (review follow-ups on the audit PR)

- Bootstrap lock acquisition is atomic (`mkdir`), closing the
  check-then-write race between concurrent SessionStarts; the non-git
  revision hash now covers skills/hooks/scripts/.claude-plugin too.
- Ambient memory-head selection is topic-grouped: newest-of-topic
  represents the group, groups rank by their best retrieval score — a
  fresh adjacent-topic Q&A can no longer take the guaranteed slot from
  an old exact-topic one at selection time.
- Corpus-absent confidence cap is skipped for memory-intent queries
  (history answers live in the lanes the source probe excludes) and for
  Korean queries over Hangul-free sources (cross-language matches leave
  no literal trace); the probe's sqlite connections are closed
  explicitly.

## Unreleased

### Added

- **Codex memory hooks**. New `codex-hook` entry handles Codex
  `SessionStart`, `UserPromptSubmit`, and `Stop` payloads. Codex receives
  hybrid-search context before exploratory prompts and completed turns are
  saved as qa logs with `trigger: codex_stop_hook` and `client: codex`.
- **Bounded answer excerpts for Stop-hook memory**. Claude and Codex completed
  turns now persist a sanitized, capped `## Answer excerpt` plus
  `answer_excerpt_chars`, improving memory quality without storing unbounded
  transcripts.
- **`install-codex-hook` CLI**. Writes Codex hooks to `.codex/hooks.json`
  or `~/.codex/hooks.json`, enables `[features].hooks = true`, and
  registers the MCP server using Codex TOML `[mcp_servers.hybrid-search]`.
- Shared hook runtime for Claude Code and Codex prompt classification,
  session context, prompt context, and completed-turn recording.
- Status now reports Codex hook/config presence and warns about
  `AGENTS.override.md` / near-limit `AGENTS.md` project docs.
- **Memory cards**. New `memory-card create/list/show/grep` commands promote
  qa logs into compact semantic memory under
  `.hybrid-search/memory/cards/`, indexed as `node_type="memory_card"`.
- **Memory compaction and graph-lite facts**. New `memory compact`,
  `memory procedural review`, and `memory facts export/list` commands create
  cards from qa logs, write reviewed procedural candidates, and export
  lightweight temporal facts to `.hybrid-search/memory/facts.jsonl`.
- **Router Phase 1 scanner noise filter**. Reindex now skips common content
  binaries/media/archives and oversized Markdown under content roots by
  default, with `[scanner.exclude]` overrides, `reindex --include-content`,
  and a doctor excluded-paths summary.
- **Router Phase 2 — quality signals**. Hybrid search responses now include
  `top_score`, `score_gap`, confidence bands, weak-result fallback hints, and
  a `recalibrate` CLI for project-specific thresholds.
- **Router Phase 3 — heuristic prompt router**. User prompt hooks now surface
  a bounded route suggestion for `hybrid_search`, `grep`, or `memory`, backed
  by a hand-labeled router benchmark.
- **In-flight dirty worktree visibility**. `hybrid_search(..., cwd=...)` now
  overlays tracked uncommitted file changes as ephemeral `[in-flight]` results,
  suppresses deleted dirty paths, and avoids persisting or embedding dirty
  content.
- `setup --dry-run`: preview CLAUDE.md/AGENTS.md changes without writing.
- `setup --force`: recover from a corrupted routing block.

### Changed

- qa log frontmatter accepts an optional `client` field. Existing records
  without it remain valid.
- Memory-aware ranking now boosts curated `memory_card` chunks above raw
  `qa_log` chunks for explicit recall queries.
- Codex hook installation now migrates the deprecated
  `[features].codex_hooks` flag to `[features].hooks`.
- CLAUDE.md and AGENTS.md routing sections now use versioned sentinel
  markers (`<!-- BEGIN/END hybrid-search-mcp routing v1 -->`). Existing
  installs migrate automatically on the next `setup`. Idempotent —
  re-running `setup` on a current install produces no diff.
- Routing block now includes a self-justify rule (one-sentence tool
  choice per call) and a weak-confidence fallback contract. G4 manual
  replay (`benchmarks/router_replay_2026-05.md`) showed these rules
  were already being followed via qa_log priming; the durable value is
  pinning the contract in CLAUDE.md so it survives qa_log churn.

## [0.4.0] — 2026-04-23

**Memory integrity.** v0.3.0 guaranteed every turn persists; v0.4.0 keeps
those persisted turns *useful* over time. Three deterministic passes run
at every reindex — staleness, semantic dedup, archive TTL — so qa_log
stops being an append-only dumpster and becomes actual memory that
consolidates.

### Added

- **Staleness pruning** (`memory.integrity.detect_stale_qa`). qa_log
  files whose ``## Top results`` paths are *all* absent from the store
  DB are archived (not unlinked). Mirrors the v0.3.0 wiki orphan
  detector.
- **Semantic dedup** (`memory.integrity.detect_semantic_duplicates`).
  For every pair of qa_log chunks, compares cosine similarity from the
  already-indexed vectors. Pairs at or above threshold
  (default **0.90**) cluster via union-find; the newest member is kept,
  older members are archived. No re-embedding, no LLM cost.
- **Archive tier**. Every prune (auto-prune, qa-prune, staleness,
  dedup) moves files into ``<project>/.hybrid-search/qa-archive/
  YYYY/MM/*.md`` instead of unlinking. Archive entries older than
  ``archive_ttl_days`` (default **30**) are permanently removed on
  subsequent reindexes — cheap insurance against regret.
- **`qa-restore` CLI**. Brings an archived entry back into qa/. Accepts
  the stem, hash prefix (≥4 hex chars), or friendly id from
  ``qa-list``. Path-preserving restore.
- **`integrity` CLI**. Runs the full pass on demand. `--dedup-threshold`
  lets users tune sensitivity without editing config.
- **`qa-stats` v2**. Surfaces ``active`` / ``archived`` / ``recent
  archive (7d)`` / ``total ever`` counters alongside the existing
  by-type / by-month breakdown. New ``by trigger`` line shows how many
  of the active qa files came from each save path (``mcp_tool``,
  ``stop_hook``, ``user_prompt_submit``).
- **`[memory.integrity]` config block** — ``enabled`` (default true),
  ``dedup_threshold`` (0.90), ``archive_ttl_days`` (30).
- **`VectorEngine.get_vector(chunk_id)`** — surfaces the stored HNSW
  vector to callers that need pairwise cosine without going through
  ``search()``. Used by the dedup pass.
- **Plan doc** `docs/plans/2026-04-23-v0.4.0-memory-integrity.md` with
  architecture, stages, verification gates, and risk register.
- Tests: 23 new in `test_memory_integrity.py` covering archive /
  staleness / dedup / purge / restore / stats. Full suite:
  **845 passing**.

### Changed

- Reindex tail order: `auto_prune` → `wiki_cleanup` → `integrity_pass`
  → `archive purge`. Independent passes; each skips silently when
  applicable sub-state is absent.
- `_ensure_gitignore_entries` now auto-patches ``.hybrid-search/
  qa-archive/`` so archived qa files never leak into git.

### Verification

Ran the full G-gate sweep:

- G1 (staleness): unit suite + single-file smoke → archived correctly
- G2 (dedup): live 3-identical-plants smoke → 1 kept, 2 archived
- G3 (archive TTL): 45-day-old mtime-forge → purged on next pass
- G4 (existing tests): 822 → 845 (+23, 100% pass)
- G5 (new tests): 23 in `test_memory_integrity.py` alone
- G6 (gitignore): ``.hybrid-search/qa-archive/`` auto-added on setup
- G_integrity (valuein live): no new stale or dedup pairs on clean
  run — environment already healthy post-v0.3.0 cleanup

### Non-goals (unchanged from plan)

- No LLM-based summarisation
- No cross-project qa consolidation
- Existing qa_log markdown format stays backward-compatible (additive
  frontmatter only; v0.2.x files still parse cleanly)

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
