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

## Revised Roadmap

| Phase | Deliverable | Est |
|---|---|---|
| P0 | qa_log v3 answer excerpt | 0.5-1 day |
| P1 | memory card schema + CLI | 1-2 days |
| P2 | card indexing + ranking boost | 1 day |
| P3 | deterministic compaction | 2-3 days |
| P4 | procedural candidates | 1 day |
| P5 | facts.jsonl graph-lite | 2-3 days |
| P6 | benchmark + report | 1-2 days |

Total: 8-13 days, depending on whether LLM-assisted card generation ships in
this release or stays behind a manual flag.

## Non-Goals

- Full transcript archival by default.
- Full Graphiti clone or external graph database dependency.
- Automatic rewriting of `CLAUDE.md` / `AGENTS.md` without user review.
- Cross-user memory sync.
- Storing secrets or credential-adjacent text.

## Immediate Next Step

Implement P0 first.

P0 is the smallest change that fixes the current mismatch:

- today, `Stop` proves a turn happened;
- after P0, `Stop` preserves the useful answer signal;
- later phases promote that signal into cards and facts.

Do not start with LLM summarization. First make the raw episode record good
enough, bounded, and safe.

## Completion Criteria

This plan is complete when:

- Claude and Codex both save bounded answer excerpts.
- Memory cards are indexed and preferred over raw qa logs.
- Compaction keeps active memory small.
- Procedural lessons become reviewed candidates.
- Benchmarks show memory-specific improvement in hit rate and context bytes.
- `status` reports Claude memory hooks, Codex hooks, qa/card counts, and last
  compaction time.
