"""Versioned, idempotent routing-block writer for CLAUDE.md / AGENTS.md."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BEGIN_RE = re.compile(r"^<!-- BEGIN hybrid-search-mcp routing v(\d+) -->$", re.M)
END_RE = re.compile(r"^<!-- END hybrid-search-mcp routing v(\d+) -->$", re.M)

CURRENT_VERSION = 1

LEGACY_CLAUDE_MARKER = "<!-- hybrid-search -->"
LEGACY_AGENTS_MARKER = "<!-- hybrid-search-mcp:codex-routing -->"

# The routing block is machine-owned: every write replaces it wholesale. That
# made the most natural place to write a project rule (right under the routing
# table) a place where rules silently died on the next reindex. The block now
# carries a human-owned sub-region that survives updates, and any hand-written
# line found in the machine-owned part is moved into it instead of dropped.
USER_BEGIN = "<!-- BEGIN hybrid-search-mcp user-additions -->"
USER_END = "<!-- END hybrid-search-mcp user-additions -->"
USER_HINT = (
    "<!-- 이 구역만 사람이 쓴다 — 도구 업데이트는 여기를 덮어쓰지 않는다.\n"
    "     위 표나 규칙에 프로젝트 고유 항목을 더하려면 이 안에 적을 것. -->"
)

_USER_REGION_RE = re.compile(
    r"(?ms)^" + re.escape(USER_BEGIN) + r"\n(.*?)^" + re.escape(USER_END) + r"\n?"
)

ROUTING_BODY = """## 검색 전략 — 반드시 이 순서로

이 프로젝트는 `hybrid-search-mcp` Memory Layer가 설치돼 있다. **아래 규칙을 예외 없이 지킬 것.**

| 질문 유형 | 신호 | **반드시 먼저 호출** | 보충 |
|---|---|---|---|
| **기능 탐색** | "어떤 기능", "관련 기능", "어떻게 구성", "흐름", "설명해줘", "정리해줘", "아키텍처" | `mcp__hybrid-search__hybrid_search` | Grep, Read |
| **설계/맥락** | "왜 이렇게", "배경", "이유", "결정", "히스토리", "지난번" | `mcp__hybrid-search__hybrid_search` | Wiki |
| **스키마/DB** | "테이블", "마이그레이션", "DDL", "스키마 변화" | `mcp__hybrid-search__hybrid_search` (file_pattern `*.sql`) | Grep |
| **구조/관계** | "전체 그림", "누가 호출", "의존" | Wiki (`.hybrid-search/wiki/index.md`) | `mcp__hybrid-search__hybrid_search` |
| **정밀 조회** | 정확한 심볼명 / 파일명 / 에러 문자열 | Grep | Read |

**운영 규칙**:
- **탐색형 질문에 Grep 먼저 호출 금지** — 반드시 `mcp__hybrid-search__hybrid_search` 먼저.
- MCP 서버가 많은 환경에선 이 도구가 **deferred**(스키마 미로드)일 수 있다 — 직접 호출이 실패하면 `ToolSearch`(query `select:mcp__hybrid-search__hybrid_search`)로 로드한 뒤 다시 호출할 것. Grep으로 이탈 금지.
- **쿼리는 사용자의 자연어 문장을 그대로** 쓸 것 — 키워드 뭉치로 재작성 금지.
  (예: "우리 환불 기능에 대해 알려줘" ⭕ / "환불 퇴원 refund 워크플로우 정산" ❌)
  자연어 문장이 벡터 매칭 품질이 더 좋고, 분류기가 가중치를 자동 조정한다.
- 1차에서 답이 부족해도 도구를 **바꾸지 말고 같은 레인에서 보충** (hybrid→wiki MCP 레인, grep→read 텍스트 레인).
- Wiki는 `.hybrid-search/wiki/index.md`에서 시작, `[[링크]]` 있으면 따라갈 것.

**자동 동작 (수동 개입 불필요)**:
- 질문 시작 시 관련 과거 Q&A 자동 컨텍스트 주입 (UserPromptSubmit)
- 세션 시작 시 최근 Q&A 요약 주입 (SessionStart)
- 답변 종료 시 `.hybrid-search/qa/`에 자동 저장 (Stop)
- `git commit` 후 변경 파일만 재인덱싱 + 좀비 wiki 자동 삭제

**자기 정당화 (Self-justify)**:
- 모든 검색 호출 직전, **한 문장으로 어떤 도구를 골랐고 왜인지** 말할 것.
- 예: "탐색형 질문이라 `mcp__hybrid-search__hybrid_search` 먼저 호출합니다."

**Confidence 계약 (weak → fallback)**:
- `hybrid_search` 응답의 `confidence: weak`이면 답하기 전에 `fallback_hint`에 적힌 대체 도구로 한 번 더 시도할 것.
- `strong`/`mixed`면 그대로 진행.
"""


@dataclass(frozen=True)
class RoutingBlock:
    """Resolved block contents for a given target file."""

    target: Literal["claude", "agents"]
    body: str

    def render(self, user_body: str = "") -> str:
        user = user_body.strip("\n")
        region = f"{USER_BEGIN}\n{USER_HINT}\n"
        if user.strip():
            region += f"\n{user}\n"
        region += USER_END
        return (
            f"<!-- BEGIN hybrid-search-mcp routing v{CURRENT_VERSION} -->\n"
            f"{self.body.strip()}\n\n"
            f"{region}\n"
            f"<!-- END hybrid-search-mcp routing v{CURRENT_VERSION} -->"
        )


@dataclass(frozen=True)
class UpdatePlan:
    status: Literal[
        "no_change",
        "fresh_install",
        "update",
        "migrate_legacy",
        "corrupted",
        "version_mismatch",
    ]
    current: str
    proposed: str
    message: str = ""
    preserved: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyResult:
    status: str
    diff: str
    written: bool
    preserved: tuple[str, ...] = ()


def claude_block() -> RoutingBlock:
    return RoutingBlock(target="claude", body=ROUTING_BODY)


def agents_block() -> RoutingBlock:
    return RoutingBlock(target="agents", body=ROUTING_BODY)


def _append_block(existing: str, rendered: str) -> str:
    if not existing:
        return rendered + "\n"
    return existing.rstrip("\n") + "\n\n" + rendered + "\n"


def _legacy_pattern(target: str) -> re.Pattern[str]:
    if target == "claude":
        return re.compile(
            re.escape(LEGACY_CLAUDE_MARKER) + r"\n## [^\n]+\n.*?(?=\n## |\Z)",
            flags=re.DOTALL,
        )
    return re.compile(
        re.escape(LEGACY_AGENTS_MARKER) + r"\n## [^\n]+\n.*?(?=\n\n|\Z)",
        flags=re.DOTALL,
    )


def _strip_all_hybrid_markers(existing: str) -> str:
    text = re.sub(
        r"(?ms)^<!-- BEGIN hybrid-search-mcp routing v\d+ -->\n.*?"
        r"^<!-- END hybrid-search-mcp routing v\d+ -->\n?",
        "",
        existing,
    )
    text = _legacy_pattern("claude").sub("", text)
    text = _legacy_pattern("agents").sub("", text)
    lines = [
        line for line in text.splitlines()
        if "hybrid-search-mcp" not in line and LEGACY_CLAUDE_MARKER not in line
    ]
    return "\n".join(lines).strip("\n")


def _split_user_region(inner: str) -> tuple[str, str]:
    """Split a block's inner text into (machine-owned, human-owned)."""
    match = _USER_REGION_RE.search(inner)
    if not match:
        return inner, ""
    managed = inner[: match.start()] + inner[match.end() :]
    user = match.group(1).replace(USER_HINT, "")
    return managed, user.strip("\n")


def _is_marker_line(line: str) -> bool:
    stripped = line.strip()
    return (
        "hybrid-search-mcp routing" in stripped
        or "hybrid-search-mcp user-additions" in stripped
        or stripped in {LEGACY_CLAUDE_MARKER, LEGACY_AGENTS_MARKER}
    )


def _rescue_lines(managed: str, reference: str) -> list[str]:
    """Lines of the machine-owned part that no template ever wrote.

    ``reference`` is the union of the body we are about to write and the body
    we last wrote (when known), so a template upgrade does not mistake its own
    retired lines for something a human typed.
    """
    known = {line.strip() for line in reference.splitlines() if line.strip()}
    rescued: list[str] = []
    seen: set[str] = set()
    for line in managed.splitlines():
        stripped = line.strip()
        if not stripped or stripped in known or stripped in seen or _is_marker_line(line):
            continue
        seen.add(stripped)
        rescued.append(line.rstrip())
    return rescued


def _merge_user_body(user_body: str, rescued: list[str]) -> str:
    existing = {line.strip() for line in user_body.splitlines() if line.strip()}
    new = [line for line in rescued if line.strip() not in existing]
    parts = [part for part in (user_body.strip("\n"), "\n".join(new)) if part.strip()]
    return "\n\n".join(parts)


def _snapshot_path(path: Path, target: str) -> Path:
    return path.parent / ".hybrid-search" / "runtime" / f"routing-body-{target}.md"


def _read_snapshot(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_snapshot(path: Path, body: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass


def plan_update(
    existing: str,
    block: RoutingBlock,
    previous_body: str | None = None,
) -> UpdatePlan:
    """Classify the current file state and return the proposed write.

    Anything a human wrote inside the block is carried into the human-owned
    sub-region of the new block; the tool never drops lines it did not write.
    """

    rendered = block.render()
    reference = block.body + "\n" + (previous_body or "")
    begins = list(BEGIN_RE.finditer(existing))
    ends = list(END_RE.finditer(existing))

    if len(begins) != len(ends):
        marker = "BEGIN" if begins else "END"
        return UpdatePlan("corrupted", existing, existing, f"only {marker} marker found")

    if begins and ends:
        begin = begins[0]
        end = ends[0]
        begin_version = int(begin.group(1))
        end_version = int(end.group(1))
        if begin_version != CURRENT_VERSION or end_version != CURRENT_VERSION:
            return UpdatePlan("version_mismatch", existing, existing)
        if end.start() < begin.end():
            return UpdatePlan("corrupted", existing, existing, "END marker appears before BEGIN")
        current_block = existing[begin.start():end.end()]
        managed, user_body = _split_user_region(existing[begin.end():end.start()])
        rescued = _rescue_lines(managed, reference)
        new_block = block.render(_merge_user_body(user_body, rescued))
        if current_block == new_block:
            return UpdatePlan("no_change", existing, existing)
        proposed = existing[:begin.start()] + new_block + existing[end.end():]
        return UpdatePlan("update", existing, proposed, preserved=tuple(rescued))

    legacy_match = _legacy_pattern(block.target).search(existing)
    if legacy_match:
        # No rescue here: a pre-v1 block carries no user sub-region and we keep
        # no snapshot of those bodies, so every line would look hand-written and
        # the whole retired routing table would land in the user region.
        proposed = existing[:legacy_match.start()] + rendered + existing[legacy_match.end():]
        if existing.endswith("\n") and not proposed.endswith("\n"):
            proposed += "\n"
        return UpdatePlan("migrate_legacy", existing, proposed)

    return UpdatePlan("fresh_install", existing, _append_block(existing, rendered))


def _diff(path: Path, current: str, proposed: str) -> str:
    if current == proposed:
        return ""
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
        )
    )


def _raise_for_plan(path: Path, plan: UpdatePlan) -> None:
    if plan.status == "corrupted":
        raise RuntimeError(
            f"{path.name} routing block is corrupted ({plan.message}).\n"
            "Remove the orphan marker manually and re-run setup, or pass --force to\n"
            "strip all hybrid-search-mcp markers and rewrite."
        )
    if plan.status == "version_mismatch":
        raise NotImplementedError(
            f"{path.name} has an unsupported hybrid-search-mcp routing marker version."
        )


def apply_update(
    path: Path,
    block: RoutingBlock,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> ApplyResult:
    """Read path, compute plan_update, and write unless dry-run."""

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    snapshot = _snapshot_path(path, block.target)
    plan = plan_update(existing, block, previous_body=_read_snapshot(snapshot))
    if force and plan.status in {"corrupted", "version_mismatch"}:
        proposed = _append_block(_strip_all_hybrid_markers(existing), block.render())
        plan = UpdatePlan("fresh_install", existing, proposed)
    else:
        _raise_for_plan(path, plan)

    diff = _diff(path, plan.current, plan.proposed)
    written = False
    if plan.proposed != plan.current and not dry_run:
        path.write_text(plan.proposed, encoding="utf-8")
        written = True
    if not dry_run and plan.status != "corrupted":
        _write_snapshot(snapshot, block.body)
    return ApplyResult(
        status=plan.status, diff=diff, written=written, preserved=plan.preserved
    )
