# Tool Router + Quality Signals

**Status:** ACTIVE — 2026-05-01
**Builds on:** `docs/plans/2026-04-26-memory-layer-10x-context-engineering.md` (memory write/cards path complete)
**Implementation:** Codex (this plan is the handoff document — must be self-contained)
**Goal:** close the AI tool-selection gap surfaced by the valuein field report so
that Claude/Codex reflexively pick the right retrieval tool (hybrid_search vs
grep vs memory) for each prompt, and recover gracefully when the first choice
returns weak results.

---

## Success Goals (must hit every one to call this plan complete)

| # | Metric | Target | Measured by |
|---|---|---|---|
| **G1** | Pre-fetch precision (% of hits whose `file_path` is under a code root, not content/binary) | **≥ 90 %** on a valuein-style content-heavy project | `benchmarks/prefetch_precision.py` (new, Phase 1) |
| **G2** | Confidence band correctness — `weak` precision/recall against ground-truth "should fall back" labels | **≥ 80 % both** on `benchmarks/router_gold.json` | `benchmarks/confidence_eval.py` (new, Phase 2) |
| **G3** | Router classification accuracy (suggested tool == labeled correct tool) | **≥ 80 %** on `benchmarks/router_gold.json` (~30 prompts) | `benchmarks/router_eval.py` (new, Phase 3) |
| **G4** | Re-run valuein-style multi-domain trace, count first-pick tool correctness with router enabled vs baseline | **≥ 90 %** first-pick correctness, baseline measured first | manual replay, results in `benchmarks/router_replay_2026-05.md` |
| **G5** | Pre-fetch additionalContext token budget | unchanged or smaller vs current 360-char ceiling | `tests/test_memory_hook.py` assertions |
| **G6** | Full test suite | **green** (currently 894 cases) + net-new tests per phase | `pytest -q` |

If any one goal slips, that phase does not ship.

---

## Current Situation

The valuein field report (2026-04-30, `project_valuein_field_report_v2.md`)
validated the "Memory Layer for Claude Code" thesis on a real session:

- 4 decisive wins for `hybrid_search` — all sharing the pattern *"business rule
  scattered across `services/`, `hooks/`, `app/`"*. Estimated 1.5–2x time saved.
- `grep` was faster for: single-file edits, exact symbol/path/error string,
  one-shot SQL.
- 3 systemic gaps observed:
  1. **AI grep bias** — even with the CLAUDE.md routing table, the model
     defaults to grep on prompts that should hit `hybrid_search` first.
  2. **Pre-fetch noise** — content folders (PDFs, docs, learning material) get
     indexed and surface as irrelevant pre-fetch hits, eroding trust.
  3. **No result-quality self-check** — the model can't tell whether a
     `hybrid_search` answer was strong or weak, so it does not fall back.

The 2026-04-26 plan delivered the *write* + *cards* path. This plan delivers
the *route* + *trust* path.

## Product Principle

The router never decides for the model. It exposes signals the model can act
on: a tool suggestion based on prompt shape, and a confidence score on every
search result. The model still chooses; the surface area just stops hiding
the right answer.

## Target Architecture

```
UserPromptSubmit hook
  -> heuristic router classifies prompt
       NL flow / "왜" / 분산 룰        -> suggest hybrid_search
       정확 심볼 / 파일 / 에러 문자열  -> suggest grep
       히스토리 / 지난번              -> suggest memory + wiki
  -> additionalContext: route hint + confidence band
       (uses pre-fetch data; warns when weak)

hybrid_search response
  -> includes top_score, score_gap, confidence band
  -> includes a "weak match -> consider grep for X" hint when low

reindex / scanner
  -> default file-pattern excludes content folders
       (.pdf, .epub, oversized .md books, configurable allowlist)
  -> existing CLI flags still override
```

---

## Decisions (settled — no more open questions)

### D1. Router gold set lives at `benchmarks/router_gold.json`

Co-locates with `valuein_gold.json` so a future CI job can run all gold-set
evals together. Schema:

```json
[
  {
    "id": "vi-001",
    "prompt": "saveStudentMonthlyPlan 흐름이 어떻게 되지",
    "expected_tool": "hybrid_search",
    "rationale": "함수명 + '흐름' = 도메인 흐름 추적",
    "source": "valuein-2026-04-30"
  }
]
```

`expected_tool` enum: `"hybrid_search" | "grep" | "memory" | "none"`.

Sourcing: harvest from `.hybrid-search/qa/*.md` across this repo + valuein,
hand-label retrospectively based on which tool actually produced the answer.
Strip duplicates and near-paraphrases. Target ~30 prompts (≥ 7 per class for a
±15 % accuracy estimate).

### D2. Hook output language: English verbs + project-language nouns

Reasoning: 360-char hook ceiling (`a1b7e44`); English verbs are short and
unambiguous; nouns/identifiers stay in their original form so they remain
grep-able.

**Format contract (every router-emitted line):**

```
[hybrid-search route] suggest <tool> · <reason>
pre-fetch confidence: <strong|mixed|weak> · <fallback hint when weak>
```

Example:

```
[hybrid-search route] suggest hybrid_search · NL flow signal
pre-fetch confidence: mixed · weak match → grep `paid_fee_guard`
```

### D3. Confidence bands: distribution-based, derived from the gold set

Calibration procedure (Phase 2 ships with the calibrated values):

1. Run every `benchmarks/router_gold.json` prompt where `expected_tool ==
   "hybrid_search"` through the search.
2. Plot `top_score` and `score_gap` distributions.
3. Pick percentile bands:
   - `strong` = top_score ≥ P67 **and** score_gap ≥ P67
   - `mixed`  = top_score ≥ P33 (and not in `strong`)
   - `weak`   = otherwise (or 0 hits, or score_gap is None and not in `strong`)
4. Persist the chosen thresholds in `config.toml`:

```toml
[router.confidence]
strong_score = 0.0XX   # filled in by calibration
strong_gap   = 0.0YY
weak_score   = 0.0ZZ
```

5. Provide `hybrid-search-mcp recalibrate` CLI (Phase 2) so projects with
   different corpus sizes can re-derive their own thresholds.

**Edge cases**:

- 0 hits → `weak`
- 1 hit (no gap) → `mixed`
- ties (`gap < 0.001`) → `weak`

**Reference implementation sketch:**

```python
def classify_confidence(top_score: float, gap: float | None,
                        thresholds: dict) -> str:
    if top_score == 0:
        return "weak"
    if gap is not None and gap < 0.001:
        return "weak"
    if (gap is not None
            and top_score >= thresholds["strong_score"]
            and gap >= thresholds["strong_gap"]):
        return "strong"
    if top_score >= thresholds["weak_score"]:
        return "mixed"
    return "weak"
```

### D4. CLAUDE.md update: idempotent block between named sentinel markers

Industry standard (Ansible `blockinfile`). Conda's known anti-pattern
(non-unique markers, stacking blocks — issue conda/conda#8703) is what we
explicitly avoid by versioning the marker.

**Marker pair** (Markdown comments — invisible in rendered view):

```markdown
<!-- BEGIN hybrid-search-mcp routing v1 -->
... managed content ...
<!-- END hybrid-search-mcp routing v1 -->
```

**Setup algorithm**:

```
1. Read CLAUDE.md (or AGENTS.md if Codex-only project).
2. Find both BEGIN and END markers (regex: ^<!-- (BEGIN|END) hybrid-search-mcp routing v\d+ -->$).
3. Three cases:
   a. Both markers present, same version → replace content between them.
   b. Both markers present, version mismatch → log migration, replace block AND markers.
   c. Exactly one marker present → abort with error: "CLAUDE.md routing block is corrupted; remove BEGIN/END markers manually and re-run setup".
   d. Neither marker → append block (with markers) to end of file, preceded by a blank line.
4. `setup --dry-run` prints proposed change as unified diff, no write.
5. Never touch lines outside the marker pair.
```

**What goes inside the block (managed content)**:

- The routing table (which tool for which prompt shape).
- Self-justification rule: "Before any retrieval call, state in one sentence
  which tool you picked and why."
- Confidence band contract: "If `confidence: weak`, fall back to the suggested
  alternative tool before answering."

---

## Phases (each one is a separate PR, in order)

### Phase 1 — Index-side noise filter

**Scope**

- Default exclusion list in `src/hybrid_search/index/scanner.py`:
  - File extensions: `.pdf`, `.epub`, `.docx`, `.pptx`, `.xlsx`, `.zip`,
    `.tar`, `.gz`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.mp3`, `.mp4`.
  - Markdown size cap: any `.md` larger than `content_md_max_bytes` (default
    `262144` = 256 KB). Applies only under common content roots:
    `docs/learning/`, `학습/`, `자료/`, `materials/`, `book/`, `책/`. Other `.md`
    paths (README, plan docs, design docs) are unaffected.
- Per-project override in `config.toml`:

```toml
[scanner.exclude]
extensions   = [".pdf", ".epub", ...]   # extends default
allow_paths  = ["docs/learning/keep-this.md"]   # forces include
content_md_max_bytes = 262144
content_roots = ["docs/learning", "학습", "자료"]
```

- New flag `reindex --include-content` opts back into the full scan.
- `doctor` command appends an "Excluded paths summary" section: count by
  reason (`extension`, `oversize_md`, `manual`).

**Acceptance**

- A `tests/test_scanner.py` case proves `.pdf`/`.epub` files are skipped.
- A test proves an oversized `.md` under `docs/learning/` is skipped, but a
  same-size `.md` under `docs/` is kept.
- `config.toml` allow_paths override is honored.
- `doctor` output regression test snapshots the new section.

**Goal hit:** **G1** (pre-fetch precision ≥ 90 %).

### Phase 2 — Result quality signals

**Scope**

- Extend the search response schema with three fields:
  - `top_score: float` — RRF score of hit #1 (0 if no hits).
  - `score_gap: float | None` — `score(#1) − score(#2)`, `None` if < 2 hits.
  - `confidence: "strong" | "mixed" | "weak"`.
- When `confidence == "weak"` add `fallback_hint: str` — the most distinctive
  token from the prompt, plus a suggested alternative tool ("grep" if the
  prompt has identifier-shape tokens, "wiki" otherwise).
- Surface the same fields in:
  - MCP tool response (`tools/hybrid_search.py`).
  - Pre-fetch hook output (`memory/hook_runtime.py::_format_user_prompt_context`).
- New CLI: `hybrid-search-mcp recalibrate` — runs the gold-set procedure from
  D3 and writes thresholds to `config.toml`.

**Acceptance**

- Existing benchmark queries return identical hit ordering (snapshot test).
- New tests assert: known good queries → `strong`, known no-hit queries →
  `weak`, single-hit queries → `mixed`, near-tie queries → `weak`.
- Pre-fetch hook output stays ≤ 360 chars including new field.
- `recalibrate` writes valid TOML; re-run is idempotent.

**Goal hit:** **G2** (`weak` precision/recall ≥ 80 %), **G5** (token budget).

### Phase 3 — Heuristic router (UserPromptSubmit)

**Scope**

- New module `src/hybrid_search/memory/router.py`:

```python
def classify_prompt(prompt: str) -> RouterDecision:
    """Returns (tool, reason) where tool ∈
    {"hybrid_search", "grep", "memory", "none"}."""
```

- Signal patterns (regex/keyword):
  - **grep** signals: backtick-wrapped identifier (`` `foo_bar` ``), CamelCase
    of length ≥ 8, file-extension token (`*.ts`, `*.py`), absolute/relative
    path, error/stack signature (`Error:`, `Traceback`, `at line`).
  - **hybrid_search** signals: Korean exploratory tokens (already enumerated in
    `hook_runtime.py::_EXPLORATORY_TOKENS_KO`) plus English equivalents
    (`why`, `how does`, `flow of`, `where is X handled`).
  - **memory** signals: `지난번`, `이전에`, `왜 이렇게 결정`, `last time`,
    `previously`, `earlier we decided`.
  - Otherwise → `hybrid_search` (current default).
- Hook output (UserPromptSubmit additionalContext):

```
[hybrid-search route] suggest <tool> · <reason>
pre-fetch confidence: <band> · <fallback hint when weak>
```

- Disabled via `HYBRID_SEARCH_ROUTER=0` (default on).
- Build `benchmarks/router_gold.json` with ≥ 30 hand-labeled prompts (D1).
- Build `benchmarks/router_eval.py` to compute classification accuracy from
  the gold set.

**Acceptance**

- `pytest tests/test_router.py` covers each signal pattern with both
  positive and negative cases.
- `python benchmarks/router_eval.py` reports accuracy ≥ 80 %.
- Hook output ≤ 360 chars.
- `HYBRID_SEARCH_ROUTER=0` disables the router; verified by hook test.

**Goal hit:** **G3** (router accuracy ≥ 80 %).

### Phase 4 — CLAUDE.md / AGENTS.md template + self-justify rule

**Scope**

- `setup` command writes/updates the routing block per D4 sentinel-marker
  algorithm. Same logic for `CLAUDE.md` (Claude projects) and `AGENTS.md`
  (Codex projects).
- Block content (versioned `v1`):
  - Routing table (compact form, ≤ 30 lines).
  - "Self-justify" rule: one sentence per retrieval call.
  - Confidence-band contract: weak → fall back.
- `setup --dry-run` prints unified diff before writing.
- CHANGELOG entry under `## Unreleased`.

**Acceptance**

- Setup on a fresh project creates the block with markers.
- Re-running setup is a no-op (no diff).
- Setup on a corrupted file (one marker only) aborts with a clear error.
- Setup preserves all content outside the marker pair (asserted byte-for-byte
  on a fixture).
- Tests for both `CLAUDE.md` and `AGENTS.md` paths.

**Goal hit:** **G4** (first-pick correctness ≥ 90 % in replay), **G6**
(suite green throughout).

---

## Non-goals

- New MCP tools. Per `feedback_no_new_mcp_tools.md`, low-frequency router
  helpers stay as CLI/hook surface.
- ML-based or learned routing. Heuristic only — calibration over a small
  hand-labeled gold set is the cheapest credible win.
- Cross-project router state (per-project only).
- Changing how `hybrid_search` ranks results. RRF + the existing classifier
  stay; this plan only adds metadata around the response.
- Auto-recalibration on every reindex. Recalibration is opt-in CLI.

## Risks

- **Over-correction**: router may push exact-symbol prompts to `hybrid_search`
  if regex patterns miss. Mitigated by gold-set accuracy gate (G3) and
  env-var off-switch.
- **Confidence drift**: thresholds tuned on the current gold set may degrade
  as the corpus grows. Mitigation: `recalibrate` CLI; re-measure annually or
  when corpus doubles.
- **Content-folder false negatives**: legitimate large `.md` design docs
  could be excluded. Mitigation: per-project `allow_paths`, doctor visibility,
  and the `content_roots` allow-list narrows the scope.
- **CLAUDE.md write conflict**: user may have edited inside the marker pair.
  Mitigation: setup detects and aborts (no silent overwrite); `--force` flag
  available for explicit overwrite.

## Order of Execution & Branching

Each phase = one branch + one PR. Suggested branch names:

1. `feature/router-phase1-noise-filter`
2. `feature/router-phase2-quality-signals`
3. `feature/router-phase3-heuristic-router`
4. `feature/router-phase4-claude-md-template`

Each PR must:

- include all listed acceptance tests,
- update CHANGELOG `## Unreleased`,
- leave the suite green (G6).

Phase 1 and 2 ship as additive metadata only (no behavior change for existing
callers). Phase 3 ships gated by `HYBRID_SEARCH_ROUTER` env var (default on,
easy off-switch). Phase 4 changes setup output and documents itself in
CHANGELOG.

## Definition of Done (this plan as a whole)

All six success goals (G1–G6) measurably hit, with their measurement scripts
checked in under `benchmarks/`. Plan moves to
`docs/plans/completed/2026-05-01-router-and-quality-signals.md` with a final
"Outcome" section appended (numbers actually achieved, links to the merge
commits, any deviations from the plan).
