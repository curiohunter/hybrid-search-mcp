# Q1 — Routing Hook 구현 플랜

**Sprint:** 1.5일
**목표:** Claude가 Grep/Glob 호출 직전에 wiki 리마인더를 자동으로 보게 만든다.
**전략적 배경:** "Memory Layer for Claude Code" 포지셔닝의 **자율 루프 축** 시작점. 이 훅이 없으면 라우팅 표가 CLAUDE.md에만 있어 Claude가 무시하고 Grep으로 직행함.

---

## 기존 훅 인프라 (그대로 유지)

`src/hybrid_search/cli.py` `cmd_setup` (line 1726~1890)에 이미 설치 로직 존재.

`~/.claude/settings.json`의 PreToolUse에 3개 훅:

| 훅 | 매처 | 용도 |
|----|------|------|
| `auto_index_hook` | `Read` | 첫 파일 읽을 때 백그라운드 인덱싱 트리거 |
| `stale_hook` | `Edit\|Write` | 수정 전 STALE.md 경고 출력 |
| `gaps_hook` | `Read\|Edit\|Write` | wiki-gaps.txt 1회 표시 (`.gaps-shown` marker) |

Identity 필터 (1840~1846)는 command 문자열의 substring으로 기존 훅을 식별:
- `"hybrid-search/wiki"`
- `"STALE.md"`
- `"wiki-gaps"`

재설치 시 기존 걸 제거하고 새로 append → idempotent.

---

## 추가할 것 — `route_hook`

### Hook 정의

```python
route_hook = {
    "matcher": "Glob|Grep",
    "hooks": [{
        "type": "command",
        "command": (
            'ROOT=$(git rev-parse --show-toplevel 2>/dev/null) && '
            '[ -n "$ROOT" ] && [ -f "$ROOT/.hybrid-search/wiki/index.md" ] && '
            'echo \'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            '"additionalContext":"hybrid-search: 이 프로젝트에 wiki 인덱스가 있습니다 '
            '(.hybrid-search/wiki/index.md). 구조/관계/설계 질문은 wiki 먼저 확인. '
            '한국어 자연어 질의는 mcp__hybrid-search__hybrid_search 사용. '
            '다른 프로젝트 참조 시 project 파라미터 지원."}}\''
        ),
    }],
}
```

**Gate 조건:** `.hybrid-search/wiki/index.md` 존재 여부.
- 있으면 JSON 방출 → Claude가 추가 컨텍스트로 받음
- 없으면 훅 no-op (조용히 종료)

**Scope:** 현재 프로젝트만. Cross-project는 MCP tool `project` 파라미터로 별도 처리.

### Identity 필터 확장

`cli.py:1840~1846` 패턴에 추가:

```python
new_pre = [h for h in pre_hooks if not (
    isinstance(h, dict) and (
        "hybrid-search/wiki" in str(h.get("hooks", [{}])[0].get("command", ""))
        or "STALE.md" in str(h.get("hooks", [{}])[0].get("command", ""))
        or "wiki-gaps" in str(h.get("hooks", [{}])[0].get("command", ""))
        or "hybrid-search: 이 프로젝트" in str(h.get("hooks", [{}])[0].get("command", ""))  # ← route_hook
    )
)]
new_pre.extend([auto_index_hook, stale_hook, gaps_hook, route_hook])
```

### Has-check 확장

`cli.py:1774~1789`의 `has_auto_index`/`has_stale_check`/`has_gaps_check` 패턴 따라가서:

```python
has_route_hook = any(
    "hybrid-search: 이 프로젝트" in str(h.get("hooks", [{}])[0].get("command", ""))
    for h in pre_hooks
)

if has_auto_index and has_stale_check and has_gaps_check and has_route_hook:
    print(f"Hooks already registered: {settings_path}")
```

---

## Phase 2 — `hybrid-search-mcp status`

### 출력 예시

```
Global (~/.claude/):
  ✓ MCP server registered         (~/.claude.json)
  ✓ Settings file exists          (~/.claude/settings.json)
  ✓ PreToolUse hooks: 4/4         (auto_index, stale, gaps, route)
  ✓ Skills: 7 installed           (~/.claude/skills/)
  ✓ API key configured            (OPENAI_API_KEY)

Project (/Users/ian/project/value-in-homepage):
  ✓ Index (.hybrid-search/)       8,231 chunks, last reindex 2h ago
  ✓ Wiki                           42 pages (3 stale)
  ✓ post-commit hook installed    (.git/hooks/post-commit)
  ⚠ .gitignore missing wiki entry (run: hybrid-search-mcp install-hook)
  ✓ CLAUDE.md routing present
```

### 체크 함수 구조

```python
def cmd_status(args: argparse.Namespace) -> None:
    print("Global (~/.claude/):")
    _check_mcp_registered()
    _check_settings_exists()
    _check_pretool_hooks()   # 4개 개별 체크
    _check_skills_installed()
    _check_api_key()
    
    print("\nProject (<cwd>):")
    _check_index()
    _check_wiki()
    _check_post_commit_hook()
    _check_gitignore()
    _check_claude_md()
```

각 체크 함수는 3-5줄. 전체 `cmd_status`는 ~100줄.

---

## Phase 3 — `.gitignore` 자동 추가

`cmd_install_hook` (cli.py:1893~) 끝부분에 추가:

```python
def _ensure_gitignore_entries(project_root: Path) -> None:
    """Ensure .hybrid-search/ artifacts are git-ignored."""
    gi = project_root / ".gitignore"
    required = [
        ".hybrid-search/wiki/",
        ".hybrid-search/wiki-gaps.*",
        ".hybrid-search/coverage.json",
        ".hybrid-search/.reindex.lock",
    ]
    existing = gi.read_text() if gi.exists() else ""
    missing = [e for e in required if e not in existing]
    if missing:
        block = "\n# hybrid-search (auto-added)\n" + "\n".join(missing) + "\n"
        gi.write_text(existing.rstrip() + "\n" + block)
        print(f"Added {len(missing)} entries to .gitignore")
```

`.gitignore`에 이미 해당 라인이 있으면 skip.

**주의:** 현재 `.hybrid-search/wiki/*.md` 파일들이 이미 git에 추적 중. `install-hook` 자체는 추적 해제하지 않음. 사용자가 직접 `git rm --cached -r .hybrid-search/wiki/` 실행해야 함 (이전 세션 대화 참조). 추후 별도 마이그레이션 메시지로 안내.

---

## Phase 4 — 테스트 + 문서

### 테스트 (tests/test_cli_hook_install.py)

1. **신규 설치:** 빈 `settings.json`에 `cmd_setup` 실행 → 4개 훅 생김
2. **Idempotent:** 재실행 → 4개 훅 그대로 (중복 없음)
3. **기존 훅 보존:** 관련없는 PreToolUse 훅(예: 다른 도구)이 있을 때 → 그건 그대로, 우리 것만 교체
4. **Gate 검증:** `.hybrid-search/wiki/index.md` 없는 폴더에서 훅 command 실행 → 아무 출력 없음
5. **Gate 통과:** 파일 있는 폴더에서 → JSON 출력 포함

### 스킬 문서 업데이트

- `~/.claude/skills/setup-hybrid-search/SKILL.md` Step 4 뒤에 "route_hook으로 grep 전 wiki 리마인더 자동 주입" 한 줄 추가
- `~/.claude/skills/search.md` 상단에 "**Claude가 Grep/Glob을 부를 때 이 스킬의 wiki 우선 원칙이 자동으로 주입됩니다**" 추가

---

## 파일 변경 요약

| 파일 | 변경 |
|------|------|
| `src/hybrid_search/cli.py` | `route_hook` 추가, identity 필터 확장, `cmd_status` 신설, `_ensure_gitignore_entries` 추가 |
| `tests/test_cli_hook_install.py` | 신규 (5개 테스트) |
| `~/.claude/skills/setup-hybrid-search/SKILL.md` | 한 줄 추가 |
| `~/.claude/skills/search.md` | 상단 메모 추가 |
| `CHANGELOG.md` | entry 추가 |

예상 diff 규모: ~300줄 (테스트 제외하면 ~180줄).

---

## 완료 조건

- [ ] `hybrid-search-mcp setup` 실행 시 `~/.claude/settings.json`에 4개 훅 생성
- [ ] 재실행 시 훅이 정확히 4개 유지됨 (중복 없음)
- [ ] 새 프로젝트에서 Claude가 Grep을 호출하면 `additionalContext`에 wiki 안내가 주입됨
- [ ] `.hybrid-search/wiki/index.md` 없는 프로젝트에서는 Grep이 영향 없이 동작
- [ ] `hybrid-search-mcp status`가 4개 훅 상태를 각각 리포트
- [ ] `install-hook` 실행 시 `.gitignore`에 wiki 관련 엔트리 자동 추가
- [ ] 모든 테스트 통과

---

## 다음 단계 (이 스프린트 후)

Q1 완료 후 자연스럽게 이어지는 작업:
- **Q7 — CLAUDE.md 자동 주입** (프로젝트별 `./CLAUDE.md`에 라우팅 섹션)
- **Q8 — core.hooksPath 존중** (post-commit hook 설치 시 Husky 호환)
- **M2 — post-checkout 훅 추가** (브랜치 스위치 자동 reindex)
- **M4 — needs_synthesis flag** (stale wiki 알림 개선)

위 4개가 "자율 루프" 축의 완성.
