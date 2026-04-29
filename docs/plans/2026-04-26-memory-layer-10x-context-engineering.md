# Memory Layer 10x — Context Engineering v2

**Status:** ACTIVE — 2026-04-26  
**Supersedes / updates:** `docs/plan/2026-04-21-memory-layer-10x.md`  
**Completed prerequisite:** `docs/plans/completed/2026-04-26-codex-memory-hooks.md`  
**Goal:** make `hybrid-search-mcp` a cross-client memory layer that gets
materially smarter with use, without storing unbounded chat transcripts or
polluting the model context.

## Current Situation

The 2026-04-21 10x plan correctly identified the core product goal:

> Claude/Codex should get better over time because prior project work becomes
> searchable memory.

Since then, the memory write path matured:

- Claude Code memory hooks can install `PreToolUse`, `SessionStart`,
  `UserPromptSubmit`, and `Stop`.
- Codex memory hooks now install `SessionStart`, `UserPromptSubmit`, and
  `Stop` using `.codex/hooks.json` plus Codex TOML MCP config.
- Both clients can write to the same `.hybrid-search/qa/` corpus.
- qa logs are indexed as `node_type="qa_log"` after reindex.
- `status` now reports Claude memory hooks and Codex hooks separately.

The remaining weakness is memory quality:

- `Stop` currently stores the user prompt and metadata, but not the assistant's
  useful answer content.
- Storing every full answer forever would grow files quickly, increase index
  noise, and raise privacy/token risks.
- The right next step is not "store more raw text"; it is "promote useful
  conversation turns into compact, typed memory."

The next weakness after the core memory loop is product UX:

- Users should not have to remember separate install, compact, facts, and
  reindex commands.
- A project should be able to explain why memory is not working before the
  user discovers it through a bad answer.
- Memory needs a visual surface: cards created, raw logs vs real turn logs,
  hook readiness, and suggested recall questions.

## External Research Checkpoints

These references point to the same product direction:

- LangGraph memory docs distinguish short-term memory from long-term memory,
  and split long-term memory into semantic, episodic, and procedural forms.
  They also frame memory writes as either hot-path updates or background
  consolidation: <https://docs.langchain.com/oss/python/concepts/memory>
- Deep Agents docs treat persistent memory as filesystem-backed files and
  describe skills as procedural memory that can be loaded on demand rather
  than injected into every prompt:
  <https://docs.langchain.com/oss/python/deepagents/memory>
- Zep / Graphiti uses raw episodes plus a temporal knowledge graph: raw
  conversation data is ingested, then entities/facts/relationships are
  synthesized and queried with temporal, full-text, semantic, and graph search:
  <https://help.getzep.com/graphiti/getting-started/overview>
- Zep's 2025 memory architecture paper reports that dynamic conversational
  memory needs temporal synthesis rather than static RAG over raw messages:
  <https://blog.getzep.com/zep-a-temporal-knowledge-graph-architecture-for-agent-memory/>
- Letta/MemGPT's memory-block framing is relevant for context budgets: memory
  should be organized into bounded, purposeful blocks rather than treated as
  one unbounded transcript:
  <https://www.letta.com/blog/memory-blocks>

Implication for this project:

> Keep raw turn evidence, but retrieve compact semantic/episodic/procedural
> memory units first.

## Product Principle

Do not build an append-only transcript archive and call it memory.

Build a three-tier project memory system:

1. **Episode memory** — raw-ish turn evidence, cheap and bounded.
2. **Semantic memory** — facts, decisions, architecture notes, file/module
   relationships, and rationales extracted from episodes.
3. **Procedural memory** — project-specific instructions and learned workflow
   rules that update `CLAUDE.md` / `AGENTS.md` / skills cautiously.

The agent should usually see memory cards, not full transcripts.

## Target Architecture

```
Claude/Codex turn
  -> hook Stop
  -> qa_log episode record
       query
       answer_excerpt
       client / trigger / tools
       referenced files
       safety flags
  -> background consolidation
       memory cards
       decision records
       fact relationships
       workflow rules
  -> reindex
       node_type=memory_card boosted
       node_type=qa_log fallback
  -> UserPromptSubmit / SessionStart context
       compact relevant cards first
       raw qa only when needed
```

## Memory Types

### Episode Memory

Stored under:

```
.hybrid-search/qa/YYYY/MM/*.md
```

Purpose:

- Preserve enough evidence to audit what happened.
- Allow raw recovery when a card is wrong or insufficient.
- Feed background consolidation.

Do not store unbounded full answers by default.

Episode frontmatter v3:

```yaml
query: "..."
trigger: "codex_stop_hook"
client: "codex"
tools_used: ["Grep", "Read"]
answer_chars: 2345
answer_excerpt_chars: 1800
memory_quality: "candidate"
sensitive: false
```

Episode body:

```markdown
# Q: ...

## Answer excerpt

First 1.5k-3k useful chars, sanitized and truncated at sentence boundary.

## Top results

Existing result list, when available.
```

### Semantic Memory Cards

Stored under:

```
.hybrid-search/memory/cards/YYYY/MM/*.md
```

Purpose:

- Searchable, compact, high-signal project knowledge.
- Preferred retrieval unit for memory.

Example:

```yaml
type: memory_card
source: qa_log
client: codex
confidence: high
status: active
topics: ["codex hooks", "qa_log", "memory"]
files:
  - src/hybrid_search/codex_hooks.py
  - src/hybrid_search/memory/qa_log.py
decisions:
  - "Codex UserPromptSubmit writes pending prompt but does not write qa_log."
  - "Codex Stop is the only automatic Codex qa_log writer."
followups:
  - "Store answer_excerpt in qa_log v3."
```

Body:

```markdown
## Summary

Codex memory uses UserPromptSubmit for context injection and Stop for completed
turn persistence.

## Evidence

- qa: 2026/04/26-080217-c3a79c0e.md
- docs: docs/plans/completed/2026-04-26-codex-memory-hooks.md

## When to use

Use when answering how project memory is saved across Claude Code and Codex.
```

### Episodic Examples

Some turns are valuable not as facts, but as examples of how to solve a task.
These should become few-shot style examples:

```yaml
type: episodic_example
task: "install and verify a client hook"
outcome: "success"
steps:
  - "Install hook"
  - "Restart client"
  - "Ask exploratory prompt"
  - "Verify qa_log trigger"
```

### Procedural Memory

Procedural memories should not be generated freely into every prompt. They
should become small, reviewed updates to:

- `CLAUDE.md`
- `AGENTS.md`
- local skills
- command recipes

Examples:

- "After installing Codex hooks, restart Codex before testing."
- "When validating memory hooks, check both hook injection and Stop save."
- "Do not judge Memory Layer health from MCP registration alone."

## What To Store

Store:

- Durable decisions
- Bug causes and fixes
- Architecture relationships
- File/module ownership knowledge
- Reusable workflows
- Failed assumptions that would waste time later
- User/project preferences

Avoid or down-rank:

- Long raw command outputs
- Repetitive status logs
- Draft reasoning with no durable conclusion
- Transient environment details
- Secret-like strings
- Full answer transcripts unless explicitly requested

## Ranking Policy

Default retrieval order for memory-aware prompts:

1. `memory_card`
2. `episodic_example`
3. `qa_log` with `answer_excerpt`
4. raw `qa_log` metadata-only
5. ordinary docs/code chunks

Precision/code-symbol queries should still prefer code chunks. Memory cards
should boost exploratory, architectural, "what did we decide", and "last time"
queries.

## Implementation Plan

### P0 — Fix The Episode Record

**Goal:** Stop hooks preserve useful answer signal without storing full
transcripts.

Tasks:

- Add `answer_excerpt` and `answer_excerpt_chars` to `QARecord`.
- Sanitize answer excerpts with the existing sensitive-query regex extended to
  answer text.
- Truncate to a configurable limit, default 2,000 chars.
- Prefer sentence/paragraph boundary truncation.
- Codex: populate from `last_assistant_message`.
- Claude: collect assistant text from transcript tail and populate the same
  field.
- Keep `answer_chars` for backward compatibility.

Tests:

- Codex Stop stores `answer_excerpt`.
- Claude Stop stores `answer_excerpt`.
- Sensitive answer text suppresses excerpt or the whole record.
- Existing v0.2-v0.4 qa readers tolerate absent excerpt.

Acceptance:

- A real Codex/Claude turn creates qa with query + answer excerpt + client.
- Full suite green.

### P1 — Memory Card Schema And CLI

**Status:** shipped in `memory-card create/list/show/grep`.

**Goal:** introduce compact semantic memory as the primary retrieval unit.

Files:

- `src/hybrid_search/memory/cards.py`
- `src/hybrid_search/memory/card_reader.py`
- `tests/test_memory_cards.py`

CLI:

```bash
hybrid-search-mcp memory-card create --from-qa <id>
hybrid-search-mcp memory-card list --cwd .
hybrid-search-mcp memory-card show <id>
hybrid-search-mcp memory-card grep <term>
```

Card fields:

- `summary`
- `decisions`
- `files`
- `followups`
- `topics`
- `source_ids`
- `confidence`
- `status`: `active | stale | superseded | archived`

Implementation note:

- Start with deterministic/local extraction heuristics:
  - file paths from answer/query
  - "decision", "fix", "root cause", "next" markers
  - first paragraph summary fallback
- Add LLM-assisted card generation later, behind an explicit command or config.

Acceptance:

- A qa log can be promoted to a memory card.
- Cards are markdown + frontmatter and parse without PyYAML.
- Cards are gitignored by default unless user opts in.

### P2 — Index Cards Before Raw QA

**Status:** shipped for `.hybrid-search/memory/cards/` scanner allowlist,
`node_type="memory_card"`, and memory-card ranking boost.

**Goal:** search retrieves compact memory before long raw logs.

Tasks:

- Include `.hybrid-search/memory/cards/` in scanner allowlist.
- Tag card chunks as `node_type="memory_card"`.
- Keep qa logs as `node_type="qa_log"`.
- Add ranking boost for `memory_card` on exploratory/memory-intent prompts.
- Render memory-card snippets with summary/decisions/files first.

Tests:

- Card appears above source qa log for "what did we decide" queries.
- Precision symbol search does not get polluted by cards.
- Reindex includes cards and qa logs.

Acceptance:

- "지난번 Codex hook 저장 구조 어떻게 했지?" returns memory cards top-3.

### P3 — Background Consolidation

**Status:** shipped as deterministic `memory compact`; LLM-assisted
summarization remains a future optional mode.

**Goal:** prevent qa from becoming an append-only dump.

CLI:

```bash
hybrid-search-mcp memory compact --cwd .
hybrid-search-mcp memory compact --cwd . --since 7d
hybrid-search-mcp memory compact --cwd . --dry-run
```

Behavior:

- Group related qa logs by topic/hash/vector similarity.
- Create or update one memory card per cluster.
- Mark older cards superseded when a newer card contradicts them.
- Move old low-value raw qa to archive after successful card promotion.

Use deterministic clustering first:

- query hash/similarity
- shared files
- shared trigger/client
- temporal proximity

LLM summarization can be optional:

- off by default
- explicit `--llm` flag
- output schema validated before write

Acceptance:

- 20 related qa logs compact into <=5 cards.
- No card is generated for pure command/status noise.
- Archive movement is reversible through existing qa restore path.

### P4 — Procedural Memory Guardrail

**Status:** shipped as `memory procedural review`; apply remains intentionally
manual/future.

**Goal:** convert repeated workflow lessons into reviewed instructions, not
silent prompt drift.

Tasks:

- Detect procedural candidates:
  - repeated "next time do X" patterns
  - hook/install/test workflows
  - failure modes fixed more than once
- Write candidates to:

  ```
  .hybrid-search/memory/procedural-candidates.md
  ```

- Add CLI:

  ```bash
  hybrid-search-mcp memory procedural review --cwd .
  hybrid-search-mcp memory procedural apply --cwd .
  ```

Acceptance:

- Procedural updates never auto-edit `CLAUDE.md` or `AGENTS.md` without an
  explicit command.
- Candidate file is small, reviewed, and diffable.

### P5 — Temporal Facts / Graph Lite

**Status:** shipped as `memory facts export/list` backed by
`.hybrid-search/memory/facts.jsonl`.

**Goal:** borrow the useful part of Graphiti without building a full graph
database first.

Start with a lightweight facts file:

```
.hybrid-search/memory/facts.jsonl
```

Record:

```json
{
  "subject": "Codex Stop hook",
  "predicate": "writes",
  "object": "qa_log when prompt and last_assistant_message exist",
  "valid_from": "2026-04-26T08:02:17Z",
  "valid_to": null,
  "source": "memory_card:..."
}
```

Tasks:

- Extract facts from cards, not raw transcripts.
- Support invalidation when a newer card contradicts an older fact.
- Add query path for "current truth" vs "history".

Acceptance:

- "현재 Codex hook 저장 방식은?" ignores superseded facts.
- "예전에는 어떻게 했지?" can surface historical facts.

### P6 — Evaluation

**Status:** shipped for the self-hosting memory benchmark in
`benchmarks/memory_layer_v2_2026-04-28.md`; broader valuein
agent-in-loop benchmark remains future work.

**Goal:** prove 10x as lower turns/tokens and better recall, not just more
stored files.

Benchmarks:

- Extend existing valuein benchmark with memory-specific queries.
- Add this repo as a self-hosting benchmark.
- Measure:
  - `memory_hit_rate@3`
  - `card_vs_raw_ratio`
  - `context_pack_bytes`
  - `read_count_estimate`
  - manual agent-in-loop turns/tokens for 5 queries

Gold queries:

- "Codex hook 저장 구조 어떻게 결정했지?"
- "Claude와 Codex memory hook 차이가 뭐였지?"
- "왜 Stop만 Codex qa_log writer로 정했지?"
- "메모리 레이어에서 qa_log와 memory card는 어떻게 달라?"
- "이 프로젝트에서 hook 설치 검증 순서는?"

Acceptance:

- Memory queries retrieve card top-3 in >=80% of gold cases.
- Average context bytes decrease vs raw qa retrieval.
- Agent-in-loop pilot shows fewer search/read turns than metadata-only qa.

Shipped result:

- `memory_hit_rate@3 = 1.00` on the 5-query self-hosting memory gold set.
- `card_vs_raw_ratio = 3.00`.
- `read_count_estimate = 1.00` for hybrid vs `11.00` for grep.
- `benchmarks/valuein_gold.json` includes the 5 memory queries with per-query
  project overrides.

### P7 — Product UX: Setup, Doctor, Refresh

**Status:** shipped for setup / doctor / refresh / recall.

**Goal:** make the memory layer operable as a product, not a set of internal
maintenance commands.

User-facing commands:

```bash
hybrid-search-mcp setup --cwd .
hybrid-search-mcp doctor --cwd .
hybrid-search-mcp memory refresh --cwd .
hybrid-search-mcp memory recall "지난번 대시보드 변경 방향이 뭐였지?" --cwd .
```

Short aliases can be added later (`hybrid setup`, `hybrid refresh`), but the
canonical implementation should live in the existing CLI first.

#### `setup`

One command installs the whole client surface:

- Claude memory hooks:
  - `PreToolUse`
  - `SessionStart`
  - `UserPromptSubmit`
  - `Stop`
- Codex hooks:
  - `.codex/hooks.json`
  - `.codex/config.toml` with `[features] codex_hooks = true`
  - project MCP server registration
- `CLAUDE.md` / `AGENTS.md` routing snippet when missing or stale.

Acceptance:

- A fresh project reaches `Claude memory hooks: 4/4`.
- A fresh project reaches `Codex hooks: project` and `Codex config: project
  feature, project MCP`.
- Output ends with the one required human action: restart Claude/Codex.

#### `doctor`

Diagnose why memory did not work.

It must explicitly report:

- Claude hook count and missing events.
- Codex hook/config state.
- `qa` count, `memory_card` count, `facts` count.
- Last compaction time.
- Whether recent records are mostly `mcp_tool` logs or real completed turns
  (`stop_hook`, `codex_stop_hook`).
- Whether cards are indexed as `node_type="memory_card"`.

Example output:

```text
Memory is not fully active.

Claude: 2/4 hooks (missing UserPromptSubmit, Stop)
Codex:  missing
Corpus: qa=14, cards=0, facts=0
Recent QA: 12 mcp_tool logs, 0 completed-turn logs

Fix:
  hybrid-search-mcp setup --cwd .
  restart Claude/Codex
  hybrid-search-mcp memory refresh --cwd .
```

Acceptance:

- The valuein failure mode is caught before the user asks a recall question:
  `Claude memory hooks: 2/4`, `Codex missing`, `cards=0`.
- Doctor distinguishes "search logs exist" from "conversation memory exists".

#### `memory refresh`

One command runs the whole background consolidation loop:

```bash
memory compact
memory procedural review
memory facts export
reindex
status summary
```

Behavior:

- Skip expensive work when no new qa/card files exist.
- Show created/updated/skipped counts.
- Surface next recall questions from the newest cards.
- Never delete raw qa unless an explicit archive flag is provided.

Example output:

```text
Hybrid Memory Refresh

Project: valuein_homepage

Hooks
  Claude: 4/4 ready
  Codex:  ready

Memory
  QA logs:       14
  New cards:      6
  Total cards:    6
  Facts:         18

Index
  Reindexed:      6 memory files
  Status:         ready

Try
  "대시보드 페이지 변경 방향에 대해 지난번에 뭐라고 했지?"
```

Acceptance:

- A user can run one command after a work session and get a searchable memory
  card corpus.
- The command is idempotent.
- The command fails with actionable guidance when hooks are incomplete.

#### `memory recall`

Provide a memory-first search surface for humans:

- Force or strongly prefer `memory_card`, then `episodic_example`, then
  `qa_log`.
- Print concise card summaries and evidence paths.
- Clearly label `mcp_tool` logs as tool-search evidence, not conversation
  memory.

Acceptance:

- "대시보드 페이지에 대해 무슨 대화를 나눴지?" returns actual completed-turn
  cards if they exist.
- If only `mcp_tool` logs exist, it says so and tells the user how to enable
  completed-turn memory.

### P8 — Visual Memory Surface

**Status:** shipped as static HTML report via `memory open`.

**Goal:** make memory visible enough that users trust it and can debug it
without reading raw markdown files.

CLI:

```bash
hybrid-search-mcp memory open --cwd .
```

Minimum viable implementation:

- Generate a static HTML report under `.hybrid-search/memory/report.html`.
- Open automatically only when the platform allows it; otherwise print the
  path.
- No server required for v1.

Report sections:

- Project readiness:
  - Claude hook state
  - Codex hook state
  - MCP registration
  - last reindex / last compaction
- Corpus:
  - qa count
  - completed-turn qa count
  - `mcp_tool` qa count
  - card count
  - facts count
- Recent memory cards:
  - summary
  - topics
  - source qa
  - referenced files
- Raw vs compact ratio:
  - qa logs promoted
  - qa logs not promoted
  - cards vs raw qa
- Suggested recall prompts:
  - generated from newest cards and procedural candidates
- Warnings:
  - cards=0
  - missing Stop hook
  - Codex not installed
  - mostly `mcp_tool` logs

Acceptance:

- Opening the report makes the valuein state obvious: hooks incomplete,
  cards=0, and recent dashboard hits are tool-search logs rather than actual
  conversation turns.
- The report gives the same fix path as `doctor`.
- The visual surface is useful without requiring a web app framework.

### P9 — Trust Signals at Retrieval

**Status:** shipped for hybrid/semantic result trust meta and superseded/archived
down-ranking — added 2026-04-28 from external Codex review of valuein
usage.

**Goal:** surface confidence and recency on every search hit so the agent does
not silently overweight stale or low-confidence memory.

External evidence (Codex, 2026-04-28):

> 검색 결과에 "최근성", "코드에서 검증됨", "과거 대화 기반 추정" 같은 신뢰도
> 표시가 있으면 LLM이 메모리를 맹신하지 않고 적절히 검증할 수 있습니다.

P3 and P5 already track confidence on the **write** side (`status`,
`valid_to`, `confidence`). They are not surfaced on the **read** side, so
hybrid_search hits look uniformly authoritative regardless of age or origin.

Tasks:

- Annotate each hit snippet with a single-line trust meta:
  - `[card · confidence=high · 2d ago]`
  - `[qa · stop_hook · 14d ago]`
  - `[fact · superseded · valid_to=2026-04-20]`
  - `[code · indexed]`
- Down-weight `superseded` and `archived` records in default ranking.
- Suppress superseded facts in "current truth" queries; allow them in
  "history" queries (P5 query path).
- Doctor reports per-record-type freshness, not only corpus totals.

Acceptance:

- A user can read 5 hits and tell at a glance which is durable and which is
  one stale guess.
- "현재 어떻게 처리하지?" does not return a card whose `status=superseded`
  in the top 3.
- An old qa_log without an answer excerpt is clearly labeled as
  metadata-only.

### P10 — Domain Glossary As First-Class Memory

**Status:** shipped as deterministic `domain_term` card subtype and ranking lane;
glossary seeding benchmark remains future work — added 2026-04-28 from external Codex review of valuein
usage.

**Goal:** make the "vibe coder vocabulary → code location" translation, which
the tool already does implicitly via BM25+vector+wiki, an explicit and
benchmarkable feature.

External evidence (Codex, 2026-04-28):

> "상담에서 테스트 예약되는 부분"이라고 말해도, 실제로는 consultations,
> entrance_tests, students, reservation 등이 얽혀 있을 수 있는데, 이 간극을
> 줄여줍니다.

This is the strongest framed value of the tool but is currently emergent, not
designed. Domain-heavy projects like valuein expose the gap most clearly: a
non-engineer's natural-language phrase rarely matches a single code symbol.

Tasks:

- Add `type: domain_term` as a memory_card subtype:

  ```yaml
  type: domain_term
  term: "입학테스트 예약"
  aliases: ["entrance test reservation", "테스트 예약"]
  maps_to:
    tables: ["entrance_tests", "reservations", "students"]
    files: ["app/models/entrance_test.rb", "app/services/reservation_creator.rb"]
    flows: ["consultation → reservation → test"]
  confidence: medium
  source: qa_log:...
  ```

- Boost `domain_term` cards above generic `memory_card` when the query is a
  Korean NL phrase or contains domain-language markers.
- Seed the glossary deterministically from existing wiki pages and qa_log
  excerpts; LLM-assisted seeding stays optional behind a flag.
- Extend the benchmark with a domain-NL gold set: phrasings a non-engineer
  would use, scored by hit-rate on the correct file/symbol.

Acceptance:

- A 5-query domain-NL benchmark on valuein hits the right file in top-3 ≥
  80% of cases.
- Glossary entries are creatable and editable via `memory-card create
  --type domain_term`.
- Glossary boost does not regress the existing self-hosting memory bench.

## Revised Roadmap

| Phase | Deliverable | Est |
|---|---|---|
| P0 | qa_log v3 answer excerpt | 0.5-1 day |
| P1 | memory card schema + CLI | 1-2 days |
| P2 | card indexing + ranking boost | 1 day |
| P3 | deterministic compaction | 2-3 days |
| P4 | procedural candidates | 1 day |
| P5 | facts.jsonl graph-lite | 2-3 days |
| P6 | benchmark + report | shipped self-hosting; valuein pilot future |
| P7 | setup / doctor / refresh / recall product UX | 1-2 days |
| P8 | static visual memory report | 1-2 days |
| P9 | trust signals on retrieval hits | 1-2 days |
| P10 | domain glossary as first-class memory | 2-3 days |

Total: 13-22 days, depending on whether LLM-assisted card generation ships in
this release or stays behind a manual flag, whether the visual report stays
static HTML or becomes a richer dashboard, and whether glossary seeding stays
deterministic.

## Non-Goals

- Full transcript archival by default.
- Full Graphiti clone or external graph database dependency.
- Automatic rewriting of `CLAUDE.md` / `AGENTS.md` without user review.
- Cross-user memory sync.
- Storing secrets or credential-adjacent text.

## Immediate Next Step

Implement P7 next.

P0-P6 proved the memory loop can work. The valuein smoke test exposed the next
product gap: users can have hooks partially installed, `mcp_tool` logs present,
and zero cards, then reasonably conclude memory is broken. `setup`, `doctor`,
and `memory refresh` should make that state obvious and fixable with one or two
commands.

## Completion Criteria

This plan is complete when:

- Claude and Codex both save bounded answer excerpts.
- Memory cards are indexed and preferred over raw qa logs.
- Compaction keeps active memory small.
- Procedural lessons become reviewed candidates.
- Benchmarks show memory-specific improvement in hit rate and context bytes.
- `status` reports Claude memory hooks, Codex hooks, qa/card counts, and last
  compaction time.
- `setup` installs Claude and Codex memory surfaces for a project.
- `doctor` explains missing hooks, zero-card corpora, and `mcp_tool`-only logs.
- `memory refresh` runs the consolidation/reindex loop in one command.
- `memory open` gives a visual report of memory health, cards, facts, and
  suggested recall questions.
- Search hits carry trust meta (record type, confidence, recency, supersession)
  so the agent and user can tell durable memory from stale guesses.
- Domain glossary entries (`type: domain_term`) translate non-engineer NL
  phrases to the right files/tables/flows, with a benchmark proving hit-rate
  on a domain-NL gold set.
