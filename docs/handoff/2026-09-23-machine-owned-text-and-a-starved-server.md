# 기계가 쓴 자리에 사람이 쓴 것 · 굶은 서버 · 그리고 재현된 회귀

**작성** 2026-09-23 · **브랜치** `fix/note-splice-ownership` (+ PR 두 개) ·
**전제 문서** `docs/handoff/2026-09-11-note-identity.md`, `docs/studies/2026-09-23-distillate-vs-raw-log.md`
**테스트** 1,801 통과(이 브랜치) / 1,771 통과(main 기반 PR 브랜치)
**측정** 얼린 스냅샷 2026-09-22 21:02, `--repeat 3`, 스프레드 0.00

---

## 0. 한 줄

CLAUDE.md 가 반복해서 규칙을 잃던 원인을 도구에서 고쳤고(PR #7), 공유 임베딩 서버를
상대하는 두 잘못을 고쳤고(PR #8), 10일 만에 돌린 주기 측정이 **기억축에서 실재하는
회귀**를 냈다. 회귀의 원인은 코드가 아니라 코퍼스이며, 다음 작업은 그것을
`docs/studies/2026-09-23-distillate-vs-raw-log.md` 에 **착수 전 기준으로 등록해 뒀다.**

---

## 1. 완료 — PR #7: 라우팅 블록이 사람이 쓴 규칙을 먹고 있었다

`CLAUDE.md` 의 `BEGIN/END hybrid-search-mcp routing v1` 구간은 `reindex` 자동
패치(`cli.py:494`), `setup`, `install-hook` 이 돌 때마다 통째로 템플릿으로 교체된다.
규칙을 쓰기 가장 자연스러운 자리(라우팅 표 바로 아래)가 그 안이라, 거기 적힌 규칙이
다음 `/maintain` 에 조용히 사라졌다. 여러 세션이 이 일로 며칠 소모했다.

- 블록 안에 **사람 소유 구역** `BEGIN/END hybrid-search-mcp user-additions` 신설.
- 기계 소유 구역에서 템플릿이 쓴 적 없는 줄은 **삭제 대신 그 구역으로 이사**. CLI 가
  옮긴 줄을 출력한다 — 조용함이 이 사고의 절반이었다.
- `apply_update` 가 마지막에 쓴 본문의 원본을
  `.hybrid-search/runtime/routing-body-<target>.md` 에 남긴다. 템플릿 업그레이드가
  자기가 버린 옛 줄을 사람 규칙으로 착각해 쌓지 않게.
- 레거시(pre-v1) 이관은 그대로. 그 본문은 전부 옛 도구가 쓴 것이라 건져내면 폐기된
  라우팅 표가 통째로 이사한다.
- 옛 테스트 `test_update_replaces_existing_v1_body` 는 "블록을 통째로 지운다"는
  **버그 계약**이었다. 뒤집고 회귀 4개 추가.

측정 대상 프로젝트에서 읽기 전용 dry-run: 지워질 예정이던 8줄이 전부 보존 대상으로
전환됨. **그 프로젝트 파일은 건드리지 않았다** — 다음 실행에서 도구가 처리한다.

## 2. 완료 — PR #8: 공유 로컬 임베딩 서버를 상대하는 두 잘못

**(a) 임베딩만 있는 서버에 번역 요청을 보내고 있었다.** `providers.py` 가 ollama 의
`chat_model=""` 를 적고 "임베딩만 있는 서버에 착륙하지 말라"는 주석까지 달아 뒀는데
지키는 코드가 없었다. 빈 모델명이 `"model": ""` 로 POST 되고 400. 실패는 캐시되지
않으므로 한국어가 우세한 질문마다 워커 스레드 하나 + 6초 데드라인 + 헛된 왕복.
`provider_has_chat_lane()` 로 레인을 막고, 관측 계약에 `"unavailable"` 을 추가했다
(`"skipped"` 와 구분되지 않은 것이 며칠간 이걸 숨겼다).

**(b) 벌크 한 요청이 러너를 붙잡는 동안 검색이 줄을 섰다.** `MAX_BATCH_TOKENS`
25만은 호스티드 API 한도인데 ollama 에도 적용됐다. `ProviderSpec.max_batch_tokens`
신설, ollama 만 4,096. 근거는 PR 본문과 `providers.py` 주석에 표로 남겼다.
`batch_size` 는 건드리지 않았다 — 설치 스크립트가 `config.toml` 에 `100` 을 써 넣어서
"사용자가 고른 값"과 구분되지 않는다(§1 과 같은 함정).

## 3. 측정 — 회귀는 실재하고, 원인은 코퍼스다

| 지표 | 2026-09-12 | 2026-09-23 | |
|---|---|---|---|
| Set A found | 0.90 | 0.65 | 회귀 |
| Set A **top3** | 0.70 | **0.20** | 회귀 |
| Set A mrr | 0.6002 | **0.1648** | 회귀 |
| Set B found / top3 | 0.1463 / 0.0488 | 0.122 / 0.0244 | 회귀 |
| 코드축 top5 | 0.88 | **0.92** | 개선 |
| 코드축 recall@10 · memory@3 | 0.77 · 0.16 | 0.77 · 0.16 | — |
| 밀어냄 damage | — | 0 | 라벨 없음 18건 |

`cycle.py` 의 exit 1 은 고장이 아니라 "게이트가 회귀했다"는 설계된 신호다(`cycle.py:25`).

**배제한 원인** — 넷 다 증거로 닫았다:

1. **검색·융합 코드** — 9/12 이후 `search/`·`index/` 변경은 워크트리 정규화 3곳뿐이고,
   벤치는 워크트리가 아닌 본 체크아웃을 cwd 로 넘기므로 분기가 같다.
2. **임베딩 강등** — 9/22 의 중단된 실행과 9/23 정상 실행의 Set A 가 소수점 넷째
   자리까지 같다(0.65 / 0.20 / 0.1648).
3. **supersession** — 답을 담은 레코드는 `qa/consolidated/` 에 그대로 있다.
4. 남은 것은 **희석**: 측정 사이에 대상 코퍼스 qa 가 729건 늘었다(전체의 36%).

**단일 사례 해부(`C4`)**: 답을 담은 레코드는 reflector 가 만든 **통합 레코드**이고,
순위가 2위 → 12위로 내려갔다. 1~11위는 대화 청크 2 · 원본 일일 qa 6 · 커밋 청크 2 이며
그 원본 qa 중 여럿이 9/12 이후 생긴 것이다. 즉 **증류물이 자기 재료에게 밀렸다.**

Set A 20문항: 답 소실 5 · 강등 10 · 동일 5 · **개선 0**. 개선이 하나도 없는 균일
하락이 "잡음이 아니다"의 근거다.

## 4. 다음 작업 — 착수 전 기준은 이미 등록돼 있다

`docs/studies/2026-09-23-distillate-vs-raw-log.md` 를 **먼저 읽을 것.** 게이트만 옮겨
적는다:

- **성공(둘 다)**: Set A `answer_in_top3 ≥ 0.50`, `mrr ≥ 0.40`
- **회귀 금지**: 코드축 `top5 ≥ 0.90` · `recall@10 ≥ 0.77` · `memory@3 ≥ 0.16`,
  `damage = 0`, `mean_memory_hits` 9.9 ± 0.5, 한국어 회상 쿼리 p50 ≤ 1.2s
- **기각**: 회귀 금지를 지키며 0.35 를 못 넘기면 가설이 틀렸다 → 회수 깊이 가설로 다시
  열고 그 문서에 기각을 적는다
- `limit`·깊이를 키워 통과하면 "창을 넓힌 것"으로 따로 기록하고 읽기 비용
  (`context_pack_bytes_mean`, 현재 11,967B) 증가를 함께 적는다

**두 갈래 중 고를 것** (이 세션의 권고는 후자):

| | 무엇 | 대가 |
|---|---|---|
| A | 증류물에 가중을 준다 | "reflector 산출물이 더 낫다"를 코드에 박는다. 그 가정이 틀리면 조용히 나쁜 답을 올린다 |
| B | **원본 로그가 상위 슬롯을 독점하지 못하게 자리를 나눈다** | 가정이 적고 되돌리기 쉽다. 슬롯 정책이 하나 더 늘어난다 |

B 를 먼저 재라. 기존 슬롯 기계(`_splice_lexical_memory_tail`, `_apply_memory_boost`,
`_guard_code_lane` — 모두 `search/orchestrator.py`)와 같은 자리에 붙는다.

## 5. 주의사항

- **벤치는 같은 스냅샷에서.** `benchmarks/cycle.py --no-freeze` 로 2026-09-22 21:02
  스냅샷을 재사용해야 §3 과 비교된다. 새로 얼리면 코퍼스 증가가 다시 섞여 비교가 무효다.
- **얼린 스냅샷에서 이 지표들은 결정적이다**(spread 0.00). 0.05 이상 움직이면 실재.
- **임베딩 서버는 맥미니(Tailscale)이고 `-np 1` 이다.** ollama 0.33 은 임베딩 전용
  모델에 `OLLAMA_NUM_PARALLEL` 을 적용하지 않는다(맥미니에서 plist 적용 후 무효 확인,
  원복됨). 그래서 벤치·재인덱싱이 도는 동안 대화형 검색은 굶는다. 4,096 천장은
  **필요조건일 뿐** — 경합 중엔 4k 요청도 10초였고, BM25 fail-open 이 그걸 받는다.
- **"서버가 죽었다"로 성급히 판정하지 말 것.** 이 세션이 그렇게 틀렸다. 제어
  경로(`/api/tags`, `/api/ps`)가 즉답하는데 추론만 무응답이면 러너가 먹은 게 아니라
  **우리 요청 뒤에 줄이 선 것**일 수 있다. 판정 근거는 서버 로그(응답 200 이 계속
  나오는가, 400 이 클라이언트 절단 흔적인가)이고, 그건 맥미니 쪽에서만 볼 수 있다.
  38분 진행 중이던 벤치를 정지로 오판해 죽였다 — 다시 하지 말 것.
- **잘림 경계 4,096 은 양쪽 실측이 일치한다**(맥북: 1,200 정상 / 4,200 잘림, 맥미니:
  2,700 정상 / 4,095 절단). 초과분은 **에러 없이** 버려진다.
- **사람 라벨은 덮어쓰지 않는다.** 판정 대기 밀어냄 18건:
  `python benchmarks/judge_displacements.py --report <경로>`.
- **`benchmarks/cycle.py` 는 `main` 에 없다.** 이 브랜치에만 있다. main 기반 브랜치로
  체크아웃하면 러너와 `_cycle_line` 훅이 워킹트리에서 사라진다(이 세션이 겪었다).
- **SessionStart 컨텍스트가 잘릴 수 있다.** 측정 주기 알림(`_cycle_line`,
  `hook_runtime.py:224`)이 정상 동작했는데도 이 세션에 전달되지 않았다 — 프리페치 덤프가
  먼저 오고 그 문장 중간에서 컨텍스트가 끊겼다. **싼 수정**: 행동을 요구하는 짧은 줄을
  프리페치 덤프보다 앞에 놓는다. 아직 안 했다.
- **파이썬 크래시 리포트가 뜨면 우리 것인지 먼저 보라.** 9/22 의 두 건
  (`SIGSEGV`/`SIGBUS`)은 PyMuPDF(`libmupdf pdf_drop_obj` → `fz_drop_document`)였고,
  시스템 python 3.9.6 + fitz 1.26.5 를 Warp 에서 돌린 것이다. 우리 venv 엔 `fitz` 가
  없다. 처방은 venv(3.12/3.13) + 최신 pymupdf, `doc.close()` 명시, 자식 객체 참조를
  먼저 버리기.

## 6. 관련 파일

| 파일 | 왜 |
|---|---|
| `docs/studies/2026-09-23-distillate-vs-raw-log.md` | **다음 작업의 판정 기준. 먼저 읽을 것** |
| `src/hybrid_search/search/orchestrator.py` | 슬롯·융합. B 안이 붙을 자리(`_splice_lexical_memory_tail`, `_apply_memory_boost`, `_guard_code_lane`) |
| `src/hybrid_search/memory/routing_template.py` | PR #7 — 사람 소유 구역, 이사 로직, 원본 스냅샷 |
| `src/hybrid_search/providers.py` | PR #8 — `max_batch_tokens`, `chat_model`, 측정 표 |
| `src/hybrid_search/search/translation.py` | PR #8 — `provider_has_chat_lane` |
| `src/hybrid_search/index/embedder.py` | 배치 분할(`_split_into_token_batches`), 데드라인(`_BULK_EMBED_DEADLINE`) |
| `benchmarks/cycle.py` | 주기 측정. exit 1 = 게이트 회귀 |
| `~/.hybrid-search/benchmarks/cycle/2026-09-23.json` | 이번 기준선(레포 밖) |
| `~/.hybrid-search/.cycle-snapshot/` | 비교에 재사용할 얼린 스냅샷 |

## 7. 마지막 상태

| | |
|---|---|
| 브랜치 | `fix/note-splice-ownership` (워킹트리 깨끗) |
| 마지막 커밋 | `0415749` [docs] 사전 등록 — 증류물이 날것 로그에 밀린다 |
| 열린 PR | [#7](https://github.com/curiohunter/hybrid-search-mcp/pull/7) CLAUDE.md 사람 소유 구역 · [#8](https://github.com/curiohunter/hybrid-search-mcp/pull/8) 번역 가드 + 배치 천장 |
| 테스트 | 1,801 통과(이 브랜치) / 1,771 통과(PR 브랜치) |
| 임베딩 | 맥미니 ollama 정상(임베딩 158ms, 검색 741ms). PR #8 머지 후 **Claude Code 재시작** 필요 — MCP 서버 프로세스가 옛 코드를 들고 있다 |
