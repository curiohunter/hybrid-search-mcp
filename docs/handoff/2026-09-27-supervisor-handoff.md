# 감독 세션 인계 — 2026-09-23 ~ 27

새 세션은 이 문서만 읽고 시작한다. 다음 일은 **§3 설치 경험 게이트**다.

---

## 1. 지금 상태 (2026-09-27, main `c0989a7`)

- 로컬·원격 main 동일. 작업 폴더(worktree)는 main 하나. 원격 브랜치는 main 과
  `docs/readme-positioning`(PR #31, 주인 검토 대기) 둘.
- 전체 테스트 약 1,976 통과. CI: test 3.11 · 3.12 · package(릴리스 검사, 배포 단계 없음).
- 버전 표기는 0.9.0 이지만 **PyPI 에는 아직 0.8.0**. 업로드·태그 안 함(§3 통과 후).
- 이 레포의 라이브 인덱스는 답 없는 기억 게이트가 적용된 상태(`memory_gate=1`).
- 맥미니: 레포 `2c6bdfb` 로 동기화, Ollama `keep_alive=-1`(사용자 LaunchAgent 의 ollama plist 에
  환경변수 추가, 원본은 같은 폴더에 `.bak-20260927`). 임베딩 모델
  qwen3-embedding:0.6b 가 상주한다.

## 2. 이번 기간에 한 것 (PR 순)

| PR | 내용 |
|---|---|
| #17 · 히스토리 재작성 | 코퍼스를 인용한 골드셋 10개 추적 해제 + 전 브랜치·태그 히스토리에서 제거(force-push). 닫힌 PR 의 `refs/pull/*` 16개에는 남음 |
| #18 | 답 소유 판정 정본 `memory/qa_shape.owns_answer` (인용된 답을 제 답으로 읽던 버그) |
| #19 | 재정렬(`_order_qa_by_recency`)에도 "같은 일"(실물 겹침) 규칙 |
| #22 | 질문 기반 LLM 판정 러너 `benchmarks/query_judge.py` — 쌍 라벨 대신 이게 기본 판정 |
| #23 | 기억 head 가 이미 1위인 기록을 3위로 내리던 결함 수정 (프로브 392:22) |
| #24 | 답 없는 기억을 인덱스에서 뺀다(게이트) — 재측정 34:7 로 채택 |
| #25 | 계획 `docs/plans/2026-09-27-open-the-doors.md` |
| #26 | selfeval v1.2 — 셸 grep/sed 로 결과 파일 읽기 = 읽기, 인용 기억 채택(정밀도 ~50%, 상한) |
| #27 | `hybrid-search-mcp miss` (레포 밖 기록) — 사용자용이 아니라 보조 수단 |
| #28 | Claude Code auto memory 공존 실측 — 실제 중복 상한 ~13%, **아무것도 안 함**(닫힘) |
| #29 · #30 | 0.9.0 준비(CHANGELOG 업그레이드 영향, 릴리스 CI), 키 없을 때 안내 문구 |
| #32 | 불안정 테스트 수정(원인: 초 단위 qa 파일명 덮어쓰기) |
| #34 | **mcp SDK `<2` 상한** — mcp 2.2.0 에는 `Server.list_tools` 가 없어 서버 즉사. 맥미니에만 있던 미푸시 커밋을 옮김 |
| #35 | 계획 §4.1 설치 경험 게이트 |

근거 문서: 연구 `docs/studies/2026-09-23-distillate-vs-raw-log.md` §20~§24, 게이트
`docs/plans/2026-09-23-answerless-memory-gate.md`, 판정 `docs/plans/2026-09-25-query-grounded-judgment.md`.

## 3. 다음 일 — 설치 경험 게이트 (계획 §4.1)

주인 지적(2026-09-27): "PyPI 는 누군가 받아서 쓰는 건데 친절하게 프로덕션급이냐." 아니다.
키는 `echo … >> ~/.env.local` 로 손으로 넣고 검증도 없다, Ollama 는 설정 파일을 고쳐야 한다,
첫 인덱싱은 진행 표시 없이 5분, 7/26 새 설치 실측은 양쪽 실패였다.

순서:
1. setup 대화형 임베딩 안내(OpenAI 키 / Ollama 주소 / Gemini) + **실호출로 검증 후 저장**.
   비대화형(CI·`--yes`)은 지금 안내 문구 유지. 키 값은 절대 출력·로그 금지.
2. 첫 인덱싱 진행 표시(n/N, 예상 남은 시간).
3. **새 사용자 시험(합격 조건)**: 맥에 새 사용자 계정 → Claude Code 에게 README 만 보고 설치
   → 첫 인덱싱 → 검색 → 두 번째 세션 회상까지 개발자 개입 없이. 옛 버전 업그레이드도 한 번.
   **새 사용자 계정 생성은 주인 확인 후.**
4. README 설치 절 재작성(PR #31 과 순서 조율).
5. 통과 후에만 PyPI 0.9.0 업로드 · 태그 — **주인 확인 후.**

그 뒤: 계획 §5 소프트 런칭(허브 등록·SNS 는 주인 확인). §6 공개 대회 · §7 두 결정은
**valuein 실사용 검증으로 성능이 확인된 뒤**(주인 결정).

## 4. 주인 결정 대기

- README PR #31(포지셔닝) 머지 여부.
- 같은 초 · 같은 질의의 두 qa 기록이 파일명 충돌로 덮이는 문제(계획 §4 알려진 문제) — 영향
  범위 조사 후 결정.
- setup 이 만든 `.claude/settings.local.json` 이 새 프로젝트 검색에 in-flight 로 섞임(알려진 문제).

## 5. valuein 실사용 검증 — 사람은 쓰기만

주인이 valuein 세션을 재시작하고 평소처럼 쓴다. 판정은 사람이 하지 않는다(주인 원칙: 목적
없는 판정은 사람이 못 한다). 놓친 순간은 **selfeval v1.2 가 자동으로** 잡는다(결과 밖 파일에서
답을 찾으면 그 파일이 정답으로 저장). 할 일: 질문 판정 러너를 주기 측정(`benchmarks/cycle.py`)에
넣어 valuein 의 최근 실제 질문으로 자동 판정 + selfeval 요약을 주기 보고에 — 아직 안 함.

## 6. 감독 방식 (이번에 쓴 것)

- 구현은 별도 세션, 감독 세션은 **단계마다 보고 → 검토 → 허가**. 머지는 감독이
  `gh api -X PUT repos/curiohunter/hybrid-search-mcp/pulls/<n>/merge -f merge_method=merge`
  로 — `gh pr merge` 는 로컬 체크아웃을 바꿀 수 있어 같은 폴더를 쓰는 구현 세션과 부딪힌다.
- 머지 전 확인: 사전 등록 커밋이 측정보다 먼저인지(산출물 생성 시각까지), diff, 훅 코드는
  try/except 안인지, 배포 단계·시크릿 없음, 실명 스캔
  (`[가-힣]{2,4}(여고|여중|고등학교|중학교)`, `/Users/`), CI.
- 판정은 질문 기반(`query_judge.py`) — 판정자 보정(골드 ≥0.80, 순서만 바뀌는 변경은 합성
  1→3 순서 보정), 순서 뒤집기 재판정, degraded 0 이어야 유효. 판정은 로컬 서브에이전트만.

## 7. 함정 (이번에 실제로 밟은 것)

- **main 체크아웃에서 인덱싱 코드가 다른 브랜치를 checkout 하면** post-checkout · merge ·
  commit 훅이 그 코드로 라이브 인덱스를 재인덱싱한다. 그런 작업은 linked worktree 에서만(세 훅
  모두 worktree 에서는 빠져나간다).
- **히스토리 재작성 뒤의 옛 복제본은 `git pull` 금지** — `fetch` 후 미푸시 커밋 확인, 없으면
  `reset --hard origin/main`. 맥미니에서 실제로 미푸시 수정(#34)을 건졌다.
- **추적 해제 커밋을 pull 하면 로컬 파일도 지워진다** — 벤치 골드 파일은 무시 패턴의 로컬 파일.
- **부분 문자열로 기록 종류 판정 금지** — qa 기록은 다른 기록을 인용한다. 답은
  `owns_answer`, 노트는 frontmatter 첫 블록의 `trigger: reflector`.
- **frontmatter 파서는 여러 줄 질문의 첫 줄만** 읽는다.
- **zsh**: `"origin/$b:path"` 는 `:s` 치환 수식어로 해석된다 → `"origin/${b}:path"`.
  `pgrep -f "<패턴>"` 대기 루프는 자기 명령줄에 걸려 끝나지 않는다.
- **측정 중 src 수정 금지** — 러너는 단계마다 새 프로세스로 코드를 다시 읽는다.
- **선주입 degraded** — 임베딩 모델 콜드 로드가 제한 시간을 넘기면 BM25 전용으로 떨어진다
  (맥미니 keep_alive 로 해결). 측정에 섞이면 무효.
- **mcp 2.x** — 상한 없이 새 설치하면 서버 즉사(#34).

## 8. 어디에 무엇이

- 측정 산출물(커밋 안 함): `~/.hybrid-search/benchmarks/` — `query-judge-2026-09-26*`,
  `conv-merge-2026-09-27`, `coexist-2026-09-27`, 라벨 파일, 골드셋.
- 히스토리 재작성 전 백업: `~/.hybrid-search/backups/2026-09-24-pre-rewrite-{local,remote}.bundle`
  (검증 통과). 맥미니에 `~/backup-trust-layer-p0-p1-2026-09-27.bundle`.
- 얼린 스냅샷: `~/.hybrid-search/.cycle-snapshot` (frozen_at 2026-09-22). 측정은 사본으로.
- 경쟁 지형(2026-09-27 스캔)·전략·이번 결정은 에이전트 메모리(`project_competitive_landscape`,
  `project_open_the_doors`, `project_answerless_gate`)에.
