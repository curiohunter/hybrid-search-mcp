"""Typed memory schema — write-time classification (P1-1).

MemGuard (arXiv 2605.28009, preprint): in their error analysis, the
dominant share of "answered when it shouldn't have" failures traces to
write-time contamination — heterogeneous memories (events, facts, rules)
stored as interchangeable evidence. The read-time confidence contract
(false-strong 0/27) treats the symptom; this module types memories at
the moment they are written so downstream ranking and confidence can
tell an executed observation from a model guess.

Two frontmatter fields on every new qa record:

- ``memory_type`` — what kind of thing this memory is:
  observation / decision / hypothesis / task_state / procedure /
  review_finding
- ``verification`` — how much to trust it:
  verified (backed by executed evidence) / accepted (user approved) /
  inferred (model reasoning, the conservative default) /
  needs_revalidation (anchored code changed since — set by P1-2) /
  superseded (a newer answer exists — set by the integrity layer)

The classifier is deliberately heuristic and HIGH-PRECISION-BIASED: a
wrong "verified" label is the exact write-time contamination this exists
to prevent, so everything uncertain lands on ``inferred``. Legacy
records without the fields stay untyped and keep today's ranking
behavior — a mass retroactive "inferred" stamp would silently demote the
whole existing corpus.
"""

from __future__ import annotations

import re
from typing import Iterable

MEMORY_TYPES = (
    "observation", "decision", "hypothesis",
    "task_state", "procedure", "review_finding",
)
VERIFICATIONS = (
    "verified", "accepted", "inferred", "needs_revalidation", "superseded",
)

# Tools whose presence means the turn actually executed something.
_EXEC_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit"}

# Short approval turns — the user green-lighting a previously proposed
# plan. Deliberately narrow: any Korean imperative ends in "~해줘", so
# matching that alone would classify most REQUESTS as decisions. An
# approval must LEAD with an affirmation ("응 진행해"), be a bare
# go-ahead ("진행해", "시작하자"), or select a proposed option
# ("1번으로 해줘").
_AFFIRM_LEAD_RE = re.compile(
    r"^\s*(?:응|네|넵|예|좋아|그래|좋습니다|오케이|ok(?:ay)?|yes|yeah)\b",
    re.IGNORECASE,
)
_BARE_GO_RE = re.compile(
    r"^\s*(?:진행해줘?|진행하자|진행해\s*주세요|시작해줘?|시작하자|시작|"
    r"고|go|proceed|do it)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_SELECTION_RE = re.compile(r"^\s*\d+\s*번?\s*(?:으로|부터)?\s*(?:진행)?해?줘?\s*$")
_APPROVAL_MAX_CHARS = 40


def _is_approval(q: str) -> bool:
    if len(q) > _APPROVAL_MAX_CHARS:
        return False
    return bool(
        _AFFIRM_LEAD_RE.match(q) or _BARE_GO_RE.match(q) or _SELECTION_RE.match(q)
    )

# Review-shaped turns from the Codex reviewer lane.
_REVIEW_RE = re.compile(
    r"리뷰|검증|평가|판정|blocker|request changes|lgtm|검토",
    re.IGNORECASE,
)

# Completion claims in the answer — these become task_state records so a
# later session asking "what's done?" retrieves state, not prose.
_COMPLETION_RE = re.compile(
    r"완료|끝났|머지|배포했|커밋했|passed|merged|deployed|shipped",
    re.IGNORECASE,
)

# Executed-evidence markers: only these earn ``verified``, and only when
# an execution tool actually ran this turn.
_EVIDENCE_RE = re.compile(
    r"\d+\s*(?:passed|/\s*\d+\s*(?:passed|통과))|테스트.{0,12}통과|"
    r"all tests? pass|exit code 0",
    re.IGNORECASE,
)

# How-to answers worth reusing as procedures.
_PROCEDURE_RE = re.compile(
    r"어떻게\s+(?:설치|설정|실행|사용)|설치\s*방법|how\s+(?:do\s+i|to)\s+"
    r"(?:install|set\s*up|configure|run)",
    re.IGNORECASE,
)


def classify(
    *,
    query: str,
    answer_excerpt: str | None,
    tools_used: Iterable[str] = (),
    trigger: str | None = None,
    client: str | None = None,
) -> tuple[str, str]:
    """(memory_type, verification) for a new qa record.

    Order matters: the earlier rules are the higher-precision ones.
    """
    q = (query or "").strip()
    answer = (answer_excerpt or "").strip()
    tools = set(tools_used or ())

    if q.startswith("<"):
        # Machine payloads (task notifications, hook events) — events we
        # observed, never facts a model asserted.
        return "observation", "inferred"
    if trigger == "mcp_tool":
        # A search log: the record IS the retrieval observation.
        return "observation", "inferred"
    if client == "codex" and (_REVIEW_RE.search(q) or _REVIEW_RE.search(answer[:400])):
        return "review_finding", "inferred"
    if _is_approval(q):
        return "decision", "accepted"
    if _PROCEDURE_RE.search(q):
        return "procedure", "inferred"
    if tools & _EXEC_TOOLS:
        verified = bool(_EVIDENCE_RE.search(answer))
        mtype = "task_state" if _COMPLETION_RE.search(answer) else "observation"
        return mtype, ("verified" if verified else "inferred")
    return "hypothesis", "inferred"
