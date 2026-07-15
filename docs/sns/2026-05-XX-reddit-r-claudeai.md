# Reddit draft — r/ClaudeAI

**Status:** DRAFT — ready for owner review (numbers updated 2026-07-15 for v0.7.2)
**Target:** reddit.com/r/ClaudeAI/submit
**Posting window:** Tue/Wed 10:00–13:00 ET (r/ClaudeAI skews 9-to-5 dev
audience, lunch-window scrolling). Avoid late-night and weekend slots —
they have lower engagement here than on r/LocalLLaMA.
**Owner action:** read once, edit ≤ 2 lines in own voice, submit.
**Spacing:** post **at least 4 days after** the r/LocalLLaMA submission
so cross-subreddit moderators don't flag the duplicate.

---

## Title

**Memory shared between Claude Code and Codex — hooks-based, no manual save/recall**

*Why this framing:* this subreddit is full of Claude-Code-as-daily-driver
users who actively dislike "I have to remember to save this" workflows.
Leading with the hooks-based auto-save / dual-agent angle hits the
audience's actual pain. Avoid the word "MCP" in the title (it's the
implementation, not the value).

---

## Flair

`Coding` (subreddit's primary technical-content flair).

## Body (≈ 270 words)

I run Claude Code as my planning agent and Codex as the executor on a
1,307-file Next.js production codebase. The single biggest friction
was: yesterday's Claude reasoning vanished by today's Codex turn, and
vice versa. Every session started cold.

So I built [hybrid-search-mcp](https://github.com/curiohunter/hybrid-search-mcp).
Four Claude Code hooks wire a memory layer into every turn:

- **`Stop`** writes every answered query as a markdown qa-log to
  `.hybrid-search/qa/YYYY/MM/`.
- **`UserPromptSubmit`** pre-fetches related past Q&A and injects it
  before Claude picks a tool.
- **`SessionStart`** surfaces a summary of recent topics in this project.
- **`PreToolUse (Grep|Read)`** reminds Claude to check memory before
  reaching for grep.

Save is independent of Claude's tool choice — no "remember this" call,
no dashboard. The Codex hook pair (`install-codex-hook`) writes to the
same directory, so Claude Code and Codex share one memory.

Two things from real use that I think are interesting:

**1. The router has a `confidence: weak` contract.** When `hybrid_search`
returns weak, Claude is contractually obligated by CLAUDE.md to retry
through the fallback hint before answering. In a production payment-API
migration last week, Codex hit `weak` frequently and fell back to grep —
ended at 30 % hybrid / 40 % official docs / 30 % grep. The
[case study](https://github.com/curiohunter/hybrid-search-mcp/blob/main/docs/case-studies/2026-05-20-payssam-v2-migration.md)
publishes the verbatim scores: Claude 9/10, Codex 6.5/10. The lower
score is the router doing its job.

**2. I measured my own Phase 4 hypothesis and it failed.** I expected
the v1 routing marker to lift first-pick tool correctness. It didn't
(delta = 0 across 4+4 sessions). The
[replay write-up](https://github.com/curiohunter/hybrid-search-mcp/blob/main/benchmarks/router_replay_2026-05.md)
publishes the null result.

**3. The stale-fact test keeps finding real bugs before good news.**
A bench that plants two conflicting answers 90 days apart (does the
newer one outrank the stale one?) scored 50 % on first run — qa
recency was reading filesystem mtime, which lies after `git clone`.
Fixed: 6/6 on my Korean dev corpus. Then a blind holdout on an English
OSS repo scored **1/6** — the topic matcher was overfit to Korean
token statistics. Rewrote it language-general, and a fresh untouched
holdout (ripgrep, single run, published as-is) scored 6/6 synthetic
(incl. Korean-question→English-memory probes), 3/5 on
CHANGELOG-derived planted cases, 2/3 adversarial. Zero false-"strong"
across 27 verified-absent probes on three codebases, ~3 k tokens per
answer (README § Memory bench v2 → Language generality).

Repo: https://github.com/curiohunter/hybrid-search-mcp (MIT)

Happy to dig into hook internals or CLAUDE.md routing rules in comments.

---

## Anticipated replies + draft responses

### "How does this compare to Claude's built-in memory?"

> Claude's built-in memory (user-level facts, "remember I prefer
> TypeScript") is orthogonal — it persists user preferences across all
> projects. This persists *project Q&A* in the project directory, so
> different projects don't bleed memory into each other, and the
> markdown qa-logs are grep-able / git-able. Both can run together.

### "Won't the context window explode if every past Q&A is searchable?"

> Two safeties. (a) Pre-fetch hook caps injection at 800 chars per
> turn and stays silent when nothing meaningfully matches. (b) Memory
> chunks compete for top-10 like any other chunk — they don't get a
> free pass into context. There's a memory-intent boost (2×) when the
> user says "지난번에…" / "previously…", but it has to win on score.

### "Does the post-Stop save slow down Claude?"

> The qa-log write is on a daemon thread, so the Stop hook returns
> immediately. Pre-fetch (UserPromptSubmit) does add ~400 ms per
> prompt — the README has the numbers. For grep-shaped lookups the
> router stays light; the cost is paid mostly on exploratory turns.
> `HYBRID_SEARCH_ROUTER=0` is the hard off-switch.

### "Source for the Codex hook pair?"

> `src/hybrid_search/codex_hooks.py` + `install-codex-hook` CLI command.
> Writes `.codex/hooks.json` and enables `[features].hooks = true` in
> `.codex/config.toml`. Smoke test: `hybrid-search-mcp status --cwd .`.

---

## Acceptance

- [ ] Owner reads, edits ≤ 2 lines in own voice
- [ ] Posted to r/ClaudeAI in Tue/Wed 10:00–13:00 ET
- [ ] At least 4 days after the r/LocalLLaMA submission
- [ ] Standby for replies for ~2 hours after post
