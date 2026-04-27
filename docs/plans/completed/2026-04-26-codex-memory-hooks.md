# 2026-04-26 — Codex Memory Hooks

**Author**: Codex planning session
**Status**: plan draft

---

## Problem statement

Hybrid Search MCP's memory layer currently assumes Claude Code's hook
contract. The search/indexing core is MCP-client agnostic, but the
deterministic memory loop is not:

- `UserPromptSubmit` retrieval is implemented for Claude Code hook payloads.
- `SessionStart` recent-memory injection is implemented for Claude Code hook
  payloads.
- `Stop` save is implemented for Claude Code's transcript/session contract.
- Setup writes `~/.claude.json`, `.claude/settings*.json`, and `CLAUDE.md`.

Codex supports MCP servers and a hooks feature, so the product can support
the same memory guarantee for Codex users. The work is an adapter, not a
rewrite: keep the existing indexing/search/qa storage logic and add a Codex
hook surface that speaks Codex's stdin/stdout protocol.

Reference: https://developers.openai.com/codex/hooks

---

## Goals

### Must-have

- **G1** Codex can call the existing `hybrid_search` MCP tool via
  `hybrid-search-mcp serve`.
- **G2** Codex `UserPromptSubmit` auto-searches relevant past Q&A/code for
  exploratory prompts and injects `additionalContext`.
- **G3** Codex `SessionStart` injects recent Q&A summary on startup/resume.
- **G4** Codex `Stop` persists every answered turn to `.hybrid-search/qa/`,
  independent of whether Codex used the MCP tool.
- **G5** Claude Code behavior remains unchanged.
- **G6** Setup is idempotent and non-destructive for both user-scope and
  project-scope Codex configuration.
- **G7** Tests cover Codex hook parsing, output envelopes, save dedup, and
  no-op behavior when required fields are missing.

### Nice-to-have

- **G8** `AGENTS.md` routing section analogous to the existing `CLAUDE.md`
  routing section.
- **G9** `hybrid-search-mcp status` reports Codex MCP/hook health alongside
  Claude health.
- **G10** A smoke-test command that runs the hook handler against fixture
  Codex payloads.
- **G11** README has a "Codex Integration" section with install and recovery
  commands.

### Non-goals

- Do not replace Claude Code hooks.
- Do not introduce extra MCP tools for admin operations.
- Do not change the qa markdown format in a backward-breaking way.
- Do not depend on Codex internal SQLite/session files.
- Do not require network access during hook execution except the existing
  embedding/search path already required by Hybrid Search.

---

## Current architecture to preserve

The core loop is already separated well enough to reuse:

```
src/hybrid_search/
  index/                 # scanner, chunker, embeddings, modules, call graph
  search/                # BM25 + vector + RRF + memory boost
  memory/qa_log.py       # markdown persistence
  memory/reader.py       # qa listing/stats/prune helpers
  hooks.py               # Claude Code hook adapter
  cli.py                 # setup, reindex, qa commands
  server.py              # MCP stdio server
```

Codex support should add a sibling adapter:

```
src/hybrid_search/codex_hooks.py
```

The new adapter should call shared functions or small extracted helpers rather
than duplicating the full Claude hook implementation.

---

## Codex hook mapping

| Memory behavior | Claude hook today | Codex hook target | Notes |
|---|---|---|---|
| Recent memory at session start | `SessionStart` | `SessionStart` | Use `source=startup|resume`; skip `source=clear`. |
| Search before model sees prompt | `UserPromptSubmit` | `UserPromptSubmit` | Use `prompt` from Codex payload. |
| Save after final answer | `Stop` | `Stop` | Use `last_assistant_message`; persist best-known user prompt. |
| Tool route nudges | `PreToolUse` | deferred | Not required for deterministic memory; not suitable for v1 context injection. |

The critical guarantee is covered by `UserPromptSubmit` + `Stop`. Even if
Codex `PreToolUse` coverage differs from Claude Code, retrieval-before-answer
and save-after-answer are enough to preserve the compounding memory loop.

---

## Proposed CLI surface

### `codex-hook`

Reads one Codex hook JSON payload from stdin and writes a Codex hook response
to stdout.

```
hybrid-search-mcp codex-hook
```

Responsibilities:

- Detect `hook_event_name`.
- Resolve project root from payload cwd or current working directory.
- For `SessionStart`, emit recent memory context.
- For `UserPromptSubmit`, classify prompt and optionally emit search context.
- For `Stop`, save a qa log entry.
- Never crash Codex sessions; on internal errors, return a permissive no-op
  response and log to stderr.

### `install-codex-hook`

Installs or updates Codex config idempotently.

```
hybrid-search-mcp install-codex-hook --cwd .
hybrid-search-mcp install-codex-hook --user
hybrid-search-mcp install-codex-hook --cwd . --target local
```

Responsibilities:

- Ensure Codex hooks are enabled explicitly:

  ```toml
  [features]
  codex_hooks = true
  ```

- Register the MCP server using Codex's TOML schema, not Claude's JSON
  `mcpServers` schema:

  ```toml
  [mcp_servers.hybrid-search]
  command = "/path/to/python"
  args = ["-m", "hybrid_search.cli", "serve"]
  ```

- Write hook configuration to `hooks.json`, not `config.toml`.
- Prefer project hooks at `.codex/hooks.json`; support user hooks at
  `~/.codex/hooks.json`.
- Preserve unrelated user configuration.
- Add or update an `AGENTS.md` hybrid-search routing section.
- Hook commands embed the current `sys.executable` path, mirroring the
  v0.3.0 `_hook_command()` helper used by `install-memory-hook`. This avoids
  the login-shell PATH failure mode fixed in commit `abce5eb`.

Only MCP server registration belongs in `config.toml`. Hooks belong in
`hooks.json`, which is closer to Claude Code's `settings.json` structure and
keeps the installer simpler.

T0 must still verify that the installed Codex CLI version loads both files as
expected. If a local Codex version has the known MCP loading regression
tracked in `openai/codex#3441`, the installer must report the failure and
offer a fallback instead of claiming success.

Project-local hooks only load when Codex trusts the project `.codex/` config
layer. The installer/status command must distinguish "written" from "loaded":
writing `.codex/hooks.json` is not sufficient unless a smoke test proves Codex
loads it for the current repository.

### Hook config target

Recommended project-scoped hooks file:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/python -m hybrid_search.cli codex-hook",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/python -m hybrid_search.cli codex-hook",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/python -m hybrid_search.cli codex-hook",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Do not use `PreToolUse` for v1 memory injection. Codex currently parses some
`PreToolUse` output fields that are not supported for context injection and
fail open. `UserPromptSubmit` and `Stop` are the deterministic memory path.

### Codex research checkpoints

Current plan assumptions are grounded in these Codex references:

- Hook discovery, feature flag, concurrent matching hook behavior, and event
  output semantics: <https://developers.openai.com/codex/hooks>
- Generated hook wire schemas:
  <https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated>
- MCP config shape and CLI/IDE shared config:
  <https://developers.openai.com/codex/mcp>
- AGENTS.md discovery, `AGENTS.override.md`, and `project_doc_max_bytes`:
  <https://developers.openai.com/codex/guides/agents-md>

---

## Hook response contract

Codex docs describe `hookSpecificOutput.additionalContext` as the way to
inject context for `SessionStart` and `UserPromptSubmit`. The adapter should
centralize response creation so the code can adjust cleanly if the exact JSON
envelope changes.

Expected shape:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "..."
  }
}
```

The implementation should keep this in one helper:

```python
def codex_context_response(event: str, text: str) -> dict: ...
def codex_noop_response() -> dict: ...
```

Use JSON responses, not plain stdout, for all events. `UserPromptSubmit` can
add plain stdout as context, but JSON is easier to test against the generated
schemas. `Stop` is stricter: when the hook exits successfully, plain stdout is
invalid and the no-op response must be valid JSON, normally `{}`.

Do not rely on shell fallbacks such as `|| true` to hide failures. The
`codex-hook` CLI entrypoint itself must catch internal errors, log diagnostics
to stderr, and print the event-appropriate JSON no-op response. This matters
most for `Stop`, where an empty successful stdout can be interpreted as an
invalid hook response.

---

## Shared logic extraction

To avoid two hook implementations drifting, extract reusable pieces from
`hooks.py` into a small shared module:

```
src/hybrid_search/memory/hook_runtime.py
  classify_prompt_for_memory(prompt) -> bool
  build_user_prompt_context(project_root, prompt, limit=10) -> str
  build_session_context(project_root) -> str
  record_completed_turn(project_root, prompt, answer, trigger, tools_used=()) -> Path | None
```

Claude Code's `hooks.py` and Codex's `codex_hooks.py` must both depend on this
in v0.5.0. Do not ship separate classifier or context-builder implementations
in the two hook files; that creates avoidable behavior drift.

---

## Data model

Keep qa frontmatter compatible with v0.3/v0.4:

```yaml
trigger: "codex_stop_hook"
tools_used: []
answer_chars: 1234
client: "codex"
```

`client` is additive and optional. Existing readers must tolerate its absence.

In the Codex flow, only `Stop` writes to `qa_log`. `UserPromptSubmit` injects
context and writes the pending-prompt runtime file but does not call
`qa_log.record()`. This differs from the Claude flow where `UserPromptSubmit`
can double-save and relies on the 5s dedup window.

For `Stop`, Codex provides the answer directly as `last_assistant_message:
string | null`. Do not port Claude Code's transcript JSONL parser into the
Codex adapter.

The remaining open question is how to recover the matching user prompt.
Preferred order:

1. Use a `prompt`/`last_user_message` field if Codex includes one.
2. Read a small project-local pending-turn file written by `UserPromptSubmit`.
3. If neither exists, save only when enough metadata is present; otherwise no-op.

Pending-turn file:

```
.hybrid-search/runtime/codex-last-prompt.json
```

It should be ignored by git and overwritten every prompt. Store only minimal
data: prompt text, timestamp, cwd, and a short hash.

Writes are atomic: write `codex-last-prompt.json.tmp`, then rename it to
`codex-last-prompt.json`. This prevents partial reads when a `Stop` hook fires
concurrently with the next `UserPromptSubmit`.

`UserPromptSubmit` writes the pending prompt before running search so that a
search failure does not prevent `Stop` from saving the turn.

`Stop` saves only when both values are available:

- query: pending prompt or payload prompt
- answer: `last_assistant_message`

---

## Implementation stages

| Stage | Deliverable | Est |
|---|---|---|
| **T0** | Confirm Codex hook payload/response fixtures and MCP loading locally | 45 min |
| **T0.5** | Extract shared `hook_runtime.py` used by both Claude and Codex hooks | 1 h |
| **T1** | Add `codex_hooks.py` with `SessionStart`, `UserPromptSubmit`, `Stop` no-op-safe handlers | 1.5 h |
| **T2** | Add `codex-hook` CLI command and fixture tests | 1 h |
| **T3** | Implement pending prompt handoff for `Stop` save | 1 h |
| **T4** | Add `install-codex-hook` writer for `hooks.json` + Codex TOML MCP config | 1.5 h |
| **T5** | Add `AGENTS.md` routing section generation/update | 45 min |
| **T6** | Extend `status` with Codex MCP/hook checks | 45 min |
| **T7** | README/CHANGELOG docs | 30 min |
| **T8** | End-to-end manual smoke test in a throwaway repo | 45 min |

Expected total: 9-10 hours.

### T0 fixture capture

Before implementation, capture real hook payloads from the installed Codex CLI
and commit sanitized fixtures under `tests/fixtures/`:

```
tests/fixtures/codex_session_start_startup.json
tests/fixtures/codex_session_start_resume.json
tests/fixtures/codex_session_start_clear.json
tests/fixtures/codex_user_prompt_submit.json
tests/fixtures/codex_stop.json
```

Minimal fixture procedure:

```bash
mkdir -p /tmp/codex-fixture/.codex
cd /tmp/codex-fixture
cat > .codex/hooks.json <<'EOF'
{"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"python3 -c 'import pathlib,sys; pathlib.Path(\"/tmp/ups-payload.json\").write_text(sys.stdin.read()); print(\"{}\")'"}]}]}}
EOF
cat > .codex/config.toml <<'EOF'
[features]
codex_hooks = true
EOF
codex
cat /tmp/ups-payload.json
```

Repeat for `SessionStart` and `Stop`. For `Stop`, explicitly verify that a
successful no-op hook prints `{}` and that plain stdout is not treated as a
valid response.

Also verify project trust behavior: project-local `.codex/hooks.json` should
load only after Codex trusts the repository config layer, while user-level
`~/.codex/hooks.json` remains active independently.

Finally, register a throwaway MCP server in Codex TOML and verify
`codex mcp list` plus an actual session can see it. This specifically guards
against local versions affected by `openai/codex#3441`.

---

## Test plan

Unit tests:

- `test_codex_hook_user_prompt_submit_injects_context`
- `test_codex_hook_user_prompt_submit_skips_precision_prompt`
- `test_codex_hook_session_start_injects_recent_memory`
- `test_codex_hook_session_start_skips_clear_source`
- `test_codex_hook_stop_records_turn_from_pending_prompt`
- `test_codex_hook_stop_noops_without_answer`
- `test_codex_hook_stop_noop_prints_valid_json`
- `test_codex_hook_stop_is_only_qa_log_writer`
- `test_codex_pending_prompt_write_is_atomic`
- `test_codex_hook_never_blocks_on_bad_json`
- `test_install_codex_hook_preserves_existing_config`
- `test_install_codex_hook_is_idempotent`
- `test_install_codex_hook_writes_hooks_json`
- `test_install_codex_hook_enables_feature_flag`
- `test_install_codex_hook_writes_toml_mcp_server`
- `test_install_codex_hook_does_not_duplicate_existing_hybrid_search_hooks`

Integration checks:

```
python -m pytest tests/test_codex_hooks.py tests/test_qa_log.py -q
python -m pytest tests/ -q
```

Manual smoke:

1. Create a temporary project.
2. Run `hybrid-search-mcp index . --force`.
3. Run `hybrid-search-mcp install-codex-hook --cwd .`.
4. Start Codex in that project.
5. Ask an exploratory question.
6. Verify prompt context includes hybrid-search memory.
7. Let Codex answer.
8. Verify `.hybrid-search/qa/` has a `trigger: codex_stop_hook` entry.
9. Reindex and verify that qa log appears as `node_type="qa_log"`.

---

## Risks and mitigations

### R1: Codex hook config file location differs from expectation

Mitigation: use `.codex/hooks.json` / `~/.codex/hooks.json` for hooks and
`config.toml` only for MCP. Stage T0 captures real fixtures from the installed
CLI and adjusts the installer before writing user config.

### R1.5: Project-local hooks are written but not trusted/loaded

Mitigation: status must report both file presence and actual Codex loading
evidence. Manual smoke must cover the trusted project path. If project trust is
missing, warn that `.codex/hooks.json` exists but may not run.

### R2: `Stop` payload lacks the matching user prompt

Mitigation: use Codex `last_assistant_message` for the answer; persist only the
prompt during `UserPromptSubmit` to a project-local runtime file and consume it
during `Stop`.

### R3: Hook output envelope differs from Claude Code

Mitigation: Centralize Codex response formatting and keep the first release
behind a small smoke-test command.

For `Stop`, the no-op path must print valid JSON (`{}`) on stdout. Do not
swallow command failures with `|| true`, because that can convert a crash into
a successful hook with invalid empty output.

### R4: Hook search adds latency

Mitigation: Reuse existing exploratory-prompt classifier, skip slash/mention
and precision prompts, cap injected results, and time out to no-op.

### R5: Duplicate saves when Codex explicitly calls MCP search

Mitigation: keep `Stop` as the only automatic Codex qa-log writer. Reuse the
existing qa_log dedup window only for collisions between explicit MCP search
saves and the `Stop` save, and mark triggers distinctly (`mcp_tool` vs
`codex_stop_hook`).

Codex loads all matching hooks from all active hook sources; higher-precedence
layers do not replace lower-precedence hooks. The installer must update an
existing hybrid-search hook entry instead of appending another one.

### R6: Hooks silently do not fire

Mitigation: installer must set `[features].codex_hooks = true`; status must
report whether the feature flag is enabled; smoke tests must prove a dummy
hook receives payloads.

### R7: Codex MCP registration silently fails

Mitigation: use Codex TOML `[mcp_servers.hybrid-search]`, verify with
`codex mcp list`, and document fallback behavior for local CLI versions
affected by the known MCP loading regression.

### R8: AGENTS.md content is ignored or truncated

Mitigation: detect `AGENTS.override.md` and warn that it takes precedence.
Keep the hybrid-search section small and warn when `AGENTS.md` approaches
Codex's project-doc byte ceiling (`project_doc_max_bytes`, default 32 KiB).

---

## Acceptance criteria

- `hybrid-search-mcp codex-hook` handles all three target events from fixtures.
- `hybrid-search-mcp install-codex-hook --cwd .` can be run twice without
  duplicating config.
- `install-codex-hook` enables `[features].codex_hooks = true`.
- `install-codex-hook` writes hooks to `.codex/hooks.json` or
  `~/.codex/hooks.json`.
- `status` reports whether project-local hooks are actually loadable for the
  current trusted Codex project layer, not just whether the file exists.
- `Stop` no-op response is valid JSON (`{}`), and the hook command does not
  depend on shell `|| true` fallbacks.
- MCP registration uses Codex TOML `[mcp_servers.hybrid-search]`.
- Codex exploratory prompts receive hybrid-search context before answering.
- Codex completed turns are written to `.hybrid-search/qa/`.
- `SessionStart` with `source=clear` injects no memory.
- Existing Claude Code setup/tests remain green.
- Full test suite passes.

---

## Open questions

- Does the installed Codex CLI version load both project `.codex/hooks.json`
  and user `~/.codex/hooks.json`?
- What is the most reliable non-interactive way to prove the current project
  `.codex/` layer is trusted and project-local hooks are loaded?
- Does the installed Codex CLI version successfully load stdio MCP servers
  registered in TOML, or is it affected by `openai/codex#3441`?
- Does Codex `Stop` always include `last_assistant_message` in local CLI mode?
- Does Codex include tool usage in the hook payload, or should `tools_used`
  stay empty for the first release?
- Should Codex setup register only hooks, or also register the MCP server by
  default?

These should be answered in T0 before implementation starts.
