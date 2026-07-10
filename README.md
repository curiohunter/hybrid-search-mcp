# Hybrid Search MCP

[![tests](https://github.com/curiohunter/hybrid-search-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/curiohunter/hybrid-search-mcp/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

**One memory. Claude Code or Codex — doesn't matter.**
Questions you ask in one agent become context the other agent sees tomorrow.
Auto-save, auto-recall, Korean + English.

Hybrid BM25 + Vector + Memory Layer. Cross-language (Korean ↔ English)
across code, docs, and your own past exchanges.

```
Day 1 (Claude Code): "portal-v3 인증 흐름이 어떻게 되지?"
                     → answers from code, saves Q&A to .hybrid-search/qa/

Day 2 (Codex):       "portal 인증 어디서 처리해?"
                     → pre-fetch surfaces Day 1's exchange before Codex even searches
```

The Day-2 turn didn't re-search the code fresh — yesterday's Claude Code
answer surfaced as context inside Codex. Every answered query becomes a
first-class search result for every future query, in either agent.

**Trade-offs you should know up-front** (we've measured them):
- First-query latency adds **~400 ms** of pre-fetch overhead (vs ~50 ms `grep`). Worth it for exploratory questions; not for `grep`-shaped lookups (and the router knows the difference).
- Embedder = **OpenAI `text-embedding-3-small`** (API key required). **No local embedding backend — by choice, not neglect.** The first version ran local models; bulk-embedding tens of thousands of chunks pinned an M3 MacBook's fans for the entire run and made the machine unusable (CPU path was no better). A full reindex of a 2,000-file project via the API costs cents. If zero-API-key is a hard constraint, this tool isn't for you today; a `backend` config field is reserved and a local ONNX contribution is welcome.
- "0-config" is *almost* true: one `pip install` + one `setup` command after, but you also need `OPENAI_API_KEY` and (for Codex) a separate `install-codex-hook`.

**Who this is for:** 1인 개발자가 Claude Code를 주력으로 + 가끔 Codex도 쓰면서, 같은 코드베이스에서 반복 질문을 줄이고 싶은 사람. Korean + English 코드베이스에서 검증됨 (valuein_homepage 708-commit, 1,307 files).

**v0.3.0 (2026-04-23): deterministic guarantees.** Four Claude Code hooks
(PreToolUse, SessionStart, UserPromptSubmit, Stop) wire the memory layer
into every turn — save is independent of Claude's tool choice, retrieval
fires before Claude sees exploratory prompts. No stochastic "sometimes
works" behaviour; every turn persists, every exploratory prompt gets
pre-enriched.

> On a 20-query benchmark against `valuein_homepage`,
> **memory surfaces past Q&A in 80% of repeated queries and 50% of
> reworded follow-ups**, lifting end-user "answer found in top-10"
> from 75% → 90% (paraphrase) / 80% → 90% (identity). Guaranteed-save
> is unit-test verified (`TestStopHook` suite) since the bench drives
> the orchestrator directly without hooks in the loop. See
> [Compounding benchmark](#compounding-benchmark-2026-04-23).

---

## Why this is different

Most code-search and memory tools either (a) index source once and forget
your conversations, or (b) live inside one agent's UI:

- Sourcegraph / Cody: static embeddings of source files.
- Cursor / Aider: ephemeral context, tied to one tool, forgotten next session.
- Graphify: knowledge graph, rebuilt on commit.
- ChatGPT Memory: personal preferences, no code context.
- Mem0 / Letta: agent memory, but you wire it in and manage facts manually.

Two things make this stack different:

1. **Closed loop, automatic.** The answer to your last question becomes
   indexed context for the next question. No "save this" command, no memory
   dashboard — every turn persists via the Stop hook, every prompt gets
   enriched via the UserPromptSubmit hook.
2. **One memory, two agents.** Claude Code and Codex hooks both read and
   write the same `.hybrid-search/qa/` directory. Yesterday's Claude session
   informs today's Codex session, and vice versa.

Your `.hybrid-search/qa/` directory is the log of every exchange, in plain
markdown, grep-able and git-able.

### The Memory Layer at a glance

| When you… | What happens |
|-----------|--------------|
| Run `hybrid_search` | Query + top-10 results saved to `.hybrid-search/qa/YYYY/MM/*.md` |
| Run `git commit` | post-commit hook reindexes, including new qa logs |
| Ask a related question later | Past qa logs compete for top-10 like any chunk |
| Say "지난번에…" or "previously…" | Memory-intent detection → 2× boost on qa logs |
| Let time pass | 30-day half-life decay — stale answers quietly fade |
| Paste a secret by accident | Regex filter drops sensitive queries before they touch disk |

**Turn it off anytime:** `export HYBRID_SEARCH_QA_LOG=0`.

### How it compares (honestly)

`⚠️` means "claim is partly true." We won't pretend everything is `✅`.

| Project | Claude Code | Codex | 한글 | Q&A auto-save | Wiki auto-gen | Hooks | Setup | Embedding | 1st-query latency |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Mem0 | API only | API only | ❌ | manual | ❌ | ❌ | complex | external | n/a |
| Letta (MemGPT) | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ | complex | external | n/a |
| Aider repo map | ❌ | ❌ | ❌ | ❌ | ❌ | self | ✅ | none | <50 ms |
| `@mcp/server-memory` (official) | ✅ key-val | ✅ key-val | ❌ | ❌ | ❌ | ❌ | ✅ | none | <10 ms |
| Continue.dev context | VS Code only | ❌ | ⚠️ | ❌ | ❌ | ⚠️ | ⚠️ | mixed | varies |
| **hybrid-search-mcp** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 1-command after install | ⚠️ OpenAI only (no local yet) | **~400 ms** |

What this table says, plainly:

- **Strict wins:** `Q&A auto-save`, `wiki auto-gen`, dual-agent (Claude Code **and** Codex), Korean, hooks. No other OSS combines all five.
- **Honest concessions:** setup needs a Python install + API key; default embedder is OpenAI; pre-fetch adds ~400 ms.
- **Where we're not best:** if you only use one agent, `grep`-shaped queries, or zero-API-key constraint → Aider or `@mcp/server-memory` may serve you better.

---

## Privacy & Data

Read this before installing — the Memory Layer stores conversation-derived
content on disk **by default**, because that is what makes the compounding
loop work. Everything below is local, plain-text, and opt-out.

**What is stored, where:**

| Data | Location | Default |
|------|----------|---------|
| Q&A logs (your query + answer excerpt + top results) | `<project>/.hybrid-search/qa/` | **on** |
| Memory cards / facts | `<project>/.hybrid-search/memory/` | on |
| Generated wiki pages | `<project>/.hybrid-search/wiki/` | on |
| Indexed agent transcripts (Claude Code + Codex turns) | global index, from `index-conversations` / per-turn hooks | on when hooks installed |

**What leaves your machine:** chunk text (code, docs, qa logs, conversation
turns) is sent to the **OpenAI embeddings API** for embedding — nothing
else. No telemetry, no other network calls. There is currently **no local
embedding backend** — the first version ran locally, and bulk-embedding a
real codebase on laptop hardware proved unusable (sustained fan-pinning
load on an M3 MacBook; CPU path no better), so the API is a deliberate
trade-off. If sending chunk text to OpenAI is unacceptable, do not install
this tool yet; a local ONNX contribution is welcome (`[embedding] backend`
field is reserved for it).

**Safety rails built in:**
- A sensitive-query regex drops password/token/secret-shaped queries before
  they ever touch disk; files that look like secrets are never indexed.
- A quality gate drops junk turns; a re-asked question within 7 days is
  not stored twice.
- Indexed conversation turns containing prompt-injection-shaped text are
  tagged with an `[untrusted content …]` banner at display time.
- `setup` writes `.gitignore` entries so `.hybrid-search/qa/` and memory
  never end up in your repo — teammates don't see your conversation log
  unless you deliberately commit it.
- Sensitive subfolders (patient records, contracts, HR docs, …) can be
  kept out of indexing entirely: list them in a `.hybrid-search-ignore`
  file at the project root (gitignore syntax) and re-run
  `hybrid-search-mcp index . --force`. Excluded files are never chunked,
  never embedded, never sent anywhere.
- Retention: 90-day / 2,000-file auto-prune with a dry-run first pass;
  archived entries live 30 more days and are restorable (`qa-restore`).

**Full opt-out:**

```bash
export HYBRID_SEARCH_QA_LOG=0     # stop persisting Q&A turns
export HYBRID_SEARCH_INDEX_QA=0   # stop indexing existing qa logs
export HYBRID_SEARCH_ROUTER=0     # stop per-prompt pre-fetch injection
```

---

## Quick Start

### Requirements

- Python 3.11+
- OpenAI API key ([get one here](https://platform.openai.com/api-keys))

### Three commands

```bash
pipx install memory-layer-mcp                # PyPI name; the CLI is `hybrid-search-mcp`

echo "OPENAI_API_KEY=sk-..." >> ~/.env.local # once per machine — shared by all projects

cd your-project/ && hybrid-search-mcp setup  # once per project
```

`pip install memory-layer-mcp` works too — but Homebrew/system Pythons
reject it with `externally-managed-environment`, so
[`pipx`](https://pipx.pypa.io) (`brew install pipx`) is the reliable
default for a CLI tool like this.

> Why two names? `hybrid-search-mcp` on PyPI belongs to an unrelated
> project, so the distribution is published as `memory-layer-mcp` — which
> also happens to be the more accurate name. Both `hybrid-search-mcp` and
> `memory-layer-mcp` work as the CLI command.

`setup` wires everything in one shot: MCP server registration, Claude Code
hooks, the `/search` · `/maintain` skills, this project's memory hooks,
Codex hooks, a `CLAUDE.md` routing block, and `.gitignore` entries.
**Restart Claude Code** — the first file you open triggers background
indexing, and from then on every `git commit` re-indexes just the changed
files. Nothing else to run.

Adding another project later is one command: `cd other-project/ &&
hybrid-search-mcp setup` (global pieces are detected and skipped; each
project gets its own isolated index, wiki, and Q&A memory).

### CLI-only (no Claude Code)

The search engine works standalone:

```bash
cd your-project/
hybrid-search-mcp index .                       # ~165s for 1,776 files, ~$0.04
hybrid-search-mcp search "authentication flow"
```

### From source (contributors)

```bash
git clone https://github.com/curiohunter/hybrid-search-mcp.git
cd hybrid-search-mcp
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

---

## CLI Usage

```bash
# Index a project
hybrid-search-mcp index .                    # current directory
hybrid-search-mcp index /path/to/project     # specific path
hybrid-search-mcp index . --force            # full re-index

# Search
hybrid-search-mcp search "login handler"
hybrid-search-mcp search "인증 로직"                     # Korean works
hybrid-search-mcp search "handleSubmit" --node-types function
hybrid-search-mcp search "migration" --file-pattern "*.sql"
hybrid-search-mcp search "auth" --exclude-pattern "docs/*"  # drop doc noise
hybrid-search-mcp search "auth" --json                   # JSON output
hybrid-search-mcp search "query" --limit 20

# Graph exploration
hybrid-search-mcp god-nodes --cwd .          # top-N authority chunks
hybrid-search-mcp annotate-wiki --cwd .      # inject god-nodes Top-N into wiki/index.md (idempotent)
hybrid-search-mcp shortest-path <a> <b>      # call-graph path between two symbols
hybrid-search-mcp subgraph <symbol>          # N-hop forward+reverse call graph

# Memory Layer — persistent Q&A log (ON by default; =0 disables)
hybrid-search-mcp qa-list --cwd .            # recent queries, newest first
hybrid-search-mcp qa-list --all              # across every registered project
hybrid-search-mcp qa-show <id-or-hash>       # full entry (accepts hash prefix ≥4)
hybrid-search-mcp qa-grep "authority"        # frontmatter + body match
hybrid-search-mcp qa-stats --cwd .           # total / by type / by month
hybrid-search-mcp qa-prune --older-than 90d  # rotation
hybrid-search-mcp qa-prune --before 2026-01-01 --dry-run

# Status & maintenance
hybrid-search-mcp status                     # show indexed projects + hooks + skills
hybrid-search-mcp reindex --git-delta --cwd . # delta reindex (changed files only)
hybrid-search-mcp stale --cwd .              # check stale wiki pages
```

### Query auto-classification

The search engine automatically adjusts BM25/vector weights based on query type:

| Query | Type | BM25 weight |
|-------|------|-------------|
| `handleLogin` | Exact symbol | 0.8 (keyword-heavy) |
| `로그인 처리` | Korean NL | 0.15 (semantic-heavy) |
| `auth middleware` | English NL | 0.4 (balanced) |

---

## Claude Code Integration (Optional)

If you use Claude Code, hybrid-search-mcp becomes an MCP tool with auto-indexing.

### Setup

```bash
hybrid-search-mcp setup
```

This registers:
- MCP server in `~/.claude.json`
- Auto-index hook (indexes new projects on first file read)
- Stale wiki warning hook
- Wiki gap notification hook
- Route reminder hook (nudges Claude to check wiki before Grep/Glob when `.hybrid-search/wiki/index.md` exists)

Restart Claude Code after setup.

## Codex Integration (Optional)

Codex can share the same project memory layer with Claude Code. Install the
Codex hook/config pair inside a project:

```bash
hybrid-search-mcp install-codex-hook --cwd .
```

This writes `.codex/hooks.json`, enables `[features].hooks = true` in
`.codex/config.toml`, registers the MCP server as
`[mcp_servers.hybrid-search]`, and adds a small `AGENTS.md` routing note.

The Codex path uses `UserPromptSubmit` for pre-answer memory injection and
`Stop` for completed-turn persistence. `Stop` writes qa logs tagged
`trigger: codex_stop_hook` and `client: codex`, so Claude Code and Codex can
search each other's saved project memory after reindexing.

### Skills

Copy skills from `skills/` directory to `~/.claude/skills/`:

| Skill | When | Frequency |
|-------|------|-----------|
| `/setup-hybrid-search` | First install | Once |
| `/bootstrap-wiki` | Project onboarding | Per project |
| `/search` | Code/doc search with intent routing | Every time |
| `/save-wiki` | Save analysis to wiki | Optional |
| `/maintain` | Index/wiki maintenance | Occasionally |
| `/rebuild-index` | Recovery when index is corrupted or out of sync | Rare |

### Automation

| Trigger | Action | User action |
|---------|--------|-------------|
| Commit | Git delta reindex + affected wiki refresh | None |
| Branch checkout | Reindex switched branch | None |
| Before Grep/Glob | Wiki-first reminder injected into context | None |
| Before Edit/Write | STALE.md warning | Update wiki |
| After Read/Edit/Write | Undocumented module alert (`wiki-gaps.txt`) | Add wiki |

---

## How It Works

### Search strategy — intent-based routing

| Query type | Primary | Fallback | Example |
|-----------|---------|----------|---------|
| Structure/relations | Wiki | hybrid_search | "Who calls this function?" |
| Feature exploration | hybrid_search | Wiki | "Explain the billing feature" |
| Exact lookup | Grep | Read | "Where is handleSubmit?" |
| Design/context | hybrid_search | Wiki | "Why is it designed this way?" |
| Schema/DB | hybrid_search | Grep | "problems table history" |

### Benchmark (1,776 files)

| Metric | hybrid+Wiki | Grep+Read |
|--------|-------------|-----------|
| Tool calls | 2-3 | 10-15 |
| Time | ~3s | 20-30s |
| Accuracy | 90%+ | Noisy |
| Token usage | Low | High |

### Real-world benchmark (valuein_homepage, 1,307 files, 2026-04-22)

20 gold queries across 4 categories (structure / exploration / precision / rationale).
Full phase history: Phase 3 (M9 two-pass callgraph + M10 rationale) → Phase 5
(subsystem-first retrieval: module discovery + deterministic card synthesis +
module-first injection with Korean↔English alias map).

Headline ratio vs a naive token-bag grep baseline:

| Metric (top-10, n=20) | hybrid (Phase 5) | grep (token-bag) | ratio |
|-----------------------|-----------------:|------------------:|------:|
| **recall@10** (mean) | **0.77** | 0.37 | **2.1×** |
| any-hit rate | 0.90 | 0.45 | 2.0× |
| primary top-5 | 0.65 | 0.35 | 1.9× |
| **read_count_estimate** | 4.60 | 7.40 | 1.6× (fewer reads) |
| precision + rationale recall | 1.00 | 0.40 | 2.5× |

Phase-over-phase delta (hybrid track only, showing where subsystem-first helped):

| Category | recall@10 Phase 4 | recall@10 Phase 5 | delta |
|----------|-------------------|-------------------|-------|
| structure | 0.22 | **0.41** | +0.19 (~2×) |
| exploration | 0.47 | **0.67** | +0.20 |
| precision | 1.00 | 1.00 | — |
| rationale | 1.00 | 1.00 | — |

Honest trade-off: overall `read_count_estimate` worsened from 3.65 → 4.60
because module cards take rank 1, pushing the top chunk to rank 2 for queries
whose real answer is a single doc. Precision + rationale recall preserved.

### Compounding benchmark (2026-04-22)

**The question this benchmark answers:** does the Memory Layer actually
make the system smarter as it's used — or is "질문할수록 똑똑해진다" a
marketing claim with no numbers behind it?

**Methodology** (inspired by
[LongMemEval](https://github.com/xiaowu0162/LongMemEval) /
[LoCoMo](https://github.com/snap-research/locomo) session-separated recall):

1. **Cold**: move `.hybrid-search/qa/` aside, reindex with empty memory,
   run 20 queries.
2. **Plant**: run 20 Q1a "planter" queries — each logs one Q&A markdown.
3. **Warm**: reindex so the new qa files become searchable chunks, run
   20 queries against warm memory.
4. Score strict **gold retrieval** (baseline regression guard) separately
   from **memory surface** rate and **answer_found** (gold ∪ memory).

Two tracks:

- **Track A — identity**: Q1b = Q1a (user asks the same thing again).
  Upper bound for memory recall.
- **Track B — paraphrase**: Q1b keeps the principal noun phrases of Q1a
  but rewords the rest (realistic return-to-same-subsystem scenario).
  Subset `non-leaky` excludes pairs where Q1b trivially contains the
  primary_target filename.

**Results** (`benchmarks/run_compounding_bench.py` on valuein_homepage,
20 pairs, 783/783 tests green):

| Metric | Cold | Warm | Δ |
|--------|-----:|-----:|--:|
| **Track A — identity** (user repeats the question) | | | |
| answer_found (gold or memory in top-10) | 80.0% | **95.0%** | +15.0pp |
| memory surface rate | 0.0% | **80.0%** | +80.0pp |
| gold recall@10 (regression guard) | 0.656 | 0.639 | −0.017 |
| **Track B — paraphrase** (reworded follow-up) | | | |
| answer_found | 75.0% | **95.0%** | +20.0pp |
| memory surface rate | 0.0% | **65.0%** | +65.0pp |
| gold recall@10 (regression guard) | 0.613 | 0.596 | −0.017 |
| **Track B non-leaky** (15 pairs, filename not in Q1b) | | | |
| answer_found | 73.3% | **100.0%** | +26.7pp |
| memory surface rate | 0.0% | **66.7%** | +66.7pp |

**What this means:**

- **Repeated questions**: 4 out of 5 times, you see your own past answer
  without re-searching the codebase. No more retyping "wait, what did I
  ask last time?"
- **Reworded follow-ups**: 2 out of 3 times, memory still surfaces —
  the principal noun phrase is enough signal for the memory boost.
- **No regression**: strict retrieval on gold code/docs drops 1.7pp
  (within measurement noise). Memory expands answers, doesn't replace
  search.
- **Honest non-leaky subset**: every one of the 15 pairs that *don't*
  contain a primary_target filename in Q1b found an answer in top-10
  after compounding. 73% → 100%.

Run it yourself:

```bash
python benchmarks/run_compounding_bench.py
# → benchmarks/compounding_report_YYYY-MM-DD.md
```

The script backs up and restores your existing qa directory around the
experiment, so it's safe to run against a project you actively use.

### Memory integrity (v0.4.0) — consolidation beyond FIFO

Auto-prune (v0.2.0) keeps the disk bounded; orphan cleanup (v0.3.0)
keeps wiki honest. Neither touches the **content quality** of qa_log
over time. v0.4.0 adds three deterministic passes that run at the end
of every reindex:

1. **Staleness** — qa files whose ``## Top results`` references are
   all gone from the index (typical after a refactor + rename, or a
   ``.gitignore`` addition that drops a tree from indexing) are moved
   to archive.
2. **Semantic dedup** — every pair of qa_log chunks is compared on
   cosine similarity using the vectors already in the HNSW index (no
   re-embedding, no LLM). Pairs at or above
   `memory.integrity.dedup_threshold` (default 0.90) cluster via
   union-find; the newest of each cluster is kept, rest are archived.
3. **Archive TTL** — everything archived (by auto-prune, dedup, or
   staleness) lives in ``.hybrid-search/qa-archive/YYYY/MM/`` for 30
   days, then permanently unlinks. `qa-restore <id>` brings a
   regretted prune back.

```toml
[memory.integrity]
auto_prune = true              # top-level [memory] — unchanged from v0.2.0
enabled = true                 # new — v0.4.0 pass toggle
dedup_threshold = 0.90         # cosine similarity floor for near-duplicates
archive_ttl_days = 30
```

Run on demand:
```bash
hybrid-search-mcp integrity --cwd .                    # defaults
hybrid-search-mcp integrity --cwd . --dedup-threshold 0.85   # more aggressive
hybrid-search-mcp qa-restore abc12345                  # ungarbage-can
hybrid-search-mcp qa-stats --cwd .                     # active/archived counters
```

### Retention — Memory doesn't balloon your disk

`.hybrid-search/qa/` grows with every query. The reindex hook applies
[journald-style two-ceiling](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)
retention automatically — whichever ceiling binds first prunes:

```toml
[memory]
auto_prune = true
retention_days = 90       # delete anything older
max_files = 2000          # keep at most this many newest
require_first_run_confirm = true   # dry-run on first activation
```

First auto-prune on a project is a **dry-run** that reports what *would*
be deleted. Activate with:

```bash
hybrid-search-mcp qa-prune --older-than 90d --confirm-first-run
```

After that, every `reindex` applies the policy silently. Opt out with
`auto_prune = false` in `~/.hybrid-search/config.toml`.

### Automatic memory consultation — PreToolUse + SessionStart hooks

By default Claude Code users reach for Grep/Read before remembering an
MCP exists. A one-liner install fixes that:

```bash
hybrid-search-mcp install-memory-hook --cwd your-project/
```

This merges two hooks into `.claude/settings.local.json` (non-destructive
— existing hooks are preserved):

- **SessionStart**: at the start of every session, Claude sees a summary
  of the 20 most-recent Q&A topics in this project.
- **PreToolUse (Grep|Read)**: before every Grep/Read, a quick qa_log
  lookup runs — if past Q&A match the pattern, Claude is reminded to
  prefer `mcp__hybrid-search__hybrid_search`.

Both are **silent when they have nothing to say** (no past Q&A matches,
noise patterns like `.` or short strings) — no spam in the context window.
Output capped at 800 chars per injection. Inspired by the
[Graphify](https://github.com/safishamsi/graphify) pattern (71× fewer
tokens reported in real sessions by combining SessionStart + PreToolUse).

### Codex memory consultation — UserPromptSubmit + Stop hooks

Codex hooks are installed separately from Claude Code hooks:

```bash
hybrid-search-mcp install-codex-hook --cwd your-project/
```

Codex project hooks live in `.codex/hooks.json`; MCP registration and the
`hooks` feature flag live in `.codex/config.toml`. Project-local Codex
hooks only run after Codex trusts the project config layer, so use
`hybrid-search-mcp status --cwd your-project/` and a smoke test before relying
on a new install.

Reports:
- Phase 5 full write-up with per-query detail + honest failure modes:
  [`benchmarks/valuein_report_v2_2026-04-22.md`](benchmarks/valuein_report_v2_2026-04-22.md)
- Phase 4 baseline for comparison:
  [`benchmarks/valuein_report_2026-04-22.md`](benchmarks/valuein_report_2026-04-22.md)

### Memory Layer

Persist hybrid_search responses as markdown and use them as first-class search
targets. Four axes. Write and self-reference are **on by default** (that is
what makes the compounding loop work out of the box) — each is independently
opt-out:

```bash
export HYBRID_SEARCH_QA_LOG=0      # write:    stop persisting responses
export HYBRID_SEARCH_INDEX_QA=0    # self-ref: stop indexing past qa logs
```

#### 1. Write

Each response lands at `<project>/.hybrid-search/qa/YYYY/MM/DD-HHMMSS-<hash>.md`
with YAML frontmatter (query, query_type, effective BM25 weight, timestamp)
+ top-10 result snippets. A daemon thread does the I/O so the search hot
path is not touched. Default **on** (see [Privacy & Data](#privacy--data)).

#### 2. Read (human)

```
qa-list [--all] [--since 2026-04-01] [--limit 20] [--json]
qa-show <id | file-stem | hash-prefix≥4>
qa-grep <term> [--case-sensitive]
qa-stats
```

`qa-list --all` aggregates across every registered project in newest-first
order and prefixes each line with the project name.

#### 3. Self-reference (AI)

Enabled by default (`HYBRID_SEARCH_INDEX_QA=0` disables): the scanner walks
into `.hybrid-search/qa/` (overriding the `.gitignore` entry that `setup`
writes). Each log becomes a single whole-file chunk tagged
`node_type="qa_log"`, so future `hybrid_search` queries surface past
conversations alongside code. Search JSON preserves `node_type` — clients
can filter or re-rank qa hits separately.

#### 4. Rotation

```
qa-prune --older-than 30d      # or --before 2026-01-01
qa-prune --older-than 90d --dry-run --verbose
```

Durations accept `d / h / w / m` (months are 30d approximations). Empty
`YYYY/MM` directories are rmdir'd after a real prune; the `qa/` root is
preserved as an anchor.

**Caveats**
- The daemon write is reliable under the long-running MCP server. Short-lived
  CLI invocations may race with process exit — use the MCP server for
  production writes.
- qa logs may contain user content — keep both toggles off if you do not
  want those to leak into general-purpose searches.

### Tunables

`~/.hybrid-search/config.toml`:

```toml
[search]
authority_alpha = 0.3  # god-node boost weight. 0.0 disables.
                       # Validated on n=60 (NDCG +0.061, P=1.00).
                       # Externally-weighted workloads may prefer 0.5.
```

---

## Tech Stack

| Component | Stack |
|-----------|-------|
| Embedding | OpenAI `text-embedding-3-small` |
| BM25 | tantivy-py (Rust) |
| Vector DB | USearch HNSW (C++) |
| AST parsing | tree-sitter (C), 14 languages |
| Storage | SQLite WAL |

Supported languages: TypeScript, JavaScript, Python, Rust, Go, Ruby, Java, C, C++, Swift, Kotlin, CSS, HTML, SQL

---

## Performance

| Operation | Time | Cost |
|-----------|------|------|
| First index (1,776 files) | ~165s | ~$0.04 (OpenAI embed) |
| Git delta (post-commit) | ~2s | Minimal |
| Search (direct CLI / MCP call) | <2s | Free |
| **Per-prompt pre-fetch hook** (UserPromptSubmit) | **~400 ms** | Free |
| `grep` baseline (for context) | ~50 ms | Free |

The ~400 ms pre-fetch is the price you pay on **every** user prompt — that's
what lets memory and routing context arrive *before* the agent picks a tool.
For grep-shaped lookups (exact symbol, file path, error string) the router
detects this and the hook stays lightweight; for exploratory questions the
overhead pays for itself in 1.5–2× time saved downstream (valuein field
report v2).

If you want a hard off-switch: `export HYBRID_SEARCH_ROUTER=0` disables the
pre-fetch entirely.

---

## Data locations

```
~/.hybrid-search/                        # Global
├── config.toml
└── projects/{hash}/store.db

<project>/.hybrid-search/                # Per project
├── wiki/
│   ├── index.md                         # god-nodes Top-N auto-injected via annotate-wiki
│   ├── STALE.md
│   └── {module}.md
├── qa/YYYY/MM/                          # Q&A log (on by default, HYBRID_SEARCH_QA_LOG=0 disables)
└── wiki-gaps.txt
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `index <path>` | Index a project |
| `search <query>` | Hybrid search (`--file-pattern`, `--exclude-pattern`, `--node-types`, `--json`) |
| `serve` | Start MCP server (for Claude Code) |
| `setup` | Register MCP server + PreToolUse hooks in Claude Code |
| `status` | Show indexed projects, hook health, skill install state |
| `reindex --cwd .` | Delta reindex |
| `reindex --force --cwd .` | Full reindex |
| `stale --cwd .` | Check stale wiki pages |
| `install-hook --cwd .` | Install post-commit + post-checkout hooks + `.gitignore` entries |
| `install-codex-hook --cwd .` | Install Codex hooks + Codex TOML MCP config |
| `annotate-wiki --cwd .` | Inject god-nodes Top-N into wiki/index.md (idempotent) |
| `god-nodes --cwd .` | Top-N authority chunks by call-graph in-degree |
| `shortest-path <a> <b>` | Shortest call-graph path between two symbols |
| `subgraph <symbol>` | N-hop forward + reverse call graph |
| `synthesize-wiki --cwd .` | LLM synthesis for wiki pages |
| `qa-list [--all]` | Recent qa logs (Memory Layer); `--all` aggregates across projects |
| `qa-show <id>` | Full qa log by id / stem / hash prefix (≥4 chars) |
| `qa-grep <term>` | Substring search over frontmatter + body |
| `qa-stats` | Totals by query_type and month |
| `qa-prune --older-than 30d` | Delete logs older than a duration or `--before <ISO>` |
| `remove-project <name>` | Unregister a project |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `OPENAI_API_KEY not found` | Set env var or create `~/.env.local` |
| `externally-managed-environment` on pip install | Homebrew/system Python blocks global pip — use `pipx install memory-layer-mcp` |
| "hook error (non-blocking)" on every Read/Edit | Pre-0.5.1 hooks exited non-zero when idle — upgrade, then re-run `hybrid-search-mcp setup` |
| Results from wrong project | Use `--cwd` or `--project` to scope |
| Too few results | `hybrid-search-mcp index . --force` |
| Rate limit errors | Auto-retry with 0.2s batch interval |
| Hooks not working | `hybrid-search-mcp setup` (re-run) |
| Docs dominate search | `--exclude-pattern "docs/*"` or `"plan/*"` |
| qa log not written from CLI | Expected — async daemon races short-lived CLI exit. Writes via the long-running MCP server are reliable. |
| qa logs not surfacing in search | Ensure `HYBRID_SEARCH_INDEX_QA` isn't set to `0`, then re-run `reindex --force --cwd .` |

---

## License

MIT
