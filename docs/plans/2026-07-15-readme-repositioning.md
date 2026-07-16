# README 첫 화면 재설계 — Evidence-Grounded Development Memory

**Status:** PROPOSED — 2026-07-15
**원칙:** 기능 이름 나열 금지(경쟁자 전부 같은 명사를 씀). 검증 가능한 약속 + 실측
숫자 + 실패 공개. "유일" 계열 형용사 전면 금지.

---

## 1. 포지셔닝 문구

**한 줄 (영어, hero):**

> **Evidence-grounded memory for multi-agent software development.**

**부제 (한 문단):**

> A local-first memory layer that carries plans, decisions, and review
> findings between Claude Code and Codex — and checks every memory
> against your current code and commits before handing it over.

**세 가지 약속 (제품 계약):**

> - **Remember what was decided.** Conversations, plans, commits, code,
>   and docs — one index, captured automatically.
> - **Verify what is still true.** Newer facts supersede stale ones;
>   memories anchored to changed code get flagged, not repeated.
> - **Abstain when evidence is insufficient.** Every answer carries a
>   calibrated confidence label — measured on frozen holdouts, failures
>   published.

**금지 문구 (그 이유와 함께):**

| 쓰지 않는다 | 이유 |
|---|---|
| "the only / first memory with supersession" | Zep/Graphiti bi-temporal, Engram 등 동시 진행 다수 — 사실이 아님 |
| "cross-agent memory" 단독 강조 | claude-mem·agentmemory·memsearch가 이미 같은 명사를 사용. 차별점은 명사가 아니라 **verified handoff** |
| "hybrid search" 리드 | 커모디티. 수단이지 가치가 아님 |
| 벤치 최고점 주장 | Mem0의 게임. 우리는 프로토콜(frozen holdout, 실패 공개)로 승부 |

---

## 2. README 첫 화면 구조 (스크롤 1.5회 이내)

```markdown
# Memory Layer MCP

**Evidence-grounded memory for multi-agent software development.**

A local-first memory layer that carries plans, decisions, and review
findings between Claude Code and Codex — and checks every memory against
your current code and commits before handing it over.

- **Remember what was decided.** Conversations, plans, commits, code, docs — one index, auto-captured via hooks.
- **Verify what is still true.** Newer facts supersede stale ones. Memories anchored to changed code get flagged.
- **Abstain when evidence is insufficient.** Calibrated strong/mixed/weak labels, measured on frozen holdouts.

## Why this exists

You plan with Claude Code, review with Codex (or the other way around),
and every new session starts with re-explaining what was decided, what
got rejected, and what's still open. Worse: a memory system that replays
*stale* decisions as facts is more dangerous than no memory at all.

This tool exists to make the handoff trustworthy, not just persistent.

## The numbers (including the failures)

Frozen-holdout protocol: answers written before indexing, no post-hoc
edits, failures published. Full reports in `benchmarks/`.

| corpus | role | supersession | adversarial | false-strong |
|---|---|---|---|---|
| valuein (KO) | dev | 6/6 | 3/3 | 0/9 |
| httpx (EN) | burned dev | 1/6 → **6/6** | 2/3 | 0/9 |
| ripgrep (EN) | **holdout** | synthetic 6/6 · planted 3/5 | 2/3 | 0/9 |

Known open failures, in the open: R1 (stale exact-match crowding),
ADV3 (Korean probe over English memories). Both are the current
work-in-progress — see docs/plans/.

## Quickstart — both agents in 5 minutes

    pip install memory-layer-mcp
    memory-layer setup            # Claude Code: MCP + hooks
    memory-layer setup --codex    # Codex: plugin + hooks, same memory root
    memory-layer doctor           # smoke test: write from one, recall from the other

## When NOT to use this

- Team-shared memory across machines → not this (local directory based).
- No API key at all → try Aider repo-map.
- `grep` already answers your questions → keep using grep.
```

---

## 3. 반영 규칙

1. 수치는 `benchmarks/` 산출물과 **단일 소스**로 연결 — README에 수동 복사된
   숫자가 벤치 재실행과 어긋나면 CI가 잡도록 (calibration_report와 연동,
   P1-3 CC-T4).
2. "5 minutes" 문구는 P0-3 CX-T5(맥미니 실측) 통과 전까지 커밋하지 않는다 —
   그 전에는 "one command per agent"로.
3. `docs/why.md`의 origin story와 톤 일치 (과장 없는 1인칭 실증).
4. 기존 SNS 초안 3종(Show HN, r/LocalLLaMA, r/ClaudeAI)의 제목/첫 문단을 이
   포지셔닝으로 갱신 — 특히 Show HN 제목:
   > Show HN: Evidence-grounded memory for Claude Code + Codex —
   > supersedes stale facts, abstains without evidence, failures published
5. 적용 순서: 이 문서 승인 → README PR (독립) → SNS 초안 갱신 커밋 → 게시.
