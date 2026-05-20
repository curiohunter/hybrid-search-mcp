# Case Study — Payssam V2 Migration (2026-05-20)

**Project:** valuein_homepage (Next.js / Supabase / TypeScript, 1,307 files, 708 commits)
**Task:** Migrate from 결제선생 PaysSam V1 → V2 API (sandbox / stg / paymint env + 9 routes + edge functions + scripts)
**Tools in use:** Claude Code (Opus 4.7, planning agent) + Codex (executor agent)
**Memory layer:** hybrid-search-mcp v0.5-dev (post Phase 1–4 ship)

This is a production migration on a real codebase, evaluated by both
agents independently after the work shipped. We publish both scores
verbatim — including the lower one — because the gap between them is
itself evidence the system works as designed.

---

## Phase A — Impact mapping (Claude Code, planning agent)

> **Self-reported score: 9 / 10**

**What happened.** A single `hybrid_search` call with two parallel queries
mapped the entire migration surface in 1.3 seconds across 26,793 indexed
chunks:

- `"결제선생 PaysSam API 도메인 sandbox stg paymint 환경변수"`
- `"payssam API v1 v2 partner endpoint base url"`

**Top-5 of the first result set was clean** — `lib/payssam-client.ts`,
`lib/payssam-env-guard.ts`, `types/payssam.ts`, plus the relevant
`docs/features/` and `docs/archive/` design notes. No noise.

After that single call, the rest of the planning step was direct
`Read` / `Bash grep`. Pre-fetch hook had already surfaced
`payssam-verification-request.md` from the archive at session start —
the agent never had to ask for it explicitly.

**Time saved vs `grep` baseline:**

| Path | Estimated time |
|---|---|
| `grep payssam` standalone | ≈ 15 min (70+ matches, read each by hand) |
| `hybrid_search` 1 call + context cleanup | ≈ 5 min |

→ **~3× speedup on the impact-mapping step**, on the strict portion of
work where hybrid-search is *meant* to win.

### Score breakdown (Claude Code, verbatim)

| Metric | Score | Comment |
|---|---:|---|
| Precision | 10 | Top-5 was all core, zero noise |
| Recall | 9 | Single search surfaced services / 9 routes / scripts / edge functions |
| Korean ↔ English matching | 10 | Mixed `결제선생` + `paymint` + `sandbox` matched cross-lingually |
| Speed | 10 | 1.3s across 26,793 chunks |
| Pre-fetch hint | 9 | `payssam-verification-request.md` surfaced automatically at turn start |
| Limit | −1 | V2 spec lives on `developers.payssam.kr`, outside the indexed corpus (not the tool's fault) |

### Verdict (Claude Code, verbatim)

> "외부 시스템과 인접한 모든 코드/문서/스크립트를 한 번에 가시화해야
> 하는" 시나리오가 hybrid-search의 sweet spot. 직접 grep으로 했으면
> 영향 범위 매핑에 가장 시간이 들었을 텐데, 1번의 검색이 그 단계를
> 통째로 건너뛰게 해줬다. CLAUDE.md의 "탐색형 질문 → hybrid_search
> 먼저" 규칙이 실제로 회수된 케이스.

---

## Phase B — Execution (Codex, edit agent)

> **Self-reported score: 6.5 / 10**

**What happened.** Codex executed the actual migration — 30+ file edits,
verify, commit. During execution it kept calling `hybrid_search` for
help finding specific identifier-shaped things (`/partner` endpoint
deletion sites, `V1 → V2` swap points). Many of those returned
`confidence: weak`, so Codex correctly fell back to `rg` / `Read` / diff
review.

### Contribution breakdown (Codex, verbatim)

| Source | Contribution |
|---|---:|
| hybrid-search | ≈ 30 % |
| Official docs (`developers.payssam.kr`, `llms-full.txt`) | ≈ 40 % |
| `grep` / `Read` / code diff review | ≈ 30 % |

### What Codex liked (verbatim)

- Plan docs + past QA + wiki gave decent starting orientation
- Surfaced `docs/plans/2026-05-20-payssam-v2-migration.md`,
  `services/payssam-service.ts`, `lib/payssam-client.ts`, webhook
  handlers quickly
- AGENTS routing rule got it past "blindly grep first" reflex; it
  consulted memory layer before reaching for text search

### What Codex flagged as gaps (verbatim)

- `confidence: weak` was frequent; some irrelevant results mixed in
- Final correctness verification came from `llms-full.txt`, `rg`, direct
  file reads, code diff — not the memory layer
- **Time-conditioned policy** ("/partner removed as of date X") needed
  the official doc, not the index
- **Wiki was at pre-edit state** — couldn't help judge correctness of
  in-flight modifications

### Verdict (Codex, verbatim)

> 초기 지도/기억 검색 도구로는 유용하지만, 최종 판단 도구는 아니다.
> 이번처럼 외부 API 마이그레이션에서는 hybrid-search 30%, 공식 문서
> 40%, grep/read/code review 30% 정도 기여.

---

## Why the gap (9 vs 6.5) is evidence the system works

Two agents, same project, same corpus, same migration — and the lower
score is the *more important* data point. Here is why:

1. **Impact mapping vs execution are different query shapes.**
   - Mapping = NL / exploratory ("어디가 영향 받나"). Hybrid_search sweet spot.
   - Execution = identifier-shaped ("`/partner` 호출처 전부"). Grep territory.
   - The router is *designed* to suggest different tools for these. It did.

2. **Codex's frequent `confidence: weak` is the Phase 4 contract working,
   not failing.** When the query shape leaves hybrid-search's strength
   zone, the response *honestly* tells the agent so. Codex saw that and
   fell back to `rg`. This is exactly the
   `Phase 2 — Result quality signals` design (`docs/plans/completed/
   2026-05-01-router-and-quality-signals.md`).

3. **Codex naturally rates the helper by how much of the work it
   absorbed**, not by how appropriately it deferred. So the system gets
   penalized in the score for *being honest about its limits*. This is
   the right trade for a memory layer that ships into production
   decisions — overclaiming would be worse.

4. **30 / 40 / 30 split is the correct distribution for this task.** A
   memory layer that took 70% of a payment-API migration's decision load
   would be alarming, not impressive. The official spec is on
   `developers.payssam.kr`; the index can't and shouldn't try to mirror
   live external policy.

So both numbers — 9 and 6.5 — are saying the same thing about the system,
from two different ends of one workflow.

---

## Gap surfaced: in-flight (uncommitted) change visibility

The one genuine criticism worth chasing is Codex's last bullet: **the
wiki / index was at pre-edit state during the migration**.

Today's invalidation model is `post-commit hook → delta reindex`. Mid-
migration, the agent has modified 30+ files but committed zero. The
index still shows yesterday's reality, so queries about *what we just
changed* miss.

**Phase 5 candidate — in-flight visibility:**

- Surface `git diff HEAD` files as ephemeral chunks alongside indexed
  chunks (no DB write, in-memory layer)
- Or `hybrid-search-mcp reindex --staged` for explicit user trigger
- Or pre-write hook that marks modified files as priority for the next
  query in the same session

Estimated impact: Codex 6.5 → 8+ on the same migration. Low implementation
cost (no schema change; the search orchestrator already accepts ephemeral
chunks via `chunk_results`).

This becomes the natural next plan if we choose to ship Phase 5.

---

## What this case study is good for

- **OSS distribution hook.** Two honest scores on a real migration is
  hard to fake and unusual to publish. See
  `docs/plans/2026-05-20-distribution-artifacts.md` for the
  distribution plan that uses this case study as anchor evidence.
- **Phase 5 motivation.** In-flight visibility has a concrete, measured
  motivation now (Codex's wiki-stale complaint), not a hypothetical one.
- **Router validation.** Phase 1–4 ship is no longer just G1–G6 numbers
  — it survived a real payment migration and behaved as designed.

---

## Reproducibility

- valuein_homepage repo, branch active at 2026-05-20
- hybrid-search-mcp `4192ae4` (Phase 4 shipped) + `81e2517` (README
  honesty pass)
- Migration plan: `valuein_homepage/docs/plans/2026-05-20-payssam-v2-
  migration.md`
- Pre-fetch hits visible in `valuein_homepage/.hybrid-search/qa/2026/05/`
  on the dates of the migration sessions
