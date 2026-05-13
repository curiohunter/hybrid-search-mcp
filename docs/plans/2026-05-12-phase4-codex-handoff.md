# Phase 4 Codex Handoff — CLAUDE.md / AGENTS.md sentinel-marker template

**Plan:** `docs/plans/2026-05-01-router-and-quality-signals.md` (Phase 4 section)
**Branch suggestion:** `feature/router-phase4-claude-md-template`
**Previous phases (reference patterns):** `f31ccf3` (P1), `edfae35` (P2), `8a596a5` (P3)
**Do NOT commit.** Leave the working tree dirty so a human can review the diff before commit.

---

## Goal

Make `hybrid-search-mcp setup` write a versioned, idempotent routing block into `CLAUDE.md` (Claude projects) and `AGENTS.md` (Codex projects), bounded by `BEGIN`/`END` sentinel markers. Add a `--dry-run` flag that prints a unified diff without writing. Migrate the two legacy single-marker formats (`<!-- hybrid-search -->` in CLAUDE.md, `<!-- hybrid-search-mcp:codex-routing -->` in AGENTS.md) into the new `routing v1` pair on next `setup` run, with **zero manual user intervention**.

Then measure G4 via a manual replay document (no script automation).

---

## Pre-decided answers to open questions (do not deviate)

| Question | Answer |
|---|---|
| Legacy markers coexist with `routing v1`? | **No.** Migrate in place: detect legacy single-marker block, replace it with the `BEGIN/END routing v1` pair on next `setup`. |
| `--dry-run` as new command or flag? | **Flag** on existing `setup`. Matches `reindex --dry-run`, `prune --dry-run`, `install-hook --dry-run`. |
| G4 measurement: automate or manual? | **Manual.** One-time `benchmarks/router_replay_2026-05.md`. G3's gold-set already covers routing accuracy regression. |
| Force overwrite when user has edited inside the marker pair? | Detect via content hash mismatch; **abort with clear error** + suggest `setup --force` to overwrite. |

---

## What exists today (read these first)

- `src/hybrid_search/cli.py:154` — `_CLAUDE_MD_MARKER = "<!-- hybrid-search -->"` (single marker, regex-bounded to next `## ` heading)
- `src/hybrid_search/cli.py:160-183` — `_CLAUDE_MD_SECTION` (current routing block content; keep as the source of truth and extend, do not rewrite from scratch)
- `src/hybrid_search/cli.py:186-227` — `_ensure_claude_md(project_path)` (current install/update logic)
- `src/hybrid_search/cli.py:230-249` — `_remove_claude_md(project_path)`
- `src/hybrid_search/cli.py:4140+` — `cmd_setup(args)` entry point
- `src/hybrid_search/codex_hooks.py:17` — `_AGENTS_MARKER = "<!-- hybrid-search-mcp:codex-routing -->"`
- `src/hybrid_search/codex_hooks.py:355-371` — `_update_agents_md(path)` (current install — **not idempotent for replace, only appends if missing**)
- `src/hybrid_search/codex_hooks.py:389-441` — `install_codex_hook(...)` entry point
- `tests/test_codex_hooks.py` — existing test patterns for AGENTS.md
- `tests/test_cli_hook_install.py` — existing test patterns for CLAUDE.md / setup

---

## Required deliverables

### 1. New module: `src/hybrid_search/memory/routing_template.py`

Single source of truth for both CLAUDE.md and AGENTS.md routing-block management. **One algorithm, two callers.**

```python
"""Versioned, idempotent routing-block writer for CLAUDE.md / AGENTS.md.

Sentinel marker contract:
    <!-- BEGIN hybrid-search-mcp routing v1 -->
    ...managed content...
    <!-- END hybrid-search-mcp routing v1 -->

Legacy markers migrated on first run:
    CLAUDE.md: "<!-- hybrid-search -->" + regex-bounded section
    AGENTS.md: "<!-- hybrid-search-mcp:codex-routing -->" + regex-bounded section
"""

BEGIN_RE = re.compile(r"^<!-- BEGIN hybrid-search-mcp routing v(\d+) -->$", re.M)
END_RE   = re.compile(r"^<!-- END hybrid-search-mcp routing v(\d+) -->$", re.M)

CURRENT_VERSION = 1

LEGACY_CLAUDE_MARKER = "<!-- hybrid-search -->"
LEGACY_AGENTS_MARKER = "<!-- hybrid-search-mcp:codex-routing -->"


@dataclass(frozen=True)
class RoutingBlock:
    """Resolved block contents for a given target file."""
    target: Literal["claude", "agents"]
    body: str  # the managed content WITHOUT begin/end markers

    def render(self) -> str:
        return (
            f"<!-- BEGIN hybrid-search-mcp routing v{CURRENT_VERSION} -->\n"
            f"{self.body.strip()}\n"
            f"<!-- END hybrid-search-mcp routing v{CURRENT_VERSION} -->"
        )


def plan_update(existing: str, block: RoutingBlock) -> UpdatePlan:
    """Pure function: classify current state and return the proposed write.

    Returns UpdatePlan with one of these statuses:
      - "no_change"      : block already byte-identical
      - "fresh_install"  : neither legacy nor v1 markers present → append
      - "update"         : v1 markers present, body differs → replace between markers
      - "migrate_legacy" : legacy marker present → remove legacy block, append v1 block
      - "corrupted"      : exactly one of BEGIN/END present → abort
      - "version_mismatch": v2+ markers present → unsupported in this writer (future-proof)
    """
    ...


def apply_update(path: Path, block: RoutingBlock, *, force: bool = False,
                 dry_run: bool = False) -> ApplyResult:
    """Read path, compute plan_update, write if not dry_run.

    Returns ApplyResult { status, diff: str (unified, always populated),
                          written: bool }.
    """
    ...
```

**Behavior matrix**:

| Existing file state | `plan_update` status | Action |
|---|---|---|
| No file | `fresh_install` | Create file with block |
| File exists, no markers | `fresh_install` | Append block after a blank line |
| File has legacy single marker | `migrate_legacy` | Remove legacy section (use existing regex), append v1 block at original position |
| File has matching v1 BEGIN+END, identical body | `no_change` | No write |
| File has matching v1 BEGIN+END, different body | `update` | Replace content between markers |
| File has only BEGIN or only END | `corrupted` | Raise `RuntimeError` with clear remediation |
| File has v2+ markers | `version_mismatch` | Raise `NotImplementedError` (forward compat) |
| `force=True` and corrupted | (any) | Strip all `hybrid-search-mcp` markers, append fresh v1 block |

**Diff generation**: always populate `ApplyResult.diff` using `difflib.unified_diff` with `fromfile=str(path) + " (current)"` and `tofile=str(path) + " (proposed)"`. Used by both `--dry-run` and error messages.

### 2. Block content (managed section body)

Reuse the existing `_CLAUDE_MD_SECTION` body (lines 161-183 of `cli.py`), **plus** these two additions at the bottom:

```markdown
**자기 정당화 (Self-justify)**:
- 모든 검색 호출 직전, **한 문장으로 어떤 도구를 골랐고 왜인지** 말할 것.
- 예: "탐색형 질문이라 `mcp__hybrid-search__hybrid_search` 먼저 호출합니다."

**Confidence 계약 (weak → fallback)**:
- `hybrid_search` 응답의 `confidence: weak`이면 답하기 전에 `fallback_hint`에 적힌 대체 도구로 한 번 더 시도할 것.
- `strong`/`mixed`면 그대로 진행.
```

The AGENTS.md variant is the **same body**. Codex reads `mcp__hybrid-search__hybrid_search` the same way; there is no Claude-specific syntax in the block.

### 3. CLI changes — `cmd_setup` and argument parser

- Add `--dry-run` flag to the `setup` subparser.
- Add `--force` flag (only used to recover from corrupted state).
- Replace the two existing call sites:
  - `_ensure_claude_md(project_path)` → `apply_update(path=Path(project_path)/"CLAUDE.md", block=claude_block, dry_run=args.dry_run, force=args.force)`
  - `codex_hooks._update_agents_md(agents_path)` → same `apply_update` call with `agents_block`.
- On `--dry-run`: print every `ApplyResult.diff` (or "no change" line) and return without writing anywhere else either (also skip hook install, config writes, etc. — `--dry-run` is whole-`setup` scope).
- On `corrupted`: print remediation message and exit non-zero. Example:
  ```
  ERROR: CLAUDE.md routing block is corrupted (only BEGIN marker found).
  Remove the orphan marker manually and re-run setup, or pass --force to
  strip all hybrid-search-mcp markers and rewrite.
  ```

### 4. Migration semantics (legacy → v1)

When `plan_update` returns `migrate_legacy`:

1. Remove the legacy block (use existing regex from `_remove_claude_md` for CLAUDE.md; analogous logic for AGENTS.md — note: legacy AGENTS.md `_update_agents_md` was *never idempotent for replace*, so the legacy block is just "the line with the marker plus the H2 below it until the next blank line or EOF" — see `codex_hooks.py:355-371`).
2. Insert the new v1 block at the **same position** the legacy block occupied (preserve user's chosen location in the file).
3. Print: `CLAUDE.md: migrated legacy routing block to v1`.

After migration, calling `setup` again must produce `no_change`.

### 5. New tests

Create `tests/test_routing_template.py` covering:

- **Fresh install** — empty file, file without markers, file with H1 only.
- **No-change** — same body, same markers → no write, no diff.
- **Update** — same markers, body differs → diff matches expected, write happens.
- **Migrate** — legacy CLAUDE.md marker → v1 block at same position. Legacy AGENTS.md marker → v1 block.
- **Corrupted** — only BEGIN, only END → `RuntimeError` with remediation.
- **Force-corrupted** — corrupted file + `force=True` → clean rewrite.
- **Byte-preservation outside markers** — fixture with content before/after the block; assert bytes outside marker pair are byte-for-byte identical post-write.
- **AGENTS.md path** — same algorithm, different file → same guarantees.
- **Dry-run** — `dry_run=True` → diff returned, no write.

Extend `tests/test_cli_hook_install.py` with:

- `setup --dry-run` integration test: run on a temp dir with seeded CLAUDE.md, assert no file mutation, assert stdout contains unified diff lines.
- `setup` migration test: seed CLAUDE.md with legacy marker, run setup, assert v1 markers present and legacy markers absent.
- `setup --force` recovery test: corrupt the markers, assert default setup fails, then `setup --force` succeeds.

### 6. CHANGELOG entry

Under `## Unreleased` in `CHANGELOG.md`:

```markdown
### Added
- `setup --dry-run`: preview CLAUDE.md/AGENTS.md changes without writing.
- `setup --force`: recover from a corrupted routing block.

### Changed
- CLAUDE.md and AGENTS.md routing sections now use versioned sentinel
  markers (`<!-- BEGIN/END hybrid-search-mcp routing v1 -->`). Existing
  installs migrate automatically on the next `setup`.
- Routing block now includes a self-justify rule (one-sentence tool
  choice per call) and a weak-confidence fallback contract.
```

### 7. G4 measurement — `benchmarks/router_replay_2026-05.md`

Manual one-shot. Document format:

```markdown
# Router Replay — 2026-05

**Baseline:** measurements with `HYBRID_SEARCH_ROUTER=0` and the legacy
single-marker CLAUDE.md (before this phase).
**Treatment:** router on + v1 marker block installed.

## Source: valuein field report v2 (4 win cases)

For each case, record:
- prompt (verbatim)
- baseline: which tool the model picked first, why
- treatment: which tool the model picked first, why
- correct? (per valuein retrospective)

### Case 1: <prompt>
| | tool picked | rationale |
|---|---|---|
| baseline | grep | (no router hint) |
| treatment | hybrid_search | router suggested hybrid_search (NL flow) |
| correct first-pick? | ✅ |

...

## Summary
- Baseline first-pick correctness: X/4
- Treatment first-pick correctness: Y/4
- G4 target (≥ 90 %): ✅ / ❌
```

This is honest, non-automated. Mark **G4 ✅** in HANDOFF.md only when treatment column shows ≥ 90 % correct first-pick (i.e. 4/4 or 3/4 + justified).

---

## Acceptance checklist (every box must be checked before saying done)

- [ ] `src/hybrid_search/memory/routing_template.py` exists with `plan_update`, `apply_update`, `RoutingBlock`, `ApplyResult` (or equivalent dataclasses).
- [ ] `cmd_setup` calls `apply_update` for both CLAUDE.md and AGENTS.md; legacy `_ensure_claude_md` and `_update_agents_md` are deleted or thin shims that delegate.
- [ ] `setup --dry-run` flag exists, prints diffs, writes nothing.
- [ ] `setup --force` flag exists, recovers corrupted state.
- [ ] Re-running `setup` on a clean install produces no diff (byte-identical).
- [ ] Legacy CLAUDE.md (single marker) migrates to v1 on next `setup`; legacy AGENTS.md likewise.
- [ ] Corrupted file (one of BEGIN/END) aborts with remediation message and non-zero exit.
- [ ] `tests/test_routing_template.py` exists with all cases listed in section 5.
- [ ] `tests/test_cli_hook_install.py` extended with dry-run/migration/force tests.
- [ ] `tests/test_codex_hooks.py` extended or updated where AGENTS.md handling changes.
- [ ] `pytest -q` is **green** (currently 934 tests; expect ~950+ after).
- [ ] `CHANGELOG.md` updated under `## Unreleased`.
- [ ] `benchmarks/router_replay_2026-05.md` exists with 4 valuein cases filled in (Treatment column must be measured by hand-running prompts in a Claude session against this repo or valuein).
- [ ] Working tree dirty; **no commit**.

---

## Anti-patterns to avoid

- Do NOT introduce a new MCP tool. CLI/hook surface only (per `feedback_no_new_mcp_tools.md`).
- Do NOT silently overwrite when corrupted — always require `--force`.
- Do NOT duplicate the block content for AGENTS.md and CLAUDE.md. **One body, two writes.**
- Do NOT change `cli.py:_CLAUDE_MD_SECTION` to also include BEGIN/END markers — the new `RoutingBlock.render()` wraps the body. The legacy constant can be deleted once the new module owns the body string.
- Do NOT touch anything outside the marker pair. The test fixture must prove this byte-for-byte.
- Do NOT add CI for `benchmarks/router_replay_2026-05.md` — it's a one-shot honest measurement, not a recurring gate.

---

## Quick context — first 5 minutes

1. Read `docs/plans/2026-05-01-router-and-quality-signals.md` D4 (sentinel-marker algorithm).
2. Read `src/hybrid_search/cli.py:154-249` (current CLAUDE.md handling).
3. Read `src/hybrid_search/codex_hooks.py:17, 355-371` (current AGENTS.md handling).
4. Read `~/.claude/projects/-Users-ian-project-claude-project-hybrid-search-mcp/memory/feedback_no_new_mcp_tools.md` (constraint).
5. Read `~/.claude/projects/.../memory/project_valuein_field_report_v2.md` (G4 source data — 4 win cases).

---

## When you're done

Update `HANDOFF.md`:
- Mark G4 status (✅ or partial)
- Bump test count
- Set next-session entry to "Phase 4 complete — review diff and commit, then close plan"
- Move `docs/plans/2026-05-01-router-and-quality-signals.md` to `docs/plans/completed/` **only after the human commits**, not before.

Leave working tree dirty. Print a one-paragraph summary of what changed, test count delta, and any deviations from this handoff.
