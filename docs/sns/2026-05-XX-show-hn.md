# Show HN draft — hybrid-search-mcp

**Status:** DRAFT — ready for owner review (numbers updated 2026-07-15 for v0.7.2: EN holdout arc, no-local-embedder correction)
**Target:** news.ycombinator.com/submit
**Posting window:** Tue/Wed 09:00–11:00 ET (HN clock norms; highest pickup
window for tools-and-libraries). Avoid Fri afternoon and weekends.
**Owner action:** read once, edit ≤ 2 lines in own voice, submit.

---

## Title candidates (pick one)

1. **Show HN: We measured our own marketing claim and it failed (memory layer for Claude Code + Codex)**
2. **Show HN: A memory layer shared between Claude Code and Codex — production-measured, including where it didn't help**
3. **Show HN: hybrid-search-mcp — one memory across Claude Code and Codex (honest scorecard inside)**

*Recommendation: #2.* Strongest on the dual-agent angle (the actual
differentiator) without leading with self-flagellation. #1 is the catchier
hook but front-loads negativity; #3 is safest but blandest.

---

## Body (≈ 320 words)

I'm a solo developer maintaining a Next.js + Supabase production codebase
(`valuein_homepage` — 1,307 files, 708 commits, Korean domain language).
I use Claude Code as the planning agent and Codex as the executor, and the
single biggest friction was that they couldn't see each other's history.
Yesterday's Claude reasoning vanished by today's Codex turn.

So I built [hybrid-search-mcp](https://github.com/curiohunter/hybrid-search-mcp):
BM25 + vector retrieval, plus a closed-loop memory layer where every
answered query writes a markdown qa-log and every future query treats
those logs as first-class search chunks. Claude Code and Codex hooks
both read and write the same `.hybrid-search/qa/` directory.

Three things I'd want to know about it before installing:

**1. It survived a real migration, not a synthetic benchmark.** Last
week I migrated a payment API (PaysSam V1 → V2, 30+ files, 9 routes,
edge functions). Both agents self-scored after shipping. Claude Code:
9/10 on impact mapping. Codex: 6.5/10 on execution. The gap is in the
[case study](https://github.com/curiohunter/hybrid-search-mcp/blob/main/docs/case-studies/2026-05-20-payssam-v2-migration.md)
verbatim — both scores. The lower number is the more interesting one:
Codex's `confidence: weak` fallbacks are the router doing its job.

**2. An untouched holdout caught my system overfit — and I shipped
that finding too.** The stale-fact defense ("newer answer outranks the
old one") scored 6/6 on my Korean dev corpus. Then I ran a blind
holdout on an English OSS repo (httpx) and it collapsed to **1/6**:
the topic matcher was calibrated on Korean token statistics. I rewrote
it language-general (stemming, identifier weighting, complete-link
grouping), kept the Korean set at 6/6, froze the code, and ran a
fresh untouched holdout (ripgrep): synthetic update **6/6** (incl.
Korean-question→English-memory probes), CHANGELOG-derived planted
cases **3/5**, adversarial **2/3**, and **0 false-"strong"** across 27
verified-absent probes on three codebases — single run, published
as-is, misses diagnosed in the repo. (An earlier hypothesis also
failed and shipped: the v1 routing marker, delta = 0 —
[null-result write-up](https://github.com/curiohunter/hybrid-search-mcp/blob/main/benchmarks/router_replay_2026-05.md).)

**3. The honest costs.** ~400 ms pre-fetch overhead per prompt (vs
~50 ms grep). OpenAI `text-embedding-3-small` only — **no local
embedding backend, by choice**: bulk-embedding pinned an M3's fans for
the whole run; a backend config field is reserved and an ONNX
contribution is welcome. Python install + one setup command.
If those don't fit, [Aider's repo-map](https://aider.chat/docs/repomap.html)
or [@mcp/server-memory](https://github.com/modelcontextprotocol/servers)
may serve you better.

Repo: https://github.com/curiohunter/hybrid-search-mcp (MIT)

Happy to answer questions.

---

## Prepared replies — first 30 minutes of comments

These are first-pass drafts. Owner edits before posting any of them.

### Reply 1 — "Why not just use Mem0 / Letta / MemGPT?"

Fair question — those are the obvious comparisons.

Mem0 and Letta are agent-memory frameworks: you wire memory operations
into your code (`add`, `search`, `update`), and the memory is API-shaped.
hybrid-search-mcp is the opposite end: it's a code-search tool that
happens to treat past Q&A as another searchable corpus. There's no
"store this fact" call; every answered query persists automatically
via the Stop hook, and every new prompt gets enriched via the
UserPromptSubmit hook. No memory operations in your code.

The other gap: Mem0/Letta are external services or libraries that
don't ship Claude Code or Codex hooks. The dual-agent shared-memory
property of this project (Claude writes, Codex reads, and vice versa)
isn't on their roadmap because they're not built around those clients.

If your use case is "agent that needs to remember user facts across
sessions" — use Mem0 or Letta. If it's "two CLI agents on one
codebase that I want to compound knowledge across" — this is the
narrower thing.

### Reply 2 — "Does it scale beyond one project / one user?"

Honestly: I don't know yet, because I built it for myself.

What I do know:
- It's been running daily on a 1,307-file, 708-commit codebase for
  ~3 months without disk runaway. The retention policy
  (`max_files=2000`, `retention_days=90`, journald-style two-ceiling
  prune) does its job.
- The compounding benchmark (README §) is run-it-yourself: 20 query
  pairs, Cold→Warm reindex, identity recall 80→95%, paraphrase
  75→95%. Reproducible on any project.
- A second bench (README § Memory bench v2) covers the failure modes
  memory tools don't usually report: stale-fact supersession at 6/6
  on the Korean dev set AND 6/6 synthetic on an untouched English OSS
  holdout (ripgrep, single run, published as-is — CHANGELOG-derived
  planted cases 3/5, adversarial 2/3, misses diagnosed in the repo).
  Zero false-"strong" confidence across 27 verified-absent probes on
  three codebases, and ~3k tokens per answer on the MCP wire. The
  first English holdout scored 1/6 and exposed the matcher as
  Korean-overfit; the fix + fresh holdout is the v0.7.2 release.
- It is **not** multi-user. The `.hybrid-search/` directory is local
  to one machine. No server, no auth, no shared write coordination.
  Adding that would be a different project.

The honest summary: it's a tool for one developer running two agents
on one codebase. If your problem is "team of N developers sharing
memory," this isn't that.

### Reply 3 — "Korean only? What about English-only codebases?"

It works on English-only codebases — the cross-language part is a
bonus, not a requirement.

The retriever is BM25 (tantivy-py) + OpenAI embeddings (or local
fallback). BM25 is language-agnostic at the tokenizer level;
`text-embedding-3-small` handles ~100 languages including English
natively. The Korean ↔ English bilingual matching is what
distinguishes it from tools that index only one language at a time,
but it doesn't penalize monolingual codebases — your queries land
through the same hybrid pipeline.

The README benchmarks (1,776 files, 20 gold queries) are run against
a mixed-language codebase but include English-only queries.
recall@10 = 0.77 on hybrid vs 0.37 on grep-baseline.

If you only need English: it still works; you're just not using one
of its features. If you need Japanese / Chinese / etc. specifically,
the tokenizer is the open question — I haven't tested those.

---

## Acceptance

- [ ] Owner reads, edits ≤ 2 lines in own voice
- [ ] Title selected (recommend #2)
- [ ] Submitted Tue or Wed 09:00–11:00 ET
- [ ] Reply drafts available in this file for first 30 min of comments
- [ ] If < 5 points after 1 h, archive, re-attempt within a week at a
      different slot (do not spam)
