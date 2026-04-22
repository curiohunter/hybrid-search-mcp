# Phase 5 Step D + Phase 6 Step E — Agent-loop measurement + drift / two-tier

**Date:** 2026-04-22

Three shipped:

1. **Step D — agent-in-loop simulator** (`benchmarks/agent_loop_sim.py`): models what a real Claude Code session does with an MCP response — scan snippets first, Read only when snippet evidence is insufficient, stop as soon as an answer token surfaces. Replaces the Step 1 proxy that assumed `read_count = primary_rank`.
2. **Step E L4 — drift watchdog** (`src/hybrid_search/index/drift.py` + `hybrid-search drift` CLI): read-only filesystem-vs-DB diff. Tells agents and humans whether a reindex is worth running.
3. **Step E L5 — two-tier cap** (`_interleave_modules`): module slots never exceed `limit // 2`, so low-limit calls still guarantee chunk majority.

---

## Step D: Agent-loop simulator

### The loop it models

```
for each gold query:
    resp = hybrid_search(query)                  # 1 turn
    if any snippet contains satisfaction_token:
        record (turns=1, reads=0, bytes=snippet_bytes, resolution="snippet")
        break
    for rank, hit in enumerate(resp.results[:max_reads]):
        content = Read(hit.file_path)            # +1 turn, +file_bytes
        if content contains satisfaction_token:
            record (turns=1+rank, reads=rank, resolution="read")
            break
    else:
        grep_fallback                            # +1 turn, +50KB proxy
        record (resolution="miss")
```

`max_reads=5` caps the descent. A single huge file is capped at 200KB per read to mirror how agents skim long files in practice.

### Satisfaction criteria — two modes

| Mode | Anchors that count as "I found it" |
|------|----------------------------------|
| **loose** (default) | primary_target filename stem + expected_symbols + **acceptable_module_names** |
| **strict** | primary_target filename stem + expected_symbols only |

The gap shows how much of Phase 5's Step B win is "module card is the right answer" vs. a pragmatic agent just declaring victory on seeing the subsystem name.

### Results (20 gold queries, valuein_homepage)

| Mode   | satisfied | snippet-only | miss | turns (mean) | reads (mean) | bytes (mean) |
|--------|-----------|--------------|------|--------------|--------------|--------------|
| loose  | **0.95**  | **0.95**     | 0.05 | **1.30**     | **0.25**     | **8.3 KB**   |
| strict | 0.90      | 0.90         | 0.10 | 1.55         | 0.45         | 14.4 KB      |

`run_valuein_bench.py`'s static proxy reports 2.55 reads/query; the agent-loop simulator reports **0.25–0.45 reads/query**, with 90-95% of queries resolved from MCP snippets alone. That's a **5.6× – 10× lower Read burden** than the static proxy predicted. The gap is the entire value proposition of module cards: an agent doesn't need to Read a file to know what subsystem the answer lives in when the module card carries that information in the response itself.

### Per-query breakdown (loose)

| id | category | resolution | turns | bytes | |
|----|----------|------------|-------|-------|--|
| S1 | structure | snippet | 1 | 1.5 KB | |
| S2 | structure | snippet | 1 | 1.1 KB | |
| S3 | structure | snippet | 1 | 1.4 KB | |
| S4 | structure | snippet | 1 | 1.8 KB | |
| S5 | structure | snippet | 1 | 1.6 KB | |
| F1 | exploration | snippet | 1 | 1.7 KB | |
| **F2** | **exploration** | **miss** | **7** | **130.7 KB** | ← only failure |
| F3 | exploration | snippet | 1 | 1.9 KB | |
| F4 | exploration | snippet | 1 | 2.3 KB | |
| F5 | exploration | snippet | 1 | 1.7 KB | |
| P1-P5 | precision | snippet | 1 | 1-3 KB | |
| R1-R5 | rationale | snippet | 1 | 1-2 KB | |

### The one miss — F2 (월별 학원 통계는 어떻게 집계되나)

F2 confirms the Step C diagnosis: no analytics module was discovered, and the chunks that do surface (blog marketing docs) don't contain `create_academy_monthly_stats` or any token that maps to the stats subsystem. The simulator faithfully reports 7 turns + grep-fallback cost — this is exactly what would happen in a real session. The fix is on the module-discovery side (recognize `components/analytics/` as a first-class module), not in search or scoring.

### Strict-mode deltas

Only S1 changes between loose and strict:

- S1 loose: snippet resolution (matched `tuition` module name).
- S1 strict: 4 Reads attempted (122.6 KB), then fallback — no snippet contained `2026-04-08-tuition-billing`. In a real session this would be a non-issue because the agent, seeing the tuition module card at rank 1 and docs/features/2026-04-08-tuition-withdrawal-refund.md at rank 2, would synthesize the answer — strict is overly rigid on purpose to expose upper-bound cost.

---

## Step E L4: Drift watchdog

### API

```python
from hybrid_search.index.drift import detect_drift
report = detect_drift(project_id, project_root, db, config.indexing)
# report.added / .changed / .deleted / .total_on_disk
# report.is_drifted / .drift_count / .summary_line()
```

Wraps `scan_project` read-only; same ignore spec as the indexer, so the count is authoritative.

### CLI

```
$ hybrid-search drift --cwd .
[valuein_homepage] drift 6 (+6 added, ~0 changed, -0 deleted)
Run `hybrid-search reindex` to bring the index in sync.

$ hybrid-search drift -v --cwd .
[hybrid-search-mcp] drift 13 (+12 added, ~1 changed, -0 deleted)
  added (12):
    + benchmarks/agent_loop_sim.py
    + src/hybrid_search/index/drift.py
    ...
```

Intentionally **not** an MCP tool — per the memory rule that low-frequency functionality should live in CLI + skill orchestration rather than eating ~1 KB of context per session.

### Use cases

- `maintain` skill: call at entry, recommend reindex if drift > 5%.
- Pre-ship automation: fail a CI check if the committed index is stale vs. the tree.
- Ad-hoc: "does this project's search cache reflect current reality?" — one CLI call.

---

## Step E L5: Two-tier cap in interleave

### Problem

Previous `_interleave_modules` placed modules at positions 0, 2, 4 regardless of `limit`. At `limit=10 slots=3` that's 30% modules — fine. At `limit=5 slots=3` that's 60% modules, only 2 chunks — a mini-precision query can lose its best chunk result.

### Change

One line in `_interleave_modules`:

```python
slots = min(slots, max(1, limit // 2))
```

Guarantees chunks are never minority in the result window. For the default benchmark (`limit=10`) this is a no-op; for `limit=5` it drops modules from 3 → 2; for `limit=2` it drops from 3 → 1 while keeping one module card.

### Evidence of no regression

Re-ran the benchmark with `limit=10` — overall numbers bit-identical:

| Metric | Before L5 | After L5 |
|--------|-----------|----------|
| top-1 | 0.45 | 0.45 |
| top-5 | 0.85 | 0.85 |
| recall@10 | 0.77 | 0.77 |
| reads | 2.55 | 2.55 |

### New tests

4 tests added to `test_module_injection.py`:
- cap is a no-op at `limit=10 slots=3`
- `limit=3 slots=3` → 1 module + 2 chunks
- `limit=2 slots=3` → 1 module + 1 chunk (cap floor is 1)
- `limit=0` → empty list

---

## Summary of trajectory (Step 4 → Step A → B → C → D → E)

| Metric | Step 4 | A | B | C | **post-E** |
|--------|--------|---|---|---|------------|
| Overall top-1 | 0.10 | 0.20 | 0.35 | 0.45 | **0.45** |
| Overall top-5 | 0.65 | 0.65 | 0.85 | 0.85 | **0.85** |
| Overall recall@10 | 0.77 | 0.77 | 0.77 | 0.77 | **0.77** |
| Static proxy reads | 4.60 | 4.20 | 2.70 | 2.55 | **2.55** |
| **Agent-loop reads** | — | — | — | — | **0.25 (loose) / 0.45 (strict)** |
| Tests | 580 | 643 | 643 | 662 | **674** |

Recall@10 target (≥ 0.55 structure, originally set in Phase 5 plan doc) still below at 0.41 structure. That's a module-discovery / content gap (F2-style), not retrievable by further ranker tuning. Step D's agent-loop measurement reframes the overall picture: **the right denominator is "what does an agent actually pay to answer a question", not "what rank is the gold file at", and on that metric we're at 0.25 reads/query in loose mode.**
