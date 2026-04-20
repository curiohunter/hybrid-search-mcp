# Hybrid Search MCP — Handoff Document

---

## 🔴 현재 세션 인계 (2026-04-20, 6회차) — 다음 세션 여기부터 읽을 것

### 한줄 요약

**Q10 + M2 + M3 연속 완료 🎉** — Quick Wins 10/10 + M 시리즈 착수(2개). 396/396 tests passed. 전체 **12/28 (43%)**. 다음 세션은 **M1 (Confidence 3단계 라벨 + numeric score)** 또는 **M4 (`needs_synthesis` flag)**.

### ✅ 이 세션 완료된 것 (6회차)

**M3: post-commit이 diff를 커밋 시점에 캡처 → env 전달** (M2 다음 커밋)
- 문제: post-commit 훅이 `nohup reindex --git-delta &`를 띄우면, 배경 프로세스가 시작될 때 `get_changed_files_from_git(HEAD~1..HEAD)`를 재계산. 사용자가 빠르게 2차 커밋하면 HEAD~1이 이동해서 1차 커밋의 변경을 놓침 (race).
- 해결: 훅이 `git diff --name-status HEAD~1 HEAD`를 **동기적으로** 캡처해 `HYBRID_SEARCH_CHANGED_STATUS` env로 export. `nohup bash -c` 자식 프로세스가 env 상속 → `cmd_reindex`가 내부 subprocess 호출 대신 env를 파싱.
- `src/hybrid_search/index/scanner.py`에 `parse_git_diff_name_status(raw: str)` public 파서 추출. `get_changed_files_from_git`도 재사용.
- `src/hybrid_search/cli.py` `cmd_reindex`: `--git-delta` 게이트 안에서 env 우선 체크 → 없으면 기존 subprocess 경로 fallback.
- `_HOOK_DIFF_ENV = "HYBRID_SEARCH_CHANGED_STATUS"` 상수로 관리.
- **초기 커밋 처리**: `git rev-parse HEAD~1` 실패 시 env 미설정 → `cmd_reindex`가 내부 fallback에서 `None` 받고 full scan 경로로 떨어짐 (기존 동작 유지).
- `scripts/post-commit-hook.sh` 레퍼런스도 동일 패턴 적용.
- **부수 효과**: subprocess 호출 1회 절약(~50ms) + race 방지.

**테스트 (M3 신규 8개):**
- `tests/test_scanner.py::TestParseGitDiffNameStatus` 5개 (empty/all-kinds/blank-lines/unknown-codes/rename-similarities)
- `tests/test_cli_hook_install.py::TestScriptContentSanity::test_post_commit_script_exports_hook_diff_env` 1개
- `tests/test_cli_hook_install.py::TestPostCommitDiffCapture` 2개 (실제 bash+git 통합: 2차 커밋 env export 검증, 초기 커밋 env 미설정 검증)

**M2: post-checkout 훅 추가** (`b4319bc`)
- `src/hybrid_search/cli.py`에 `_build_post_commit_script()`, `_build_post_checkout_script()`, `_install_hook_file()` 헬퍼로 리팩터. `cmd_install_hook`이 두 훅 동시 설치.
- **post-checkout 게이트**: `[ "$3" = "1" ] || exit 0` (브랜치 스위치만 트리거, 파일 체크아웃 skip) + `[ -d "$PROJECT_DIR/.hybrid-search" ] || exit 0` (인덱스 미존재 시 자동 부트스트랩 금지).
- **post-checkout 동작**: `reindex --wiki-scope affected` (NO `--git-delta`, NO `--synthesize`). 이유: `HEAD~1..HEAD`는 브랜치 스위치 후 무의미. 파일시스템 delta(size/mtime/hash) 사용. synthesis는 브랜치 스위치가 빈번하므로 비용 대비 효용 낮음.
- **공유 lock**: `.hybrid-search/.reindex.lock` — post-commit과 동일. 동시 2회 리인덱스 방지.
- **status 체크 확장**: `_check_project_status`가 post-commit + post-checkout 둘 다 표시.
- `scripts/post-checkout-hook.sh` 레퍼런스 파일 추가 (수동 설치용, 베이크 안 된 버전).
- **식별 마커**: `_HOOK_IDENTITY_MARKER = "hybrid_search.cli"` 상수로 추출. 레거시 설치도 동일 문자열 포함 → 기존 설치 자동 인식.

**Q10: `.hybrid-search-ignore` + upward walk** (`c71ddb1`)
- `src/hybrid_search/index/scanner.py`에 `_collect_hybrid_search_ignore_patterns(project_root)` 추가. `project_root`부터 위로 walk하며 각 레벨의 `.hybrid-search-ignore`를 읽음. `.git` 디렉토리 있는 레벨(포함) 또는 filesystem root에서 중단. `_build_ignore_spec()`이 config excludes + `.gitignore` + 수집된 패턴 3개 소스를 하나의 pathspec으로 병합.
- **포맷 선택:** graphify의 fnmatch 대신 **pathspec (gitignore 슈퍼셋)** 채택. 기존 `.gitignore` 파싱과 동일 엔진 → comments/blanks/negation(`!`) 네이티브 처리, 코드 중복 제거.
- **안전장치:** 파일당 64KB 상한 (`_GITIGNORE_MAX_SIZE` 재사용), walk 최대 32레벨(`_IGNORE_WALK_MAX_DEPTH`) — symlink 사이클 방어.
- **양방향 통합:** `scan_project`(full scan)와 `scan_project_subset`(git diff 경로) 둘 다 `_build_ignore_spec`/`_is_indexable_path`를 거쳐 Q10 패턴 존중.

**테스트:**
- Q10: `tests/test_scanner.py::TestHybridSearchIgnore` 8개
- M2: `tests/test_cli_hook_install.py`에 `TestInstallHookBothScripts`(5) + `TestPostCheckoutScriptGates`(3, 실제 bash 실행으로 게이트 검증) + `TestScriptContentSanity`(2) = 10개
- **388/388 passed** (이전 370 + Q10 8 + M2 10).

**마이그레이션 주의:**
- Q10은 opt-in — `.hybrid-search-ignore` 파일을 만들지 않으면 동작 동일.
- M2: 기존 프로젝트는 post-commit만 설치되어 있음. `hybrid-search-mcp install-hook --cwd .` 재실행하면 post-checkout만 추가로 설치됨 (post-commit은 idempotent skip). `cmd_setup` 자동 체크도 두 훅 모두 확인하므로 다음 setup 실행 시 자동 보강.

### ✅ 이 세션 완료된 것 (6회차)

**구현:**
- **Q10: `.hybrid-search-ignore` + upward walk** — `src/hybrid_search/index/scanner.py`에 `_collect_hybrid_search_ignore_patterns(project_root)` 추가. `project_root`부터 위로 walk하며 각 레벨의 `.hybrid-search-ignore`를 읽음. `.git` 디렉토리 있는 레벨(포함) 또는 filesystem root에서 중단. `_build_ignore_spec()`이 config excludes + `.gitignore` + 수집된 패턴 3개 소스를 하나의 pathspec으로 병합.
- **포맷 선택:** graphify의 fnmatch 대신 **pathspec (gitignore 슈퍼셋)** 채택. 기존 `.gitignore` 파싱과 동일 엔진 → comments/blanks/negation(`!`) 네이티브 처리, 코드 중복 제거.
- **안전장치:** 파일당 64KB 상한 (`_GITIGNORE_MAX_SIZE` 재사용), walk 최대 32레벨(`_IGNORE_WALK_MAX_DEPTH`) — symlink 사이클 방어.
- **양방향 통합:** `scan_project`(full scan)와 `scan_project_subset`(git diff 경로) 둘 다 `_build_ignore_spec`/`_is_indexable_path`를 거쳐 Q10 패턴 존중.

**테스트:** `tests/test_scanner.py::TestHybridSearchIgnore` 8개 추가 — local ignore, parent upward walk (모노레포), git 경계 차단, `.gitignore` 병합, 주석/빈줄, 미존재 no-op, 64KB 초과 skip, subset scan. **378/378 passed** (이전 370 + 신규 8).

**마이그레이션 주의:** Q10은 opt-in 기능 — `.hybrid-search-ignore` 파일을 만들지 않으면 동작 동일. 기존 인덱스 영향 없음. 단, 새 ignore 파일을 추가/수정하면 이전에 포함되던 파일이 exclude되어 다음 reindex에서 해당 chunks/embeddings가 DB에서 삭제됨 (정상 동작).

### 이전 세션 완료

**5회차 (2026-04-20):**
- Q6 Markdown frontmatter strip (f6f5938)

## 🔵 이전 세션 인계 — 참고용

### 한줄 요약 (5회차)

캐시 안정성 축 — Q6 (Markdown YAML frontmatter strip before hash) 완료. 370/370 tests passed. Quick Wins 9/10 (90%).

### 전략적 맥락 (중요)

이 작업은 graphify 전체 소스를 6-agent로 정밀 분석 (5,035줄 분석 문서 생성) 후 확정한 3축 전략의 실행 단계:

1. **자율 루프** — Claude가 안 쓸 수 없는 도구 (Q1, Q7, Q8, M2 등) ← 현재 여기
2. **Memory Layer** — 매 사용이 도구를 더 똑똑하게 (L1 Q&A feedback loop)
3. **벤치마크 주도 품질** — 숫자로 증명 (L6)

**포지셔닝 전환:** "코드 검색 도구" → "Claude Code의 영구 기억 레이어". Graphify와 경쟁하지 않고 보완재로 공존.

### ✅ 이 세션 완료된 것 (5회차)

**구현:**
- **Q6: Markdown frontmatter strip** — `src/hybrid_search/index/scanner.py`에 `_FRONTMATTER_RE = re.compile(rb"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)` + `_strip_frontmatter(raw)` helper 추가. `compute_file_hash(path)`가 `.md` 파일일 때만 `read_bytes()` → fm strip → sha256 (body only). 비-`.md`는 기존 streaming hash 유지.
- **파급 경로 (자동 전파):** `_is_changed` → `files.file_hash` (scanner.py/pipeline.py) → wiki `file_hash_at_compile` (storage/wiki.py:460 staleness 비교) → synthesis `source_hashes` (synthesizer.py:102). frontmatter-only edit이 모든 레이어에서 재처리 skip.

**테스트:** `tests/test_scanner.py::TestComputeFileHashFrontmatter` 8개 추가. fm 있/없 해시 동일, fm 수정 시 해시 불변, body 수정 시 해시 변경, body-level `---` horizontal rule 보존, CRLF fm 지원, 비-`.md` 비영향, 미닫힘 fm은 body로 취급. **370/370 passed** (이전 362 + 신규 8).

**마이그레이션 주의:** 배포 후 첫 reindex에서 모든 `.md` 파일 해시가 한 번 바뀌어 1회 재임베딩 발생 (이후 안정). Wiki DB의 `file_hash_at_compile`과 실제 `file_hash`가 대량 불일치 → wiki 전체가 stale로 마킹됨. 권장 순서: (1) 배포, (2) `hybrid-search-mcp reindex --cwd .` 1회, (3) wiki 갱신 필요시 `reindex --synthesize`.

### 이전 세션 완료

**4회차 (2026-04-20):**
- `85716bc` — Q4 Security 모듈 (MCP 입력/출력 trust boundary)

**3회차 (2026-04-20):**
- `e4f9731` — Q3 MCP stdin blank-line filter + Q5 민감 파일 패턴 필터

**2회차 (2026-04-20):**
- `2231b1f` — Q7 CLAUDE.md idempotent + Q8 core.hooksPath (Husky 호환)

**1회차 (2026-04-20):**
- `6f0ff93` — Q1 route_hook + status + wiki 머신별 독립화
- `a3bdabf` — wiki-gaps.txt git 추적 제거
- `4ccefd8` — HANDOFF 업데이트 (Q1/Q2/Q9)

### ⬜ 다음 세션 제안 — M1 (Confidence 라벨, 1일) 또는 M4 (needs_synthesis flag, 반나절)

**M1 (권장, 1일): Confidence 3단계 라벨 + numeric score**
- 모든 call edge에 `confidence ∈ {EXTRACTED, INFERRED, AMBIGUOUS}` + `score ∈ [0,1]` 부여.
- BM25+vector fusion에서 score를 가중치로 활용. 현재는 callgraph에만 `confidence` 문자열(high/medium/low)이 존재 — 숫자 점수 도입 시 fusion/랭킹과 자연스럽게 연결.
- **중요 원칙**: Leiden/degree 같은 구조 분석은 confidence-blind 유지 (DAG 구성 정확성), 리포트/ranking만 confidence-aware. 섞지 말 것.
- 적용: `src/hybrid_search/index/callgraph.py`(엣지 emit), `src/hybrid_search/search/fusion.py`(score 활용).
- 참조: graphify `extract.py:1060-1068` (EXTRACTED 1.0), `extract.py:3206-3211` (INFERRED 0.8), `test_confidence.py:65-77` (AMBIGUOUS ≤0.4). 패치 문서 M1 섹션.

**M4 (대안, 반나절): `needs_synthesis` flag 파일 패턴**
- 훅은 LLM 호출 못 함 → 대신 flag 파일로 "synthesis 필요" 신호.
- `src/hybrid_search/tools/index.py` stale 감지 시 `.hybrid-search/needs_synthesis` flag write.
- `/search` 스킬이 읽고 사용자에게 리마인드. `/maintain` 실행 시 flag clear.
- 참조: graphify `watch.py:110-118`, 패치 문서 M4 섹션.

### 📂 필수 참조 문서 (다음 세션에서 반드시 읽을 것)

| 문서 | 경로 |
|------|------|
| **구현 플랜 (Q1 템플릿)** | `PLAN_q1_routing_hook.md` (프로젝트 루트) |
| **패치 상세 리스트 (Q1-Q10)** | `_study/graphify-analysis/99-actionable-patches-for-hybrid-search.md` |
| **전체 분석 종합** | `_study/graphify-analysis/00-overview.md` |
| **훅 상세 분석** | `_study/graphify-analysis/01-hooks-and-skill.md` |
| **전략 방향 (왜 이 작업?)** | `~/.claude/projects/-Users-ian-project-claude-project-hybrid-search-mcp/memory/project_strategic_direction.md` |
| **graphify 원본 (훅 참고)** | `/Users/ian/project/claude_project/_study/graphify/graphify/hooks.py` |

### 📋 Quick Wins 완성 + 다음 M 시리즈 로드맵

**Quick Wins (Q1~Q10): 10/10 완료 🎉 + M2 착수**

| # | 작업 | 완료 세션 |
|---|------|-----------|
| ~~Q1~~ | ~~route_hook + status + wiki 머신별 독립화~~ | 1회차 |
| ~~Q7~~ | ~~CLAUDE.md 자동 주입~~ | 2회차 |
| ~~Q8~~ | ~~core.hooksPath 존중~~ | 2회차 |
| ~~Q3~~ | ~~MCP stdin blank-line filter~~ | 3회차 |
| ~~Q5~~ | ~~민감 파일 필터~~ | 3회차 |
| ~~Q4~~ | ~~Security 모듈~~ | 4회차 |
| ~~Q6~~ | ~~Cache frontmatter strip~~ | 5회차 |
| ~~Q10~~ | ~~`.hybrid-search-ignore` upward walk~~ | 6회차 |
| ~~M2~~ | ~~post-checkout hook 추가~~ | 6회차 |
| ~~M3~~ | ~~post-commit diff env 전달 (race 방지 + subprocess 절약)~~ | **6회차** |

**다음: M 시리즈 계속 — 품질 축 전환**

| # | 작업 | 공수 | 축 | 우선순위 |
|---|------|------|----|----|
| **M1** | **Confidence 3단계 라벨 + numeric score** | **1일** | **품질** | **다음 1순위** |
| M4 | `needs_synthesis` flag 파일 패턴 | 반나절 | 자율 루프 | 중 |
| M5 | MCP 확장: `god_nodes`, `shortest_path`, `subgraph` | 2일 | 품질 | 낮음 |

전체 진행률: **12/28 (43%)**.

### 🎬 다음 세션 시작 방법

```
HANDOFF.md 최상단 섹션 + _study/graphify-analysis/99-actionable-patches-for-hybrid-search.md의
M2 섹션 (line ~367) + graphify hooks.py 읽고, M2 (post-checkout hook) 구현하자.
M3도 가까운 주제라 같은 세션 묶음 고려.
```

### 🔧 현재 상태 스냅샷

- **브랜치:** `main` (Q10 = `c71ddb1`, M2 = `b4319bc`, M3 = 이 세션 3번째 커밋)
- **워킹 트리:** Q10 + M2 + M3 커밋됨
- **테스트:** 396/396 passed (370 → +8 Q10 → +10 M2 → +8 M3)
- **주 작업 파일:**
  - `src/hybrid_search/index/scanner.py` (Q10 ignore walker, M3 `parse_git_diff_name_status` 추출)
  - `src/hybrid_search/cli.py` (M2 훅 2종 설치, M3 `_HOOK_DIFF_ENV` 경로)
  - `scripts/post-commit-hook.sh` (M3 synchronous diff capture)
  - `scripts/post-checkout-hook.sh` (M2 레퍼런스, 수동 설치용)
  - `tests/test_scanner.py` (+8 Q10, +5 M3), `tests/test_cli_hook_install.py` (+10 M2, +3 M3)
- **status 출력:** post-commit + post-checkout 둘 다 표시 (PreToolUse hooks 4/4, CLAUDE.md routing ✓).

### ⚠️ 주의사항

- **graphify 분석 문서**는 `_study/` 폴더에 있고 이 프로젝트 git과 **별개** (추적 안 됨).
- **Q4 sanitize 범위** — MCP 노출은 `hybrid_search` 1개뿐 (`server.py:89-125`). 향후 새 도구 노출 시 **반드시** `handle_*`에서 `sanitize_*` / `clamp_*` 호출. control char regex는 `\t\n\r` 보존이 의도된 동작.
- **Q3 stdin filter** — `_filter_blank_stdin()`은 전역 fd 0를 `dup2`로 교체함. pytest 메인 프로세스에서 직접 호출 금지. 테스트는 subprocess 격리 (`tests/test_server_stdin_filter.py`).
- **Q5 sensitive 패턴** — basename 우선, path 패턴은 `.ssh/id_*` 등 위치 의존에만. 정상 소스(`PasswordReset.tsx` 등) 통과 필수. 신규 패턴 추가 시 `TestIsSensitiveFile::test_source_files_not_blocked`도 업데이트.
- **Q7 marker regex:** `<!-- hybrid-search -->\n## [^\n]+\n.*?(?=\n## |\Z)` — 마커 + 첫 `##` 헤딩 + 본문. 치환은 `lambda`로 back-reference 파싱 회피.
- **Q8 core.hooksPath 폴백:** `git config --get` → `git rev-parse --git-path hooks` → `.git/hooks`.
- **Q6 frontmatter regex:** `\A---\r?\n.*?\r?\n---\r?\n` (DOTALL, count=1). `\A`로 파일 시작 고정 → body 내부 `---`(horizontal rule)을 false-positive로 잡지 않음. 이 regex를 수정하면 `TestComputeFileHashFrontmatter::test_body_level_horizontal_rule_preserved` 같이 깨질 수 있으니 주의.
- **Q6 side effect — file_size/mtime drift:** fm-only edit이면 `_is_changed`가 False 반환해서 `files.file_size`/`file_mtime`이 갱신 안 됨. 다음 스캔에서 mtime 불일치로 매번 `compute_file_hash` 재계산(정확하지만 중복 CPU). 필요시 `_is_changed`에서 hash 일치해도 size/mtime만 UPDATE하는 최적화 가능 (Q6.1 후보).
- **Q10 anchoring 주의:** 수집된 모든 `.hybrid-search-ignore` 패턴을 하나의 pathspec로 합쳐 `project_root` 기준으로 매칭함. 따라서 **ancestor 파일의 rooted 패턴**(예: `/dist/`)은 그 ancestor가 아니라 `project_root` 기준으로 해석됨 (직관과 다를 수 있음). 대부분의 ignore 패턴은 non-rooted(`dist/`, `*.log`)라 실무 영향 없음. 필요 시 per-anchor PathSpec로 정밀화 가능(Q10.1 후보). 테스트 `test_walk_stops_at_git_boundary`로 `.git` 경계 방어 확인.
- **Q10 reindex 영향:** 새 `.hybrid-search-ignore` 추가 시 다음 reindex에서 이전 포함 파일이 제거되어 해당 chunks가 DB에서 삭제됨. 의도된 동작. Full rebuild 불필요.
- **M2 post-checkout 튜닝 여지:** 현재는 단순 filesystem-delta reindex. 빠른 브랜치 왕복(`git checkout -`)이 잦은 워크플로우에서 반복 reindex 비용이 보일 수 있음. 개선 아이디어: (1) 마지막 reindex HEAD hash를 `.hybrid-search/last_indexed_head` 파일에 저장 → 동일 head면 skip, (2) M3 기반으로 `git diff <prev>..<new> --name-only` 전달. M3와 함께 묶기 권장.
- **M2 식별 마커 주의:** `_HOOK_IDENTITY_MARKER = "hybrid_search.cli"` — 두 훅 모두 이 문자열 포함. 개별 훅 파일 단위로 체크하므로 충돌 없음. 레거시(이전 단일 hook) 설치도 동일 문자열이라 자동 인식됨.
- **M3 env 보안 주의:** `HYBRID_SEARCH_CHANGED_STATUS`는 훅이 export하지만 **외부에서도 설정 가능**. `cmd_reindex`는 `--git-delta` 게이트 안에서만 env를 읽으므로 일반 사용자가 실수로 env를 상속해도 `--git-delta` 없이는 무시됨. 악성 env로 인한 "가짜 파일 리인덱싱" 공격은 이론상 가능하지만 대상 파일은 프로젝트 내 실제 경로로 제한(scan_project_subset → `_is_indexable_path` 검증) → 영향 제한적. 추후 env 서명 고려 가능.
- **M3 race 방지 원리:** 훅은 post-commit 시점에 동기적으로 `git diff --name-status HEAD~1 HEAD`를 캡처 → env export → `nohup &`로 배경 프로세스에 전달. 그 사이에 다른 커밋이 들어와도 env는 커밋 시점 snapshot이라 올바른 diff를 유지. env가 없는 경로(기존)는 `nohup` 내부에서 subprocess를 늦게 실행하므로 HEAD 이동에 취약.
- **skill 파일 수정** 시 `~/.claude/skills/`로 동기화 잊지 말 것 (setup이 처리).

---

## 📚 이전 세션 히스토리 (Phase 1~10 완료)

> **Date**: 2026-04-14 | **Branch**: main
> **설계 문서**: `docs/design.md` (v7, Phase 1-10 완료 + LLM 재랭킹)

## 프로젝트 한줄 요약

BM25 + Vector(RRF) 하이브리드 검색 MCP 서버. 한국어 자연어 → 영어 코드 크로스 언어 검색이 핵심 가치.

---

## 완료된 것

### Phase 1: MVP — 시맨틱 검색 파이프라인 ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| 임베딩 모델 벤치마크 | §7 | `benchmarks/run_benchmark*.py` | — |
| MCP 서버 뼈대 (7개 도구) | §10 | `server.py` | 257 |
| File scanner + delta detection | §9 | `index/scanner.py` | 195 |
| AST chunker (TS/JS/Python) | §8 | `index/ast_chunker.py` | 604 |
| 문서 chunker (MD/JSON/YAML) | §8 | `index/doc_chunker.py` | 181 |
| Embedding 생성 (sentence-transformers) | §7 | `index/embedder.py` | 256 |
| USearch 벡터 인덱스 | §13 | `search/vector.py` | 163 |
| `semantic_search` tool | §10.2 | `tools/semantic_search.py` | 148 |
| `search_symbols` tool | §10.6 | `tools/symbols.py` | 60 |
| `index_project` / `index_status` | §10.5, §10.7 | `tools/index.py` | 55 |
| `list_projects` / `remove_project` | §10.8, §10.9 | `tools/projects.py` | 50 |

### Phase 2: Hybrid + BM25 ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Tantivy BM25 인덱스 | §13 | `search/bm25.py` | 156 |
| RRF fusion (k=60) | §11 | `search/fusion.py` | 51 |
| 쿼리 분류기 (SYMBOL/KR/EN) | §11 | `search/orchestrator.py` | 487 |
| `hybrid_search` tool | §10.1 | `tools/hybrid_search.py` | 51 |
| 멀티 프로젝트 + cross-project 검색 | §13 | `search/orchestrator.py` | (포함) |

### 지원 모듈 ✅

| 파일 | 역할 | 줄수 |
|------|------|:----:|
| `config.py` | TOML 설정 로딩, 모델 토큰 자동감지 | 207 |
| `project.py` | 글로벌 프로젝트 레지스트리 (SQLite) | 114 |
| `storage/db.py` | per-project store.db (WAL, FK CASCADE) | ~550 |
| `storage/indexes.py` | 인덱스 경로 관리 | 45 |
| `index/pipeline.py` | 인덱싱 오케스트레이션 (multi-store 트랜잭션) | ~315 |

### Phase 3a: Call Graph ✅

| 항목 | design.md 섹션 | 구현 파일 | 줄수 |
|------|:-------------:|-----------|:----:|
| Call Graph Resolution (3단계 confidence) | §12 | `index/callgraph.py` | 155 |
| trace_callers/trace_callees 도구 | §10.3, §10.4 | `tools/trace.py` | 250 |
| StoreDB call graph 쿼리 (10개 메서드) | §12, §13 | `storage/db.py` (추가) | +180 |
| AST byte offset 버그 수정 | §8 | `index/ast_chunker.py` (수정) | — |

**검증**: 1,934 call edges, 146 resolved (7.5%), 0 dirty. trace depth 2에서 정확한 caller/callee 추적.

### Phase 3a Code Review 수정 ✅

| 수정 | 우선순위 |
|------|:--------:|
| `_process_file`에 `db.transaction()` 적용 (partial write 방지) | P1 |
| `db._conn` 직접 접근 제거 → public method/transaction 사용 | P1 |
| `call_edges.callee_chunk_id` 인덱스 추가 | P2 |
| `_get_file_from_chunks` O(N) → `file_index` dict O(1) | P2 |
| `lstrip("./")` → `removeprefix("./")` | P2 |
| 삭제 시 dangling callee edge 정리 (`delete_call_edges_by_callee`) | P2 |

### Phase 3b: 추가 언어 지원 ✅

| 항목 | design.md 섹션 | 구현 파일 | 변경 |
|------|:-------------:|-----------|:----:|
| 10개 언어 AST 청킹 (Rust/Go/Ruby/Java/C/C++/Swift/Kotlin/CSS/SQL) | §8 | `index/ast_chunker.py` | CHUNK_NODE_TYPES, CLASS_NODE_TYPES, _get_ts_language, _classify_node_type, _extract_name, _extract_imports, _extract_docstring, _extract_call_name 확장 |
| tree-sitter grammar 의존성 11개 추가 | §17 | `pyproject.toml` | +11 패키지 |

**검증**: 모든 14개 AST 언어 파싱 성공 (TS/JS/Python/Rust/Go/Ruby/Java/C/C++/Swift/Kotlin/CSS/SCSS/SQL). HTML은 fallback blank-line chunking 사용.

### Phase 4: Polish ✅

| 항목 | 설명 | 상태 |
|------|------|:----:|
| 테스트 확충 | 175개 테스트 (12개 파일) | ✅ |
| 크래시 복구 | (1) consistency mismatch → 자동 force rebuild (2) `file_hash=""` partial write 감지 → 재인덱싱 | ✅ |
| ONNX 백엔드 | `_embed_onnx_batch()` 완전 구현 (mean pooling + L2 normalize) | ✅ |
| Ollama 백엔드 | `POST /api/embed` HTTP API 백엔드 | ✅ |
| Apple Silicon MPS | ONNX: CoreMLExecutionProvider, ST: `device="mps"` | ✅ |
| 인덱싱 진행률 | `ProgressCallback(current, total, path)` | ✅ |
| config.toml 핫 리로드 | `_HotReloadableConfig` mtime 감지 | ✅ |
| CWD 프로젝트 부스트 | BM25 2:1 weighted interleave + Vector cosine +0.05 | ✅ |

### Phase 5: Reactive Wiki Layer ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| WikiConfig (config.toml `[wiki]` 섹션) | `config.py` | +15 |
| wiki_pages + wiki_dependencies 테이블 | `storage/db.py` | +25 |
| WikiStore (compile/lookup/staleness/refresh/eviction) | `storage/wiki.py` | ~270 |
| 4개 Tool 핸들러 | `tools/wiki.py` | ~180 |
| server.py 등록 (13개 도구) | `server.py` | +100 |
| 테스트 28개 | `tests/test_wiki.py` | ~280 |

**핵심 설계**: 서버는 저장 + 의존성 추적만 담당, 콘텐츠는 Claude가 작성. `source_chunk_ids`로 검색 결과 → 파일 해시 스냅샷 자동 연결. 변경 감지 시 stale 마킹.

### Phase 6a: CLI + Hook + 스킬 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| CLI 엔트리포인트 (`reindex`, `status`, `stale`, `install-hook`, `sync-wiki`) | `cli.py` | 402 |
| post-commit hook 스크립트 + 자동 설치 | `scripts/post-commit-hook.sh` | 11 |
| `/bootstrap-wiki` 스킬 | `~/.claude/skills/bootstrap-wiki/skill.md` | 142 |
| `/save-wiki` 스킬 | `~/.claude/skills/save-wiki/skill.md` | ~65 |
| `/search` 스킬 (wiki-first 4단계 폴백) | `~/.claude/skills/search/skill.md` | ~64 |

**CLI 명령어**:
- `python -m hybrid_search.cli reindex --cwd .` — delta 재인덱싱 + stale 마킹 + gap 플래그
- `python -m hybrid_search.cli status` — 전체 프로젝트 인덱스 상태
- `python -m hybrid_search.cli stale --cwd .` — wiki staleness 확인
- `python -m hybrid_search.cli install-hook --cwd .` — post-commit hook 자동 설치
- `python -m hybrid_search.cli sync-wiki --cwd .` — 디스크 wiki → DB 동기화 (backtick 경로에서 파일 의존성 자동 추출)
- `python -m hybrid_search.cli call-graph-stats --cwd .` — call graph resolution 통계 (Phase 7)

**스킬 검색 체인** (`/search`):
```
1. lookup_wiki (DB) → found+fresh → 즉시 반환
2. wiki/index.md (디스크) → Read로 확인
3. hybrid_search (MCP) → 결과 좋으면 compile_to_wiki로 축적
4. Grep/Glob (폴백) → 직접 검색
```

**설치된 hook**: valuein-homepage, breeze 프로젝트에 post-commit hook 설치 완료.

**valuein-homepage wiki 부트스트랩 완료**: 10개 wiki 페이지 생성 + DB 동기화 (sync-wiki). architecture, students, tuition-billing, attendance, learning-data, homework-analysis, diagnosis, portal, consultation, edge-functions.

### Phase 7: Call Graph Resolution 90%+ ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| Step 1: Import-Call 바인딩 | `index/ast_chunker.py` | `_extract_import_map()` 신규, `_extract_calls()` → `list[tuple[str, str\|None]]` 반환 |
| Step 2: Module Path → File 역인덱스 | `index/callgraph.py` | `_build_module_index()` 신규, 다양한 import path 형태 → 파일 chunks 매핑 |
| Step 3: 메서드 Receiver 추적 | `index/ast_chunker.py`, `index/callgraph.py` | `this`/`self` 감지 → `__self__::ClassName` 태그, `class_members` 인덱스 |
| Step 4: COMMON_NAMES 정책 완화 | `index/callgraph.py` | module context 있으면 common name도 medium으로 승격 |
| DB 인터페이스 변경 | `storage/db.py` | `insert_call_edges(calls: list[tuple[str, str\|None]])` callee_module 포함 |
| CLI call-graph-stats | `cli.py` | `python -m hybrid_search.cli call-graph-stats --cwd .` 명령 추가 |
| 테스트 10개 추가 | `tests/test_callgraph.py` | Import-Call 5개 + Self-Method 3개 + Common-Name 2개 |

**핵심 설계**: 기존 `_extract_imports()`는 raw string 리스트로 유지(임베딩용), 별도 `_extract_import_map()`으로 name→module 딕셔너리 생성. `_extract_call_name_ex()`에서 this/self receiver 감지. callgraph에 module_index + class_members 이중 인덱스 추가. `_BUILTIN_CALLS` + `_BUILTIN_METHOD_CALLS`로 built-in/라이브러리 호출 필터링.

**지원 언어 Import 파싱**: TS/JS (named/default/namespace import), Python (from...import, import...as), Go, Java, Rust, Ruby, Kotlin, Swift

**실측 결과**:

| 프로젝트 | Total Edges | Project Deps (H+M) | Module 있는 edge | 그 중 Resolved |
|----------|:-----------:|:-------------------:|:----------------:|:--------------:|
| hybrid-search-mcp (73파일) | 2,727 | 572 | 686 | **45.3%** |
| valuein-homepage (1,757파일) | 21,127 | 1,961 | 1,560 | **66.2%** |

**해석**: 전체 resolution rate(21-13%)는 외부 라이브러리 호출(`supabase.from()`, `Array.map()`)이 denominator를 부풀려 낮게 보임. import-call 바인딩이 성공한 edge는 45-66% resolve. CodeWiki에 필요한 **Project Deps (High+Medium)** = 572~1,961개 — 프로젝트 내부 의존성 그래프 구축에 충분.

**총 코드**: ~7,900줄 (31개 파일) | **MCP 도구**: 13개 | **테스트**: 185개 (12개 파일) | **CLI 명령**: 6개 | **스킬**: 3개

### Phase 8a: CodeWiki 모듈 트리 자동 생성 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| DAG 구축 (High+Medium confidence edges) | `index/dag.py` | ~310 |
| Connected Components (BFS 무방향 탐색) | `index/dag.py` | (포함) |
| Topological Sort (Kahn's algorithm, 사이클 내성) | `index/dag.py` | (포함) |
| 모듈 이름 자동 유도 (공통 디렉토리 기반) | `index/dag.py` | (포함) |
| 대형 모듈 분할 (MAX_MODULE_CHUNKS=40) | `index/dag.py` | (포함) |
| 고립 노드 디렉토리 기반 폴백 그룹핑 | `index/dag.py` | (포함) |
| `generate-wiki-plan` CLI 명령 | `cli.py` | +70 |
| `verify-wiki` CLI 명령 | `cli.py` | +50 |
| 테스트 24개 | `tests/test_dag.py` | ~280 |

**핵심 설계**: CodeWiki (ACL 2026) 파이프라인 Step 1-3 구현. call_edges에서 High+Medium confidence edge만 추출하여 방향성 DAG 구축 → 무방향 BFS로 connected component 식별 (= 1개 기능 모듈) → Kahn's algorithm으로 위상정렬 (bottom-up 처리 순서). 고립 노드(call edge 없는 청크)는 디렉토리 기반 그룹핑으로 폴백. `wiki-plan.json` 파일 출력으로 downstream 스킬/Agent 연동 가능.

**실측 결과** (hybrid-search-mcp):
- 9개 graph-based 모듈 + 10개 isolated 그룹
- 491/492 chunks 커버 (99.8%)
- Entry point 자동 식별: `cli.py::main`, `handle_hybrid_search`, `SearchOrchestrator.hybrid_search` 등

**총 코드**: ~8,500줄 (34개 파일) | **MCP 도구**: 13개 | **테스트**: 212개 (13개 파일) | **CLI 명령**: 8개 | **스킬**: 3개

### Phase 8b: Wiki 페이지 자동 생성 ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| 모듈별 구조적 wiki 마크다운 생성 | `index/dag.py` (`generate_module_wiki`) | +130 |
| 전체 프로젝트 wiki 일괄 생성 | `index/dag.py` (`generate_all_wiki_pages`) | +40 |
| `generate-wiki` CLI 명령 (디스크 + DB sync) | `cli.py` | +75 |
| 테스트 6개 추가 | `tests/test_dag.py` | +95 |

**핵심 설계**: LLM 호출 없이 코드 메타데이터만으로 구조적 wiki 생성. 각 모듈 페이지에: 파일 목록, entry points, 심볼별 call/called-by 관계, 외부 의존성. `generate-wiki` CLI가 디스크 `.hybrid-search/wiki/`에 쓰고 DB에 자동 sync (staleness 추적 포함).

**실측 결과** (hybrid-search-mcp):
- 20개 wiki 페이지 생성 (index + 9 graph + 10 isolated)
- 18개 DB sync 완료
- 자동 생성된 페이지: tools.md (19 symbols, call 관계 포함), search.md, storage.md 등

**총 코드**: ~9,000줄 (34개 파일) | **MCP 도구**: 13개 | **테스트**: 218개 (13개 파일) | **CLI 명령**: 9개 | **스킬**: 3개

### Phase 8c: verify-wiki 강화 ✅

| 항목 | 변경 |
|------|:----:|
| query_key 기반 정확한 매칭 (title 대소문자 비교 → normalize_query) | 버그 수정 |
| uncovered 파일 목록 출력 | 신규 |
| `--json` 플래그 (JSON 구조화 출력) | 신규 |
| staleness 상세 리포트 (fresh/stale 카운트 + changed files) | 강화 |

### Phase 8d: 전체 파이프라인 자동화 ✅

| 항목 | 변경 |
|------|:----:|
| `reindex` 후 자동 call graph re-resolution | 신규 |
| `reindex --wiki` 플래그 → generate-wiki 자동 체인 | 신규 |

**전체 파이프라인**:
```
git commit
  └→ post-commit hook
     └→ reindex (delta)
        └→ call graph re-resolve (자동)
           └→ wiki sync (기존 wiki 있으면 자동)

reindex --wiki (명시적)
  └→ delta reindex
     └→ call graph re-resolve
        └→ generate-wiki (모듈 트리 → wiki 생성 + DB sync)
```

### Phase 8e: Wikilink 그래프 (GraphRAG) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| `[[링크]]` 파싱 + DB 동기화 (`_sync_wikilinks`) | `storage/wiki.py` | +40 |
| BFS 양방향 그래프 탐색 (`_expand_graph`) | `storage/wiki.py` | +80 |
| `wiki_links` 테이블 (source, target, link_text) | `storage/db.py` | +10 |
| lookup_page → linked_pages 자동 확장 | `storage/wiki.py` | (통합) |

**핵심 설계**: compile_page/refresh_page 시 `[[텍스트]]` 패턴을 자동 파싱하여 wiki_links 테이블에 저장. lookup_page 호출 시 BFS(max_hops=2, max_pages=10)로 양방향 탐색하여 linked_pages 반환. CLAUDE.md 규칙: "wiki에 [[링크]]가 있으면 연결된 페이지도 반드시 읽을 것."

### Phase 9a: LLM Wiki Synthesis (prepare/finalize) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| Synthesizer (prepare/finalize/verify/merge/hash) | `index/synthesizer.py` | ~430 |
| DB 스키마 v3 마이그레이션 (synthesis_* 4컬럼) | `storage/db.py` | +30 |
| WikiStore synthesis 필드 + 헬퍼 메서드 7개 | `storage/wiki.py` | +60 |
| SynthesisConfig | `config.py` | +5 |
| `synthesize-wiki` CLI (--dry-run, --module, --finalize) | `cli.py` | +130 |
| 테스트 27개 | `tests/test_synthesizer.py` | ~280 |

**핵심 설계**: Claude Code 자체가 LLM이므로 외부 API 키 불필요. 3단계 구조:
1. CLI `synthesize-wiki` → `_synthesis_input/*.md`에 컨텍스트 수집 (DB IO만, 토큰 0)
2. Claude Code가 컨텍스트 파일 Read → 합성 작성 → `_synthesis_output/*.md`에 Write
3. CLI `synthesize-wiki --finalize` → 참조 검증 + 결정론적 wiki 병합 + `_raw/` 백업 + DB 저장 (토큰 0)

합성 결과는 상단(Overview, Key Design Decisions, Data Flow, Caveats, Related Modules) + 하단 `<details>` 접기(결정론적 구조 데이터) 형태. synthesis_hash로 변경 감지하여 불필요한 재합성 방지.

**E2E 검증** (AST Chunker): 9개 참조 100% 검증 통과, 0개 제거.

**총 코드**: ~10,000줄 (36개 파일) | **MCP 도구**: 3개 | **테스트**: 241개 (14개 파일) | **CLI 명령**: 13개 | **스킬**: 3개

### Phase 9b: 전체 모듈 Bottom-Up 합성 ✅

| 항목 | 변경 |
|------|:----:|
| 28개 모듈 일괄 prepare → Claude Code 합성 → finalize | 완료 |
| `finalize_module` 타이틀 매칭 버그 수정 (slug vs 원본 이름) | `index/synthesizer.py` |
| 중복 RAW 페이지 정리 (18개 삭제) | DB cleanup |

**실행 결과**:
- 28/28 모듈 합성 완료 (100%)
- 참조 검증: 총 108개 refs verified, 29개 removed (73% 검증률)
- `_raw/` 백업: 20개 원본 결정론적 wiki 보존
- DB: 28 pages, 28 synthesized (중복 없음)

**발견된 버그 & 수정**:
1. `finalize_module`에서 `find_page_by_title`에 raw slug(대시 포함)를 전달 → LIKE 매칭 실패
   - 수정: 원본 이름 → 대시-공백 변환 순서로 2단계 fallback 시도
2. `collect_module_context`에도 동일 패턴 적용
3. `--` 포함 타이틀 (예: "Embedder -- OpenAI API Backend")은 `replace("-", " ")`로 4개 공백 생성 → LIKE 불일치
   - 수정: 원본 이름 먼저 시도하는 fallback 체인

### Phase 9c: 지식 복리 (Incremental Re-synthesis) ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| `should_skip_synthesis()` — staleness 기반 skip | `index/synthesizer.py` | +35줄 |
| `get_synthesis_hash()` — DB 저장 hash 조회 | `storage/wiki.py` | +7줄 |
| `find_indirectly_affected()` — wikilink BFS 간접 영향 | `storage/wiki.py` | +25줄 |
| `_auto_prepare_synthesis()` — reindex → prepare 체이닝 | `cli.py` | +70줄 |
| `reindex --synthesize` 플래그 | `cli.py` | +5줄 |
| CLI prepare에서 hash skip 로직 | `cli.py` | +10줄 |
| 테스트 8개 | `tests/test_synthesizer.py` | +100줄 |

**핵심 설계**: `should_skip_synthesis()`는 file_hash_at_compile vs 현재 file_hash 비교 (staleness 기반). synthesis_hash는 finalize 시 변경되므로 단순 hash 비교는 false positive 발생 — staleness 기반이 정확함. `reindex --synthesize`로 stale 감지 후 자동 prepare, `find_indirectly_affected()`로 wikilink 1-hop 이웃 모듈도 선택적 re-prepare.

**전체 파이프라인**:
```
reindex --synthesize
  └→ delta reindex
     └→ call graph re-resolve
        └→ _mark_stale_wikis → STALE.md
           └→ _auto_prepare_synthesis
              ├→ stale 모듈 prepare (skip if unchanged)
              └→ indirect 모듈 prepare (wikilink 1-hop)
```

### Phase 9d: 환각 검증 자동화 ✅

| 항목 | 구현 파일 | 변경 |
|------|-----------|:----:|
| `verify_symbols()` — backtick 심볼 DB 존재 검증 | `index/synthesizer.py` | +55줄 |
| `SymbolVerificationResult` 데이터 클래스 | `index/synthesizer.py` | +5줄 |
| `has_chunk_matching_name()` — qualified_name LIKE 검색 | `storage/db.py` | +7줄 |
| `verify-synthesis` CLI (--json, --fix) | `cli.py` | +110줄 |
| 테스트 6개 | `tests/test_synthesizer.py` | +60줄 |

**핵심 설계**: 2종 검증 — (1) file:line 참조 (기존 `verify_references()`) + (2) backtick 심볼명 (`verify_symbols()`). 심볼 검증은 PascalCase/snake_case 식별자를 추출하여 chunks.name 또는 chunks.qualified_name에서 확인. `_SYMBOL_SKIP` 집합으로 common words (true, false, self 등) 필터링. `--fix` 플래그로 bad refs 자동 제거.

**CLI 명령**:
```bash
python -m hybrid_search.cli verify-synthesis --cwd .         # 전체 합성 검증 리포트
python -m hybrid_search.cli verify-synthesis --json --cwd .  # JSON 출력
python -m hybrid_search.cli verify-synthesis --fix --cwd .   # bad refs 자동 제거
```

**총 코드**: ~9,700줄 (34개 파일) | **MCP 도구**: 3개 | **테스트**: 255개 (14개 파일) | **CLI 명령**: 15개 | **스킬**: 3개

### Phase 10: LLM 재랭킹 (Claude Code Native) ✅

| 항목 | 구현 파일 | 줄수 |
|------|-----------|:----:|
| RerankingConfig (`[search.reranking]` TOML 섹션) | `config.py` | +10 |
| Orchestrator — reranking 시 확장 후보 반환 | `search/orchestrator.py` | +10 |
| HybridSearchResponse에 `reranked` 필드 | `search/orchestrator.py` | +2 |
| `rerank_hint` — Claude Code 재랭킹 지시 | `tools/hybrid_search.py` | +20 |
| 테스트 16개 | `tests/test_reranker.py` | ~190 |

**핵심 설계**: Phase 9a와 동일 원칙 — Claude Code 자체가 LLM이므로 외부 API 키 불필요. `hybrid_search` MCP 도구가 RRF top-20 후보를 enriched 메타데이터(name, file_path, snippet, node_type)와 함께 반환. `rerank_hint` 메시지가 Claude Code에게 "쿼리 의도에 맞게 재정렬하여 상위 10개만 제시하라"고 지시. API 호출 0, 추가 비용 0, 지연 0.

**설정**:
```toml
[search.reranking]
enabled = true                       # 기본 false
max_candidates = 20                  # RRF에서 가져올 후보 수
```

**파이프라인**:
```
쿼리 → BM25 + Vector → RRF fusion (top-20 enriched) → Claude Code 재랭킹 → top-10
```

**총 코드**: ~9,800줄 (35개 파일) | **MCP 도구**: 3개 | **테스트**: 271개 (15개 파일) | **CLI 명령**: 15개 | **스킬**: 3개

### MCP 도구 슬림화: 13→3 ✅

**이유**: MCP 도구 스키마가 매 대화 시스템 프롬프트에 로드되어 토큰 소모. 관리/wiki 도구 10개를 CLI로 이관.

| 잔류 MCP 도구 | 이유 |
|:------------:|------|
| `hybrid_search` | 핵심 검색 (semantic_search 병합: bm25_weight=0) |
| `trace_callers` | 대화 중 역방향 call graph 추적 |
| `trace_callees` | 대화 중 순방향 call graph 추적 |

| CLI로 이관 (10개) | CLI 명령 |
|:----------------:|----------|
| `index_project` | `reindex` |
| `index_status` | `status` |
| `list_projects` | `status` |
| `remove_project` | `remove-project` (신규) |
| `search_symbols` | `search-symbols` (신규) |
| `semantic_search` | `hybrid_search`에 병합 |
| `compile_to_wiki` | `generate-wiki` |
| `lookup_wiki` | `lookup-wiki` (신규) |
| `check_wiki_staleness` | `stale` |
| `refresh_wiki_page` | `sync-wiki` |

**총 코드**: ~9,200줄 (34개 파일) | **MCP 도구**: 3개 | **테스트**: 218개 (13개 파일) | **CLI 명령**: 12개 | **스킬**: 3개

---

## 실전 검증 결과

### breeze 프로젝트 (소규모)
- **규모**: 155파일, 326 chunks, 90초 (CPU)
- **한국어 검색**: "할일 관리" → action-item-calendar.tsx, today-focus-hero.tsx 등 정확 매칭
- **검색 속도**: 741ms (hybrid_search)

### valuein-homepage 프로젝트 (대규모) — 2026-04-13 추가
- **규모**: 1,757파일, 9,559 chunks, 229초 (CPU) / 193초 (MPS)
- **한국어 검색 테스트** (4/4 정확 매칭):
  - "학원비 결제 처리" → `tuition-billing.md` 학원비 상세 (464ms)
  - "로그인 인증" → `auth/rules.md` + `login/page.tsx` (403ms)
  - "학생 출결 관리" → `learning-attendance.md` 출석부 개요 (369ms)
  - "캘린더 일정 표시" → `calendar_events.md` + `schedule/page.tsx` (364ms)

### 공통
- **임베딩 모델**: OpenAI `text-embedding-3-small` (1536차원, HTTP API)
  - **비용**: 인덱싱 ~$0.04/프로젝트, 검색 사실상 무료
  - **로컬 리소스**: CPU/메모리 부하 제로 (urllib만 사용)

---

## 즉시 해야 할 것

Phase 10 완료. 전 Phase 완료 — Claude Code가 wiki + 검색 결과로 직접 답변하므로 별도 RAG 불필요.

---

## 실행 환경

```bash
# CLI 명령 (pip install 후 바로 사용)
hybrid-search-mcp index .                              # 프로젝트 인덱싱
hybrid-search-mcp search "query"                       # 하이브리드 검색
hybrid-search-mcp serve                                # MCP 서버 시작
hybrid-search-mcp setup                                # Claude Code 설정
hybrid-search-mcp reindex --cwd .                      # delta 재인덱싱
hybrid-search-mcp status                               # 인덱스 상태
hybrid-search-mcp stale --cwd .                        # wiki staleness
hybrid-search-mcp install-hook --cwd .                 # post-commit hook 설치
hybrid-search-mcp sync-wiki --cwd .                    # 디스크 wiki → DB 동기화
hybrid-search-mcp reindex --synthesize --cwd .         # reindex + stale → auto prepare
hybrid-search-mcp synthesize-wiki --cwd .              # prepare: 컨텍스트 수집
hybrid-search-mcp synthesize-wiki --dry-run --cwd .    # dry-run: 토큰 추정
hybrid-search-mcp synthesize-wiki --finalize --cwd .   # finalize: 검증+병합+DB저장
hybrid-search-mcp verify-synthesis --cwd .             # 합성 검증 (refs + symbols)
hybrid-search-mcp verify-synthesis --fix --cwd .       # 검증 + bad refs 자동 제거

# 테스트
python -m pytest tests/ -v

# 인덱스 데이터 위치
~/.hybrid-search/projects/{project_hash}/
~/.hybrid-search/global/project_registry.db
~/.hybrid-search/config.toml
```

### MCP 설정 위치

`~/.claude.json`에 등록됨 (글로벌 MCP 서버):

```json
{
  "mcpServers": {
    "hybrid-search": {
      "command": "<path-to-venv>/bin/python",
      "args": ["-m", "hybrid_search.server"]
    }
  }
}
```

`hybrid-search-mcp setup` 실행 시 실제 venv 경로로 자동 등록됩니다.

### 스킬 위치

```
~/.claude/skills/bootstrap-wiki/skill.md  — 프로젝트 wiki 자동 생성
~/.claude/skills/save-wiki/skill.md       — 대화 중 분석 → wiki 저장
~/.claude/skills/search/skill.md          — wiki-first 4단계 검색 체인
```

---

## 알려진 이슈 & 교훈

1. **FK CASCADE 주의** (§18 #6): `INSERT OR REPLACE`는 SQLite에서 DELETE+INSERT로 동작 → FK CASCADE 발동. 반드시 `ON CONFLICT DO UPDATE` 사용.

2. **Python 3.13 sqlite3** (§18 #7): `isolation_level` 기본값 변경됨. `isolation_level=None` + 명시적 `conn.commit()` 패턴 사용 중.

3. **tree-sitter-languages 미지원**: Python 3.13에서 `tree-sitter-languages` 패키지가 안 됨. 개별 grammar 패키지로 전환 완료.

4. **MindVault 공존** (§15): MindVault hook 토큰 예산을 10000→3000으로 축소하고 글로벌 폴백을 비활성화함.

5. **tree-sitter byte offset** (§8, §18 #8): tree-sitter는 UTF-8 byte offset을 반환. 반드시 `source_bytes = source.encode()` 후 `source_bytes[start:end].decode()` 사용.

6. **Transaction 캡슐화**: `db._conn` 직접 접근은 partial write 위험. 항상 `db.transaction()` context manager 사용.

7. **callee_chunk_id에 FK 없음**: `call_edges.callee_chunk_id`는 FK 제약 없음 (resolve 전 NULL). 파일 삭제 시 `delete_call_edges_by_callee()`로 dangling reference 명시 정리 필요.

8. **스킬은 지시서일 뿐**: Claude가 스킬의 모든 단계를 실행한다고 보장할 수 없음. 핵심 동작(DB 동기화 등)은 CLI 명령으로 확정적으로 실행하는 것이 안전. 예: `sync-wiki` CLI가 `compile_to_wiki` MCP 도구 호출을 대체.

9. **DB 스키마 버전은 int 비교**: `_migrate_schema()`에서 int() 변환 후 비교. 문자열 비교 시 "9" < "10"이 False가 되는 문제 해결 (v3에서 수정).

10. **WikiStore 캡슐화**: `db._conn` 직접 접근 금지. synthesizer/CLI 등 외부에서는 WikiStore의 public 헬퍼 메서드(`get_page_row`, `find_page_by_title`, `get_page_file_hashes`, `get_page_deps`, `get_linked_page_ids`, `is_synthesized` 등) 사용.

11. **Slug↔Title 매칭 주의** (Phase 9b): `finalize_module`에서 파일명 slug(예: `call-graph-&-module-tree`)를 DB 타이틀(예: `Call Graph & Module Tree`)로 변환할 때, `replace("-", " ")`만으로는 부족. `--` 포함 타이틀은 공백 4개가 되어 LIKE 불일치 발생. 해결: 원본 이름 → 대시-공백 변환 2단계 fallback.

12. **합성 에이전트의 파일 쓰기 불안정**: Claude Code의 sub-agent(Agent 도구)에게 파일 쓰기를 위임하면 실제로 파일이 작성되지 않는 경우가 빈번. 핵심 파일 쓰기는 메인 세션에서 직접 수행하거나 Bash heredoc 사용이 안정적.

13. **synthesis_hash로 skip 판단 불가** (Phase 9c): `finalize_module()`이 merged content를 DB에 저장하므로, 이후 `collect_module_context()`가 읽는 deterministic_wiki가 달라져 hash가 불일치. 해결: staleness 기반(file_hash_at_compile vs 현재 file_hash) 비교가 정확.

14. **심볼 검증은 noise 관리가 핵심** (Phase 9d): backtick 안의 모든 텍스트가 심볼은 아님. `_SYMBOL_SKIP` (common words) + 파일 경로 필터 + 길이 제한으로 false positive 최소화.

---

## 핵심 설계 결정 (빠른 참조)

| 결정 | 선택 | 이유 (design.md 참조) |
|------|------|----------------------|
| 언어 | Python + 네이티브 확장 | §4: MCP SDK 성숙, 핵심 연산은 C++/Rust |
| 임베딩 | OpenAI text-embedding-3-small | §7: 로컬 리소스 제로, ~$0.04/프로젝트. urllib만 사용 |
| BM25 | tantivy-py | §4: Rust 백엔드, Lucene급 성능 |
| Vector DB | USearch HNSW | §4: C++ SIMD 최적화, M=16 |
| 청크 크기 | 비공백 4000자 | §8: cAST 논문 근거, 줄 수보다 정확 |
| RRF k값 | 60 | §11: Cormack et al. 원논문 표준값 |
| 쿼리 분류 | 3단계 (SYMBOL/KR/EN) | §11: 자동 BM25 가중치 조절 |
| Storage | per-project store.db (SQLite WAL) | §13: 트랜잭션 일관성 + 동시 읽기 |
| Call Graph | 4단계 resolution + module index + class members | §12 + Phase 7: import-call 바인딩, self/this 추적 |
| Wiki | DB(staleness) + 디스크(.md) 이중 저장 | Phase 5+6: DB로 추적, 디스크로 CLAUDE.md 참조 |
| CLI | sync-wiki로 확정적 DB 동기화 | Phase 6a: 스킬 의존 대신 CLI로 확실한 실행 |
| Wikilink | `[[링크]]` BFS 그래프 (max_hops=2) | Phase 8e: 페이지 간 관계 자동 추적 + 지식 복리 기반 |
| Synthesis | Claude Code가 직접 합성, API 키 불필요 | Phase 9a: CLI prepare/finalize로 토큰 최소화 |
| 전체 합성 | 28개 모듈 bottom-up 일괄, slug 2단계 fallback | Phase 9b: 참조 검증 73%, `_raw/` 백업 보존 |
| Skip 판단 | staleness 기반 (file_hash 비교), synthesis_hash 아님 | Phase 9c: finalize 후 content 변경으로 hash 비교 불가 |
| 간접 전파 | wikilink BFS 1-hop 이웃 모듈 auto-prepare | Phase 9c: stale 모듈의 이웃도 Related Modules 갱신 |
| 환각 검증 | file:line refs + symbol DB 존재 확인, `--fix` 자동 정리 | Phase 9d: 2종 검증으로 합성 품질 보장 |
| LLM 재랭킹 | Claude Code native, rerank_hint로 지시, API 키 불필요 | Phase 10: Phase 9a 원칙 동일 — Claude Code가 LLM |
