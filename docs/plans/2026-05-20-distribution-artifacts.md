# Distribution Artifacts — Let the Work Speak

**Status:** ACTIVE — 2026-05-20
**Owner:** karw79@gmail.com (Claude Code drafts, owner posts)
**Why this plan exists:** the owner is empirically strong at building
and measuring, and has stated that self-promotion ("나를 들어내는 것")
is the weakest part of the loop. The right response is not to push the
owner harder onto camera, but to produce artifacts where **the work
speaks** and the owner only has to press Submit. This plan is the
inventory of those artifacts.

**Anchor evidence** (already on disk, no fabrication):

- `docs/case-studies/2026-05-20-payssam-v2-migration.md` — production
  payment-API migration, dual-agent self-assessment (Claude 9/10 +
  Codex 6.5/10, both verbatim).
- `benchmarks/router_replay_2026-05.md` — G4 manual replay where the
  marketing hypothesis was measured and falsified, and we said so.
- `benchmarks/valuein_gold.json` + Phase 5 recall numbers in README.
- README `## How it compares (honestly)` table with self-assigned `⚠️`
  marks.

These four are unusual in OSS and form a credible distribution package
on their own.

---

## Success Goals

| # | Metric | Target | How measured |
|---|---|---|---|
| **D1** | At least one English-speaking distribution channel exposure with a self-contained artifact | Show HN posted | URL recorded |
| **D2** | At least one Korean-speaking developer audience exposure | OKKY or velog post live | URL recorded |
| **D3** | Visual asset available for sharing (no founder face / voice required) | 2-min asciinema or mp4 on README + GitHub | File committed |
| **D4** | "Why I built this" narrative captured for repeat use (README, posts, future grant applications) | Page published in repo + linked from README | File committed |
| **D5** | First external GitHub star / install from someone not in the founder's direct network | 1 install via PyPI download stats or 1 cold-source star | logs / star activity |

D1–D4 are owner-driven actions on Claude-drafted content. D5 is the
downstream signal that the rest worked.

---

## Decisions (settled — no second-guessing during execution)

### Dec-1. Owner does not appear on camera or in voice

Every artifact must work with owner face/voice = 0 %. Asciinema, captions,
prose only. This removes the single biggest friction the owner named.

### Dec-2. Honesty as the hook, not the disclaimer

Every artifact opens with measured trade-offs (400 ms latency, OpenAI
default, 1-cmd setup) and the falsified G4 marketing hypothesis. This is
inverted from typical OSS marketing — it is also why these will read
differently from the dozens of competing memory-layer posts.

### Dec-3. Production case study is the anchor, not synthetic benchmarks

`docs/case-studies/2026-05-20-payssam-v2-migration.md` carries the
weight. Synthetic numbers come second. The two reasons: (a) production
data is harder to dismiss; (b) the case study is fully self-contained
once it is in the repo — every artifact can link to it instead of
re-explaining.

### Dec-4. English first, Korean second, but neither blocks the other

Order of execution favors English channels (#3 Show HN + #2 demo) only
because that is where dual-agent + memory-layer discovery currently
concentrates. The Korean post (#5) is not lower priority — it is just
sequenced after the assets it shares are ready.

### Dec-5. The owner reviews, does not author

Claude Code drafts every artifact end-to-end. The owner reads, edits at
most 2 places in their own voice, then publishes. If a draft needs more
than 2 owner edits, that is a signal Claude misjudged voice — Claude
rewrites, not the owner.

---

## The 5 Artifacts (priority order)

### A1. 2-minute demo (asciinema or short mp4)

**Why first:** highest ROI per minute of work. One artifact gets reused
in #3, #4, #5, the GitHub README, and any future channel. Without it,
written posts have nothing to link to and lose half their stopping power.

**Deliverable:**
- `assets/demo-2min.cast` (asciinema) **OR** `assets/demo-2min.mp4`
- Embed-link in README hero block
- Linked in every other artifact

**Script (60–120 s, no narration, captions only):**

| Beat | Time | Screen content | Caption |
|---|---|---|---|
| 1 | 0:00 | Empty terminal, `cd valuein_homepage` | "Real production codebase. 1,307 files, 708 commits." |
| 2 | 0:05 | `pip install hybrid-search-mcp && hybrid-search-mcp setup` | "One install + one command." |
| 3 | 0:20 | Open Claude Code, ask `"결제선생 V2 마이그레이션 영향 범위"` | "Day 1 — Claude Code planning." |
| 4 | 0:35 | hybrid_search call → top-5 result with payssam-client.ts etc. | "1.3 s mapped 30+ affected files." |
| 5 | 0:55 | Exit Claude Code, open Codex, ask `"V2 endpoint 마이그레이션 시작"` | "Day 2 — same memory, different agent." |
| 6 | 1:10 | Codex pre-fetch surfaces yesterday's Claude Q&A | "No re-search. Codex sees yesterday's Claude answer." |
| 7 | 1:25 | Show CHANGELOG / case study file | "Production-measured: Claude 9/10, Codex 6.5/10. Both honest." |
| 8 | 1:45 | Closing card: "github.com/curiohunter/hybrid-search-mcp" | — |

**Owner action:** record terminal session against valuein_homepage (or a
trimmed copy of it). Claude reviews timing on the .cast file and notes
any cuts needed.

**Acceptance:**
- ≤ 2 min duration
- 0 face / 0 voice
- All captions in EN (KO version can come later)
- File committed under `assets/`
- Embedded in README hero block

### A2. Show HN post (English)

**Why next:** the audience reading HN for OSS memory layers is exactly
the audience this project serves. Hook = the falsified G4 finding +
case study, not the feature list.

**Deliverable:** Markdown draft at `docs/sns/2026-05-XX-show-hn.md`
with title + body + 3 prepared Q&A replies.

**Title candidates** (Claude will pick or A/B):
- "Show HN: We measured our own marketing claim and it failed (memory layer for Claude Code + Codex)"
- "Show HN: A memory layer for Claude Code and Codex — production-measured, including where it didn't help"
- "Show HN: hybrid-search-mcp — one memory across Claude Code and Codex (honest scorecard inside)"

**Body skeleton:**
1. One-paragraph framing — built to survive valuein, not as a demo project.
2. The honest result: Claude Code 9/10, Codex 6.5/10 on a real migration,
   link to case study.
3. Why the gap is informative (router doing its job, weak-confidence
   contract working).
4. What it can't do (OpenAI embedder default, 400 ms pre-fetch, single user).
5. Asciinema link.
6. Repo link, AGPL / license, install command.

**Owner action:** sanity read, edit at most 2 lines in own voice,
submit to news.ycombinator.com/submit. Standby for replies for ≈ 2 h
post-submit; Claude prepares first-pass reply drafts for 3 likely
questions:
- "Why not just use [Mem0 / Letta / etc.]?"
- "Does it actually scale beyond one project?"
- "Korean-only? What about English-only codebases?"

**Acceptance:** post submitted; reply drafts in hand for first 30 min
of comments.

### A3. "Why I built this" page (EN + KO)

**Why third:** Show HN and demo both need this to link to. Owner-narrative
without being a memoir.

**Deliverable:** `docs/why.md` (EN) and `docs/why.ko.md` (KO).

**Structure (≤ 400 words each):**
1. **The problem the owner had** (factual, no melodrama): a Next.js +
   Supabase production codebase used by paying customers. The existing
   famous tools (Graphify, Karpathy-style wiki, Cursor index) lost the
   thread on a codebase this size + this Korean.
2. **What was tried, what broke** (specific): N=1 observation that
   Graphify's one-shot graph went stale and Karpathy-style wiki gave
   "plausible but wrong" pages for Korean domain terms.
3. **What was built and what it costs** (honest): closed-loop memory,
   dual-agent, hooks, but 400 ms latency + OpenAI default + Python install.
4. **Where the proof is** (link out): case study, Phase 4 G4 falsified
   measurement, README comparison table.

Owner appears in third person ("the maintainer") to keep the tone
material-focused. Owner is named once in the footer; nothing more.

**Owner action:** read, edit ≤ 2 places, approve.

**Acceptance:** both files committed, EN linked from README, KO linked
from `docs/why.ko.md`.

### A4. Reddit posts (r/LocalLLaMA + r/ClaudeAI)

**Why fourth:** reddit lifts last after HN and after the visual asset.
Posting too early without a demo link burns the karma.

**Deliverable:** `docs/sns/2026-05-XX-reddit-r-localllama.md` and
`docs/sns/2026-05-XX-reddit-r-claudeai.md`.

**Tone differences** (Claude handles this — owner does not need to
context-switch):
- r/LocalLLaMA: emphasise the OpenAI dependency as the honest weakness;
  invite contributions toward a credible local-embedder path.
- r/ClaudeAI: emphasise the dual-agent (Codex + Claude Code) angle and
  the Phase 4 self-justify / weak-confidence contract.

Both link to the asciinema demo and the case study.

**Owner action:** submit at off-hours appropriate to subreddit
demographics (Claude will note specific time windows in each draft).

**Acceptance:** both posts submitted within 1 week of each other.

### A5. Korean OSS post (OKKY / velog)

**Why last:** sequence the Korean post after EN assets are live so the
KO post can quote real HN / Reddit reactions if they happened, or stand
alone if they did not. Either is fine — the post does not depend on
external response.

**Deliverable:** `docs/sns/2026-05-XX-okky-velog.md`

**Hook (KO):** "그래피파이도, 카파시 LLM wiki도 내 valuein 코드베이스를
못 감당했다. 그래서 1인 개발자가 직접 만든 메모리 레이어를 오픈소스로
공개합니다. Claude Code도 Codex도 같은 기억을 공유합니다."

**Body:** translated and culturally-fit version of the Why page, plus
the case study summary, plus the asciinema embed.

**Owner action:** submit to OKKY 공유게시판 + 본인 velog.

**Acceptance:** at least one of (OKKY, velog) live.

---

## Sequencing

```
Session N+1: A1 demo (Claude drafts script → owner records)
Session N+2: A3 Why page EN + KO (Claude drafts → owner reviews)
             A2 Show HN draft + reply prep (Claude drafts)
Session N+3: Owner submits Show HN; Claude on standby for replies
Session N+4: A4 Reddit posts drafted and submitted
Session N+5: A5 Korean post drafted and submitted
```

This puts the highest-ROI work first, lets each artifact reuse the
previous one, and keeps each owner-touchpoint short.

---

## Non-goals

- Twitter / X — too noisy, low ROI for OSS code tools; revisit only if
  HN traffic justifies.
- LinkedIn personal post — would require the owner to be the protagonist,
  which violates Dec-1. (LinkedIn *company* post is fine if there is a
  company entity; not currently the case.)
- Conference talks, podcasts — same reason as LinkedIn.
- Paid promotion — premature; we need cold-source signal first.
- A "manifesto" blog post — the case study and Why page together cover
  the same surface area without the manifesto vibe.

---

## Risks and mitigations

- **HN flameout** — post lands at wrong hour, no traction. Mitigation:
  Claude provides 3 candidate launch times based on HN clock norms; if
  first attempt is < 5 points after 1 h, archive draft and try once
  more at a different slot within a week. Do not spam.
- **"This is just RAG" dismissal** — common HN response to memory
  projects. Mitigation: prepared reply citing the qa_log → next-prompt
  closed loop and the Phase 4 confidence contract — neither of which is
  in textbook RAG.
- **Owner overload** — distribution work compounds emotionally even if
  it is light hour-wise. Mitigation: one artifact per session max,
  explicit stop at end of each session.
- **Case study referenced from a public post; private valuein detail
  leaks** — review the case study before each public link; redact
  internal stuff if needed. (Currently the document keeps to public
  API and architecture, no customer data.)

---

## Definition of Done

When **D1 (Show HN posted) + D3 (demo committed) + D4 (Why page
committed)** have all happened. D2 and D5 are followers, not gates.

This plan completes itself when those three lines are crossed; move it
to `docs/plans/completed/` and append an Outcome section with the
posted URLs and what the comments said.
