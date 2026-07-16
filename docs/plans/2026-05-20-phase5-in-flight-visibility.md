# Phase 5 Candidate — In-flight Visibility for Uncommitted Changes

**Status:** PROPOSED — 2026-05-20
**Anchor case study:** `docs/case-studies/2026-05-20-payssam-v2-migration.md`
**Branch suggestion:** `feature/phase5-in-flight-visibility`
**Do NOT commit.** Leave the working tree dirty so a human can review the diff before commit.

---

## Goal

Make `hybrid_search` aware of the files currently being edited, before
they are committed and before the post-commit reindex hook runs.

The PaysSam V2 migration exposed the gap clearly: the index reflected the
last committed state, while Codex had 30+ modified files in flight. A
follow-up query about "what we just changed" could only see yesterday's
wiki/index reality. This phase closes that freshness gap without writing
dirty work into the persistent index.

Target behavior:

- `git diff HEAD` files are surfaced as ephemeral, query-time chunks.
- Ephemeral chunks are mixed into normal `hybrid_search` results with
  explicit metadata, not persisted to SQLite/Tantivy/vector storage.
- Dirty-file results outrank stale indexed chunks for the same file.
- Deleted files are suppressed from results when they are deleted in the
  working tree.

---

## Non-goals

- Do not build a filesystem watcher.
- Do not write uncommitted content into the DB or vector index.
- Do not add a new MCP tool.
- Do not make agents manually run `reindex` during normal edit loops.
- Do not try to solve external API freshness. Official docs remain the
  authority for time-conditioned policy.

`reindex --staged` can be useful later as a manual debug/escape hatch,
but it is not the main product behavior for this phase.

---

## Why This Matters

The current invalidation model is correct after commits:

```text
post-commit hook -> git delta -> reindex -> wiki/integrity tail
```

But long agent sessions often need search while the work is still dirty:

```text
edit 30 files -> ask follow-up -> search old index -> stale answer
```

That is exactly the shape Codex flagged in the PaysSam migration. The
right fix is not to make agents commit more often; it is to let the
search layer overlay the current worktree on top of the committed index.

Expected impact: the PaysSam execution score should move from about
6.5/10 to 8+ for codebase-local follow-up questions, while still keeping
official external specs outside the memory layer's responsibility.

---

## Existing Code To Read First

- `src/hybrid_search/search/orchestrator.py:413` — `SearchOrchestrator.hybrid_search`
- `src/hybrid_search/search/orchestrator.py:499` — chunk enrichment before memory/module injection
- `src/hybrid_search/search/orchestrator.py:580` — `_make_response` confidence metadata
- `src/hybrid_search/index/scanner.py:91` — `GitDiffResult`
- `src/hybrid_search/index/scanner.py:201` — `parse_git_diff_name_status`
- `src/hybrid_search/index/scanner.py:239` — `get_changed_files_from_git`
- `src/hybrid_search/tools/hybrid_search.py:40` — MCP handler and response serialization
- `src/hybrid_search/cli.py:4375` — post-commit hook uses `git diff --name-status`
- `tests/test_orchestrator.py` — orchestrator unit-test patterns
- `tests/test_cli_hook_install.py` — git-delta hook expectations

Useful existing primitive: `HybridResult` already has enough fields to
carry ephemeral chunks if we add clear metadata in `chunk_id`,
`node_type`, `content`, `snippet`, and `trust_meta`.

---

## Design

### D1. Query-time dirty overlay

When `hybrid_search(..., cwd=...)` resolves to exactly one project, inspect
the working tree with:

```bash
git diff --name-status HEAD
```

Use the existing parser, or a tiny wrapper around it, so the status
semantics stay aligned with post-commit delta indexing.

Only run the overlay when:

- `cwd` resolves to a known project.
- The project has a `.git` directory or `git diff` succeeds.
- The feature is enabled by config or default-on product decision.

Recommended default: **on**. This feature is query-time only and has no
persistent side effects.

### D2. Build ephemeral chunks from dirty files

For each added/modified/renamed-new path:

1. Normalize to a project-relative path.
2. Apply the same indexability rules used by scanner where practical.
3. Read the current file bytes from disk.
4. Skip binary or oversized files.
5. Chunk cheaply enough for query-time use.

Initial implementation can use one file-level chunk per dirty file:

```text
chunk_id: ephemeral:<project_id>:<sha256(rel_path + content_hash)>
node_type: in_flight_file
file_path: <relative path>
content: first bounded text window or extracted text
snippet: [in-flight] <path> ...
trust_meta: [in-flight dirty worktree; not indexed]
rrf_score: small positive score after local scoring
```

If a dirty file is large, take a bounded window and include a truncation
note in the snippet. This phase is about freshness, not perfect local
semantic indexing.

### D3. Score ephemeral chunks locally

Avoid embedding dirty content in Phase 5. That would add latency,
external calls, and ambiguity about whether uncommitted code is being
sent to an embedder.

Use a deterministic local score:

- Tokenize the query and file content.
- Boost exact path/name matches.
- Boost identifier-shaped query terms found in content.
- Boost Korean/English natural-language token overlap lightly.
- Cap the number of emitted ephemeral results.

Recommended cap:

- Inspect at most 50 dirty files.
- Return at most 5 ephemeral chunks before interleaving.
- Add a response metadata field later only if needed; first pass can
  expose this through `trust_meta` and `node_type`.

### D4. Merge policy

Merge ephemeral chunks after normal chunk enrichment and before memory
boost/module-card injection.

Rules:

- If a file is deleted in `git diff HEAD`, drop normal results whose
  `file_path` is that deleted path.
- If a file has an ephemeral result, drop older normal results for the
  same file unless the normal result is a module/memory card.
- Put matching ephemeral results before stale same-file indexed chunks.
- Keep the existing confidence contract. If all matches are weak, the
  response may still be `confidence: weak`.

This keeps the overlay honest: it corrects freshness, but does not
pretend dirty chunks are semantically embedded or externally verified.

### D5. Response visibility

The caller must be able to tell that a result came from the dirty
worktree. Use:

```text
node_type: in_flight_file
trust_meta: [in-flight dirty worktree; not indexed]
snippet prefix: [in-flight]
```

Do not hide this behind ranking alone. Agents need to know whether they
are looking at committed memory or current uncommitted work.

---

## Implementation Steps

### 1. Add dirty diff collector

New module suggestion:

```text
src/hybrid_search/search/in_flight.py
```

Public API:

```python
@dataclass(frozen=True)
class InFlightFile:
    relative_path: str
    status: Literal["added", "modified", "renamed"]
    content: str
    content_hash: str

@dataclass(frozen=True)
class InFlightOverlay:
    files: list[InFlightFile]
    deleted_paths: set[str]

def collect_in_flight_overlay(project_root: Path, *, max_files: int = 50) -> InFlightOverlay:
    ...
```

Use `git diff --name-status HEAD` and `parse_git_diff_name_status`.
Renames should treat the old path as deleted and the new path as added.

### 2. Add local dirty-file scoring

In the same module:

```python
def score_in_flight_files(
    overlay: InFlightOverlay,
    *,
    query: str,
    project_name: str,
    project_id: str,
    limit: int = 5,
) -> list[HybridResult]:
    ...
```

Keep this pure and easy to test. It should not touch the DB, registry,
embedder, or config.

### 3. Wire into `SearchOrchestrator.hybrid_search`

Only for single-project searches. The simplest wiring point is after:

```python
chunk_results = self._enrich_results(...)
```

and before:

```python
chunk_results = _apply_memory_boost(...)
```

Pseudo-flow:

```python
overlay = maybe_collect_overlay(cwd, project_infos)
if overlay:
    dirty_results = score_in_flight_files(...)
    chunk_results = merge_in_flight_results(
        chunk_results,
        dirty_results,
        deleted_paths=overlay.deleted_paths,
        limit=effective_limit,
    )
```

Keep cross-project search unchanged for Phase 5. A dirty overlay is
session-local and should not be guessed across projects.

### 4. Serialize through the MCP handler

`tools/hybrid_search.py` already serializes `node_type`, `content`,
`snippet`, and `trust_meta`. No schema change is required if the result
uses those existing fields.

Add tests to prove `trust_meta` survives sanitization.

### 5. Optional CLI escape hatch

Only after the query-time overlay is green, consider:

```bash
hybrid-search-mcp reindex --staged
```

This should be a separate small follow-up if the owner wants it. It is
not required for Phase 5 acceptance.

---

## Config Decision

Default-on is recommended.

Rationale:

- No DB write.
- No embedder call.
- No background process.
- Only affects searches scoped by `cwd` to the current project.
- The result is visibly marked as `[in-flight]`.

If a kill switch is desired, add:

```toml
[search.in_flight]
enabled = true
max_files = 50
max_results = 5
max_bytes_per_file = 200000
```

Do not block implementation on a rich config surface. Hard-coded safe
defaults are acceptable for the first pass if tests cover them.

---

## Tests

Create `tests/test_in_flight_overlay.py`:

- Parses added/modified/deleted/renamed git diff output through existing
  parser semantics.
- Collects dirty files from a temp git repo.
- Skips deleted paths and records them in `deleted_paths`.
- Skips binary files.
- Truncates oversized text files.
- Scores exact path matches above generic content overlap.
- Scores identifier-shaped query terms found in dirty content.
- Produces `HybridResult` with:
  - `node_type == "in_flight_file"`
  - `chunk_id.startswith("ephemeral:")`
  - `trust_meta` contains `in-flight`
  - `snippet` starts with `[in-flight]`

Extend `tests/test_orchestrator.py`:

- Dirty modified file replaces stale same-file indexed result.
- Dirty deleted file suppresses stale indexed result.
- No overlay is collected when `cwd` is absent.
- Cross-project search does not collect dirty overlays.
- Weak confidence behavior remains honest when dirty results have low
  local score.

Extend `tests/test_reranker.py` or MCP handler tests:

- `trust_meta` and `node_type` for `in_flight_file` survive the public
  tool response.

---

## Manual Verification

Use a temporary repo or a small fixture project:

1. Index a project with a function named `oldPayssamPartnerEndpoint`.
2. Modify the file without committing:

   ```ts
   export function createPayssamV2Payment() {
     return "/api/v2/payment/request";
   }
   ```

3. Run:

   ```bash
   hybrid-search-mcp search "createPayssamV2Payment endpoint" --cwd <repo>
   ```

4. Expected:
   - top results include the modified file as `[in-flight]`
   - `node_type` is `in_flight_file`
   - stale same-file indexed chunk is not shown above it

Then delete an indexed file without committing and verify it no longer
appears in top results for a query that used to return it.

---

## Acceptance Checklist

- [ ] `src/hybrid_search/search/in_flight.py` exists with overlay collection,
  local scoring, and merge helpers.
- [ ] `hybrid_search(..., cwd=...)` includes dirty added/modified/renamed
  files as ephemeral results for single-project searches.
- [ ] Deleted dirty files suppress stale indexed results.
- [ ] Dirty same-file results outrank or replace stale indexed chunks.
- [ ] Ephemeral results are visibly marked with `node_type="in_flight_file"`
  and `trust_meta` containing `in-flight`.
- [ ] No dirty content is written to SQLite/Tantivy/vector indexes.
- [ ] No embedder call is made for dirty file content.
- [ ] Cross-project search behavior is unchanged.
- [ ] Tests cover collector, scoring, merge, orchestrator wiring, and MCP
  serialization.
- [ ] `pytest -q` is green.
- [ ] `CHANGELOG.md` updated under `## Unreleased`.
- [ ] Working tree dirty; **no commit**.

---

## Risks

| Risk | Mitigation |
|---|---|
| Query-time `git diff` adds latency | Only run for one scoped project; cap dirty files; skip if git fails |
| Large dirty files slow search | Cap bytes per file and mark snippets as truncated |
| Binary/generated files leak noise | Reuse ignore/indexability rules and binary detection |
| Dirty local scoring beats better indexed results | Cap emitted results and require positive query overlap |
| Agents over-trust uncommitted content | Mark every result as `[in-flight]` and `not indexed` |
| Sensitive dirty content goes to embedder | Do not embed dirty content in Phase 5 |

---

## Open Questions

1. Should in-flight overlay be controlled by config from day one, or
   hard-coded default-on with safe caps?
2. Should untracked files be included via `git status --porcelain`, or
   only tracked dirty files from `git diff HEAD`?
3. Should staged and unstaged changes be distinguished in `trust_meta`?

Recommended answers for first implementation:

- Default-on.
- Include tracked dirty files only.
- Do not distinguish staged vs unstaged yet.

These choices keep the first slice small and directly address the
PaysSam gap without widening into a full worktree indexer.

