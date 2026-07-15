# Reddit draft — r/LocalLLaMA

**Status:** DRAFT — ready for owner review (updated 2026-07-15 for v0.7.2: no-local-backend correction, EN holdout numbers)
**Target:** reddit.com/r/LocalLLaMA/submit
**Posting window:** Tue/Wed/Thu 18:00–21:00 ET (r/LocalLLaMA is heavily
US/EU evening — local-LLM hobbyists log on after work). Avoid weekends
(noisier feed, lower per-post visibility).
**Owner action:** read once, edit ≤ 2 lines in own voice, submit.

---

## Title

**[Project] hybrid-search-mcp — code memory layer with OpenAI embeddings (looking for help making the local-embedder path competitive)**

*Why this framing:* this subreddit hates uncritical OpenAI dependency.
Leading with the weakness — and asking for help — converts the
weakness into the post's invitation to engage. Avoid hiding the
dependency; this audience will find it in the first comment anyway.

---

## Flair

`Resources` (the auto-categorizer will accept this; `Discussion` is
backup if removed).

## Body (≈ 280 words)

I built a code-search + memory MCP server for a solo developer running
Claude Code + Codex on a Korean / English mixed-language production
codebase. It survived a real payment-API migration (case study in
repo), and on a 20-pair Cold→Warm benchmark it lifts answer-found
from 80 % → 95 % on repeated questions, 73 % → 100 % on the non-leaky
paraphrase subset. A second bench covers what memory tools usually
don't report: stale-fact supersession 6/6 on the Korean dev set AND
6/6 synthetic on an untouched English OSS holdout (the first English
holdout scored 1/6 — matcher overfit to Korean — so we rewrote it
language-general and re-ran on a fresh repo; CHANGELOG-derived planted
cases 3/5, published as-is). Zero false-confident answers across 27
verified-absent probes on three codebases, ~3 k tokens per answer on
the wire.

**The honest gap for this subreddit:** it ships OpenAI
`text-embedding-3-small` **only** — there is currently NO local
embedder path. I tried; I dropped it deliberately rather than ship a
worse one quietly. A `backend` config field is reserved for it, and
every number I can defend today was measured with OpenAI embeddings.

What I tried locally and why I dropped it:

- **bge-m3 / bge-large-en-v1.5** — drop-in dimension-mismatched
  without re-indexing. Quality on Korean domain language hasn't been
  measured rigorously.
- **`nomic-embed-text` via Ollama** — works at the wire level, recall
  on the existing benchmarks visibly worse, not yet quantified.
- **Mixed CPU/GPU latency budget** — the architecture has a hard
  ~400 ms pre-fetch budget per prompt. CPU-only inference on bge-m3
  exceeds that on my 8-core machine. GPU works.

What I'd value from this community:

1. Which local embedder model has actually beaten OpenAI
   `text-embedding-3-small` on Korean + English mixed corpora that
   you've seen empirically (not just benchmarks).
2. Whether the local-only path should be the default for this
   subset of users, or stay opt-in.
3. Whether anyone has wired tantivy-py + a local embedder behind an
   MCP server before and hit unexpected sharp edges.

Repo: https://github.com/curiohunter/hybrid-search-mcp (MIT)
Case study: docs/case-studies/2026-05-20-payssam-v2-migration.md
Bench: README § Compounding benchmark + § Memory bench v2

Will reply through the evening. Hard data on local-embedder comparisons
appreciated.

---

## Anticipated replies + draft responses

### "Why not just use [local model X]?"

Likely concrete suggestions: `mxbai-embed-large`, `gte-Qwen2-1.5B`,
`jina-embeddings-v3`. Reply pattern:

> Haven't benchmarked that one yet on my corpus. Have you measured it
> on multilingual code (not just MTEB averages)? If yes I'd love the
> numbers. There's no local backend wired today — the `backend` config
> field is reserved — but the bench scripts
> (`benchmarks/run_compounding_bench.py`, `run_memory_bench_v2.py`)
> are embedder-agnostic: wire an ONNX/Ollama backend behind the
> `Embedder` interface and they'll measure it. That PR is the
> contribution I'd most like to receive.

### "MCP is overengineered, just use grep"

> Fair on `grep`-shaped queries (exact symbol, file path). The README
> compares hybrid + 400 ms vs grep + ~50 ms and the router defers to
> grep when the query is identifier-shaped. The case study is
> explicit: in the payment migration, Codex (executor) ended up with
> 30 % hybrid-search / 40 % official docs / 30 % grep+read. The split
> is intentional.

### "Why MIT and not AGPL?"

> Decided early it'd be easier to ship into a Claude Code workflow if
> MIT. No strong principled position; open to changing if it stops
> being viable.

---

## Acceptance

- [ ] Owner reads, edits ≤ 2 lines in own voice
- [ ] Posted to r/LocalLLaMA in Tue–Thu evening ET window
- [ ] Standby for replies for ~3 hours after post
- [ ] Cross-post to r/ClaudeAI is **separately scheduled** (different
      draft, different angle — see `2026-05-XX-reddit-r-claudeai.md`)
