# Why this exists

A short, factual account of what the maintainer needed, what didn't work,
what was built, and what it costs. No origin story — just the parts that
inform whether this tool is for you.

---

## The problem

The maintainer ships and maintains `valuein_homepage` — a Next.js +
Supabase production codebase used by paying customers. 1,307 files,
708 commits, mostly Korean domain language with English framework code.

On a codebase that size, in that language mix, the existing famous
options each lost the thread:

- **Graphify**'s one-shot knowledge graph went stale between commits
  and silently misled queries about recent work.
- **Karpathy-style LLM wiki** produced "plausibly worded but wrong"
  pages for Korean domain terms (`결제선생`, `paymint`, `학생 숙제 제출`),
  because the LLM had no signal to defer when it didn't actually know.
- **Cursor / Aider repo-map** worked per-session, but yesterday's
  reasoning was gone today, and Claude Code and Codex couldn't see each
  other's history at all.

None of these tools are bad. They just weren't built for a single
maintainer running two agents on a multilingual, actively-edited
production codebase.

## What was built — and what it costs

Three pieces, deliberately small:

1. **A hybrid retriever** (BM25 + vector + call-graph god-nodes) that
   the router picks between based on query shape, not a single weight.
2. **A closed-loop memory layer**: every answered query writes a
   markdown qa-log; every future query treats those logs as first-class
   search chunks. No "save this" command, no dashboard.
3. **Dual-agent hooks**: Claude Code and Codex both read and write the
   same `.hybrid-search/qa/` directory. Yesterday's Claude answer
   surfaces in today's Codex turn before Codex searches anything.

The honest costs:

- **~400 ms pre-fetch overhead** per prompt — vs ~50 ms for `grep`.
- **OpenAI `text-embedding-3-small` is the only embedder.** We tried
  local embedding and dropped it deliberately (bulk-embedding pinned an
  M3's fans for the whole run); a `backend` config field is reserved
  and an ONNX contribution is welcome.
- **Python install + one setup command** — not a single binary.

## Where the proof is

- **Production case study** — `docs/case-studies/2026-05-20-payssam-v2-
  migration.md`. Dual-agent self-scored after a real payment-API
  migration: Claude Code 9/10, Codex 6.5/10. Both verbatim; the gap is
  itself evidence the router does its job.
- **Falsified marketing claim** — `benchmarks/router_replay_2026-05.md`.
  We measured whether the v1 routing marker actually improves first-pick
  tool correctness. It didn't (delta = 0 vs baseline). We shipped that
  finding instead of burying it.
- **Compounding benchmark** — 20-pair Cold→Warm test, README §
  Compounding benchmark. Identity recall 80→95 %, paraphrase 75→95 %,
  non-leaky subset 73→100 %.
- **An untouched holdout that caught us** — README § Language
  generality. Stale-fact supersession was 6/6 on the Korean dev set;
  a blind English OSS holdout scored 1/6 and exposed the matcher as
  Korean-overfit. v0.7.2 is the language-general rewrite: Korean set
  still 6/6, fresh holdout (ripgrep, single run, published as-is)
  6/6 synthetic / 3/5 CHANGELOG-derived planted / 2/3 adversarial,
  zero false-`strong` across 27 verified-absent probes on three
  codebases.

If those four line up with what you need, the install is
[in the README](../README.md#quick-start).

---

*Maintained by [@curiohunter](https://github.com/curiohunter) — karw79@gmail.com*
