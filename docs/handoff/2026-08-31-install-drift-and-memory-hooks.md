# 설치본 드리프트와 memory hooks 공백

**작성** 2026-08-31 · **상태** 재시작 검증 대기 · **선행 커밋** `c95f7c4`

---

## 한 줄 요약

`git pull`은 **레포만** 갱신한다. Claude Code가 실제로 띄우는 MCP 서버는
`~/.local/share/uv/tools/memory-layer-mcp/`의 **별도 복사본**이고, 이번에
그 복사본이 **4커밋 넘게 뒤처진 채 OpenAI로 붙고 있었다.**

---

## 이번 세션에서 한 일

| 항목 | 결과 |
|---|---|
| `git pull` | `a3ea92c` → `c95f7c4`, fast-forward 5커밋 |
| 테스트 | pull 직후 **1510 passed**, 수정 후 **1511 passed** (167.6s) |
| 훅 재설치 | 3개 프로젝트 post-commit/checkout **updated(v2)** + **post-merge 신설** |
| 스키마 v9 | 양쪽 DB `modules.name_vector` 컬럼 마이그레이션 |
| name_vector 충전 | gate-ripgrep 32/32, valueinmath-web 382/382 |
| MCP 설치본 | v8 → **v9** (`uv tool install --force --no-cache --from .`) |
| 이 레포 memory hooks | **4/4 설치** → `.claude/settings.local.json` |
| 이 레포 인덱싱 | 신규 등록, 293파일 / 2818청크 / 1322.7s |
| `_record_vector_space` 결함 | **수정** — 신규 인덱스가 fingerprint를 기록한다 |

---

## 발견 1 — 설치본이 레포와 분리돼 있다

### 증상

pull·테스트·훅 재설치를 전부 마친 뒤에도 `hybrid_search` MCP 호출이
이렇게 죽었다.

```
OpenAI API error 401: The OpenAI account associated with this API key
has been deactivated.
```

`~/.hybrid-search/config.toml`은 `backend = "ollama"`다. 설정과 실행이
어긋나 있었다.

### 원인

MCP 등록(`~/.claude.json`)이 가리키는 것은 레포가 아니다.

```json
"hybrid-search": {
  "command": "/Users/ian/.local/share/uv/tools/memory-layer-mcp/bin/python",
  "args": ["-m", "hybrid_search.server"]
}
```

`uv-receipt.toml`을 보면 이 도구는 이 레포 디렉터리에서 설치됐지만
**editable이 아닌 복사본**이다. pull은 이 복사본에 도달하지 않는다.

pull 직후 복사본 실측:

| 항목 | 레포 | 설치본 |
|---|---|---|
| `SCHEMA_VERSION` | 9 | **8** |
| `name_vector` 참조 | 11건 | **0** |
| `post-merge` 참조 | 9건 | **0** |
| `base_url` 참조 | 2건 | **0** |

`backend = "ollama"`를 무시하고 OpenAI로 떨어진 것이 근거다 — 설치본은
Ollama 공급자를 추가한 `a3ea92c`**보다도 이전**에 빌드됐다. 즉
`fc39c4a` 이하 시점에 멈춰 있었다.

### 두 번 속은 지점 — uv 캐시

첫 시도는 **성공한 것처럼 보였지만 아무것도 안 바뀌었다.**

```bash
uv tool install --force --from . memory-layer-mcp   # ← 여전히 v8
```

`pyproject.toml`의 버전이 그대로라 uv가 **캐시된 휠을 재사용**한다.
`--force`는 재설치를 강제할 뿐 재빌드를 강제하지 않는다.

```bash
uv tool install --force --no-cache --from . memory-layer-mcp   # ← v9
```

**`--no-cache`가 없으면 커밋된 코드가 도구에 영원히 도달하지 못한다.**
`ecae799`가 훅 버전 마커로 고친 것과 **정확히 같은 계열의 결함**이
패키지 설치 층위에서 반복됐다.

### DB 안전성

설치본 v8이 이미 v9로 올라간 DB를 여는 상황이 발생했지만 손상은 없다.
v8의 `_migrate_schema`는 첫 줄이 이렇다.

```python
if cur_ver >= target_ver:
    return
```

다운그레이드를 시도하지 않고 그냥 빠져나온다. `name_vector`를 안 쓰고
지나갈 뿐이다.

---

## 발견 2 — memory hooks: status가 보는 곳과 실제 설치 위치가 다르다

`status` 출력:

```
✗ Claude memory hooks: 0/4  (none)
```

### 4개가 무엇인가

전부 `hybrid_search.cli qa-hook` 하나를 4개 이벤트에 물린 것이다
(`src/hybrid_search/hooks.py:596-636`). CLAUDE.md의 "자동 동작
(수동 개입 불필요)" 절이 그대로 이것이다.

| 이벤트 | 하는 일 |
|---|---|
| `UserPromptSubmit` | 질문 시점에 관련 과거 Q&A 주입 |
| `SessionStart` | 세션 시작 시 최근 Q&A 요약 주입 |
| `Stop` | 답변 종료 시 `.hybrid-search/qa/`에 저장 |
| `PreToolUse` (`Grep\|Read`) | Grep/Read 직전 메모리 주입 |

### 숫자가 오해를 부르는 이유

`cmd_status`(`cli.py:1426-1434`)는 **글로벌 두 파일만** 본다.

```python
_claude_memory_hook_status([
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
])
```

그런데 `cmd_setup`(`cli.py:4930-4937`)은 **프로젝트** settings에 설치한다.
설치 위치를 status가 안 보므로, 정상 설치된 프로젝트도 `0/4`로 뜬다.

실측:

| 위치 | memory hooks |
|---|---|
| `~/.claude/settings.json` | 없음 (setup hooks 4개만: auto_index, stale, gaps, route) |
| `valueinmath-web/.claude/settings.local.json` | **4/4 설치됨** |
| `hybrid-search-mcp/.claude/settings.*` | **없음** |

### 그래서 실제 상태는

이 레포에 한해서는 `0/4`가 **사실이었다.** 증거는 qa 로그의 시간 분포다.

```
valueinmath-web/.hybrid-search/qa/   총 60개, 최신 2026-08-31 11:42
hybrid-search-mcp/.hybrid-search/qa/ 2026-07-27 이후 비어 있음
```

**도구를 만드는 레포에서만 그 도구를 안 쓰고 있었다.**

### 설치 완료 (2026-08-31)

```bash
hybrid-search-mcp install-memory-hook --cwd /Users/ian/projects/hybrid-search-mcp
# → installed 4 hook block(s) → .claude/settings.local.json
```

`settings.local.json`은 `~/.config/git/ignore`가 잡고 있어 커밋되지 않는다 —
valueinmath-web과 같은 위치다.

**한 가지 다르다.** python 경로가 uv 설치본이 아니라 이 레포의 `.venv`다.

| 프로젝트 | 훅이 부르는 python |
|---|---|
| valueinmath-web | `~/.local/share/uv/tools/memory-layer-mcp/bin/python` |
| hybrid-search-mcp | `<repo>/.venv/bin/python` |

이 레포에는 이쪽이 **맞다.** `.venv`는 editable 설치
(`_editable_impl_hybrid_search_mcp.pth`)라 git HEAD를 직접 따라간다 —
발견 1의 설치본 드리프트가 구조적으로 발생하지 않는다.

---

## 발견 3 — 훅을 깔았더니 두 결함이 연달아 드러났다

훅을 설치하고 **실제로 실행해본 것**이 아니었다면 둘 다 못 봤다.

### 3-1. 인덱스 없는 프로젝트 → 다른 프로젝트 내용이 주입된다

이 레포에 대한 질문을 `UserPromptSubmit`에 넣었더니:

```
[hybrid-search pre-fetch] 10 hits.  confidence: mixed
1. archive/learning-legacy-2026-05/hooks/use-learning-logs.ts
2. AGENTS.md:2
3. archive/dead-code-2026-07/hooks/use-analytics.ts
```

**전부 valueinmath-web 파일이다.** 이 레포가 미등록이라 검색이 전체
프로젝트로 떨어졌고, 유일하게 인덱싱된 코퍼스가 답으로 왔다.

훅이 조용히 실패한 게 아니라 **엉뚱한 프로젝트를 사실인 양 주입했다.**
게다가 60초를 넘겨, 훅 timeout 10초에서는 매번 잘렸을 것이다.

→ 이 레포를 인덱싱해 해소 (293파일 / 2818청크 / 22분).

### 3-2. 신규 인덱스에 `embedding_fingerprint`가 기록되지 않는다 — 결함

인덱싱 직후 훅이 이렇게 답했다.

```
pre-fetch confidence: weak · DEGRADED: this index was built with a
different embedding model ... BM25 (keyword) lane only.
```

방금 Ollama로 만든 인덱스인데 "다른 모델"이라고 한다. 실측:

```
2b7ffb2eba5eac38 (hybrid-search-mcp):  (없음)      ← 키 자체가 부재
754feac95bdce325 (gate-ripgrep):       ollama:qwen3-embedding:0.6b:1024
8d221e11dbde3d01 (valueinmath-web):    ollama:qwen3-embedding:0.6b:1024
```

원인은 `src/hybrid_search/index/pipeline.py:475-482`.

```python
def _record_vector_space(self, db, full_rebuild: bool) -> None:
    ...
    if full_rebuild:
        db.set_meta(EMBEDDING_FINGERPRINT_KEY, fingerprint)
        return
    stored = db.get_meta(EMBEDDING_FINGERPRINT_KEY)
    if not vector_space_matches(stored, fingerprint):
        # 마커를 일부러 건드리지 않는다 → 벡터 레인 off
        logger.warning(...)
```

fingerprint를 **`full_rebuild`일 때만** 쓴다. `index <path>`는 프로젝트가
새것이어도 증분 경로를 탄다. 그래서 `stored`가 `None`이고,
`vector_space_matches(None, ...)`가 False라 마커를 안 남긴 채 경고만 찍는다.

**결과: 갓 만든 인덱스가 즉시 "혼합 벡터 공간"으로 판정되어 벡터 레인이
영구히 꺼진다.** 이 프로젝트의 존재 이유인 하이브리드 검색이 BM25 단일
레인으로 떨어진다.

판정이 사실과 다르다는 근거는 인덱싱 로그 자체에 있다.

```
Done: +293 added, ~0 changed, -0 deleted
```

전부 추가, 변경·삭제 0 — **한 모델·한 번의 실행**이다. 섞일 수가 없다.

### 처음 세운 수정안은 틀렸다 — `not stored`를 쓰지 말 것

이 문서의 초판은 조건을 `full_rebuild or not stored`로 하자고 적었다.
**그대로 넣으면 원래 결함보다 나쁘다.** `providers.py:143-147`이 이유를
이미 적어두었다.

> 마커가 없는 인덱스는 "알 수 없음"이 아니라 **정확히 구형 OpenAI
> 인덱스**다. 알 수 없음으로 취급하는 것이야말로 공급자 전환이 벡터
> 공간을 조용히 넘나들게 만든다.

`not stored`로 판별하면 **구형 OpenAI 인덱스에 Ollama 마커를 찍는다.**
비교 불가능한 벡터로 코사인을 계산하게 되고, 그것을 막으려고 존재하는
가드가 스스로 꺼진다.

신규와 구형은 **마커 부재가 똑같다.** 갈리는 것은 청크 수다.

| 상태 | 마커 | 청크 | 옳은 판정 |
|---|---|---|---|
| 신규 프로젝트 | 없음 | **0** | stamp — 잘못 라벨링할 벡터가 없다 |
| 구형 인덱스 | 없음 | 있음 | stamp 안 함 — 가드 유지 |

### 적용한 수정 (2026-08-31)

`src/hybrid_search/index/pipeline.py:479`

```python
if full_rebuild or db.get_chunk_count(project_id) == 0:
    db.set_meta(EMBEDDING_FINGERPRINT_KEY, fingerprint)
    return
```

호출 지점(`pipeline.py:342`)이 청크가 쓰이기 **전**이라, 이 검사는 진입
시점의 인덱스를 본다.

검증:

- **신규 프로젝트 end-to-end** — 임시 레포를 `index <path> --yes`(force
  없이)로 인덱싱 → `embedding_fingerprint = ollama:qwen3-embedding:0.6b:1024`
  기록됨.
- **legacy 회귀** — 마커를 지우고(청크 3개 잔존) 증분 재인덱싱 →
  여전히 비어 있음. 가드 유지.
- 전체 **1511 passed**.

기존 단위 테스트 4건은 `db`가 `MagicMock`이라 `get_chunk_count`가 0이
아닌 값을 우연히 반환하는 데 기대고 있었다. `_db(chunks=…, stored=…)`
헬퍼로 의도를 명시하고 `test_first_index_stamps_even_unforced`를 추가했다.
`test_incremental_over_a_legacy_index_does_not_stamp`가 **emptiness 조건이
삼키면 안 되는 케이스**임을 docstring에 남겼다.

### 그 전에 쓴 임시 조치

이 레포의 인덱스는 수정 이전에 만들어졌으므로, `--force` 재빌드(22분
재임베딩)를 피하고 `--force`가 기록했을 값을 직접 넣었다.

```python
db.set_meta(EMBEDDING_FINGERPRINT_KEY, "ollama:qwen3-embedding:0.6b:1024")
```

조치 후 훅 재실행 — DEGRADED 사라지고 이 레포 파일이 나온다.

```
[hybrid-search pre-fetch] 10 hits.  confidence: mixed
1. docs/handoff/2026-08-31-install-drift-and-memory-hooks.md
2. benchmarks/memory_gold.json:1
3. src/hybrid_search/memory/memory_types.py
[소요 0초]
```

이 값은 **이미 만들어진 인덱스를 되살린 것일 뿐**이고, 새로 만드는
프로젝트를 지키는 것은 위의 코드 수정이다.

### 인접 사실 — `index`는 `--cwd`를 받지 않는다

`reindex`·`install-hook`·`install-memory-hook`은 전부 `--cwd`인데
`index`만 위치 인자다.

```
usage: hybrid-search-mcp index [-h] [--force] [--wiki] [--yes] [path]
```

`index --cwd <path>`는 argparse 에러로 죽는다. `tail` 파이프를 통과시키면
종료 코드가 0이 되어 **성공한 것처럼 보인다.** 실제로 이번에 한 번
속았다.

### 확인해봤으나 결함이 아니었던 것 — 콜그래프

인덱싱 로그의 `Call graph: 0 resolved (0 extracted + 0 inferred),
13320 unresolved`가 의심스러워 DB를 직접 셌다.

| 프로젝트 | 해소 / 전체 |
|---|---|
| hybrid-search-mcp | 4308 / 17628 (**24%**) |
| gate-ripgrep | 3404 / 15233 (22%) |
| valueinmath-web | 6321 / 39424 (16%) |

세 프로젝트 중 가장 높다. 로그의 `0 resolved`는 증분 패스 표시이고
누적 상태가 아니다. **결함 아님.**

---

## 하지 말 것 — 전체 재인덱싱

`name_vector`를 채우려고 `reindex`를 돌렸다. **10분 타임아웃에 죽었고
결과는 0/382였다.** 파이프라인 순서 때문이다
(`src/hybrid_search/index/pipeline.py:381-403`).

```
파일 1651개 스캔·해시 → 콜그래프 재해석 → 모듈 재발견 → 모듈 합성
                                                        ↑ 필요한 건 여기 하나
```

앞 세 단계가 시간을 다 쓰고 마지막에 도달하지 못한다.

`synthesize_modules`는 DB의 모듈 레코드만 읽는다 — 파일 스캔도 콜그래프도
타지 않는다. 직접 호출하면 된다.

```python
from hybrid_search.index.module_synth import synthesize_modules
synthesize_modules(db, project_id, embedder=Embedder(cfg.embedding))
```

```
{'modules': 382, 'synthesized': 77, 'skipped': 305, 'embedded': 382}  (156.0s)
```

**156초.** 기존 청크는 재임베딩되지 않는다. 마이그레이션이 컬럼만 만들고
값은 못 만드는 상황에서, 필요한 것은 합성 패스 하나뿐이다.

---

## 임베딩 — 이 기계는 그대로 두면 된다

이 기계가 맥미니 본체(`bagseogdon-ui-Macmini`)이고 Ollama가 여기서 직접
돈다 (pid 1754, `*:11434`, `qwen3-embedding:0.6b`).

- `cfc4a74`의 `embedding.base_url`은 **맥북용**이다. 서버 본인은 설정할 게
  없다. 미설정 → 공급자 기본값 `http://localhost:11434/v1`.
- `embedding_fingerprint = ollama:qwen3-embedding:0.6b:1024` **변동 없음**
  → 코퍼스 전체 재임베딩은 발생하지 않는다.

---

## 재시작 후 확인 절차

현재 떠 있는 MCP 프로세스(pid 84926 / 85549, 11:22·11:33 시작)는 여전히
구코드다. **재시작해야 v9로 뜬다.**

### 1. MCP 검색이 Ollama로 붙는가 — 최우선

새 세션에서 `hybrid_search`를 호출한다.

- **성공 기준**: 결과가 돌아온다.
- **실패 신호**: `OpenAI API error 401 ... account_deactivated`
  → 설치본이 아직 구코드다. `--no-cache`로 재설치했는지 확인할 것.

설치본 단독 검증은 재시작 없이도 된다(이미 통과함).

```bash
/Users/ian/.local/share/uv/tools/memory-layer-mcp/bin/python \
  -m hybrid_search.cli search "출결 기능" --cwd /Users/ian/projects/valueinmath-web --limit 2
```

```
Query: KOREAN_NL | BM25w: 0.15 | 523ms | 14836 chunks
  1. [RRF=0.0000] module:learning/attendance      ← name_vector 효과
```

### 1-2. memory hooks 4개가 실제로 도는가 — 이번 세션 신규

새 세션을 열면 이런 컨텍스트가 자동 주입돼야 한다.

```
[hybrid-search memory] N past turns available.       ← SessionStart
[hybrid-search route] suggest hybrid_search · ...    ← UserPromptSubmit
[hybrid-search pre-fetch] N hits. Top paths: ...
```

- **성공 기준**: pre-fetch 경로가 **이 레포 파일**이어야 한다
  (`src/hybrid_search/...`, `docs/...`).
- **실패 신호 1**: `archive/...`, `AGENTS.md` 등 valueinmath-web 경로가
  나오면 인덱스 해석이 다시 어긋난 것이다 (발견 3-1).
- **실패 신호 2**: `DEGRADED: ... different embedding model` →
  fingerprint가 없다. 3-2를 고쳤으므로 **수정 이후 만든 인덱스에서는
  나오면 안 된다.** 나온다면 수정이 이 경로를 못 덮은 것이다. 수정 이전에
  만들어진 인덱스라면 그 프로젝트만 `index . --force`로 재빌드하면 된다.

세션 종료 후 저장 확인:

```bash
ls -lt .hybrid-search/qa/2026/08/ | head -3
```

기대: 오늘 날짜 파일이 새로 생긴다. 7/27 이후 비어 있던 디렉터리다.

### 2. name_vector가 살아 있는가

```bash
for p in 754feac95bdce325 8d221e11dbde3d01; do
  sqlite3 ~/.hybrid-search/projects/$p/store.db \
    "SELECT COUNT(*) FROM modules WHERE name_vector IS NOT NULL;"
done
```

기대: `32`, `387`(델타 재인덱싱으로 382에서 증가), 그리고
`2b7ffb2eba5eac38`(이 레포)은 `18`. `bdf9bbe`의 한국어 쿼리 회귀도 같이
볼 것 — 문제은행 / 상담 / 포털 / 출결이 각각 1위여야 한다.

fingerprint도 세 프로젝트 전부 있어야 한다.

```bash
for d in ~/.hybrid-search/projects/*/; do
  sqlite3 $d/store.db \
    "SELECT value FROM index_meta WHERE key='embedding_fingerprint';"
done
```

기대: `ollama:qwen3-embedding:0.6b:1024` 세 줄. **빈 줄이 있으면
발견 3-2가 재발한 것이다.**

### 3. post-merge 훅이 실제로 도는가

```bash
git pull   # 또는 머지
```

기대: 백그라운드 재인덱싱이 뜬다. `ecae799` 이전에는 아무 신호도 없었다.

### 4. 워크트리 가드

워크트리에서 커밋했을 때 등록 프로젝트 수가 **늘지 않아야** 한다.

```bash
hybrid-search-mcp status | grep "All registered"
```

기대: **3개** 유지 (gate-ripgrep, hybrid-search-mcp, valueinmath-web).
이번 세션에서 이 레포가 등록되어 2 → 3이 됐다. 그 이상으로 늘면
`ecae799`의 워크트리 가드가 새는 것이다.

---

## 열린 항목

### A. `_record_vector_space`의 신규 인덱스 처리 — **수정 완료**

발견 3-2. `full_rebuild or db.get_chunk_count(project_id) == 0`으로
고쳤고 테스트 1511 passed. 초판에 적었던 `not stored` 안은 폐기했다 —
이유는 3-2 본문에 남겼다.

**남은 확인**: 다른 기계(맥북)의 인덱스들도 마커가 있는지 봐야 한다.
같은 결함으로 비어 있다면 벡터 레인이 꺼진 채 돌고 있다.

### A-2. 이 레포 memory hooks — **설치 완료**

7월 27일 이후 이 레포의 대화가 하나도 기억되지 않았다. 이제 축적되지만
**소급되지는 않는다** — 7/27~8/31 구간은 영구 공백이다.

### B. `status`의 memory hooks 카운트 — 결함 후보

글로벌만 보면서 `0/4`를 단정한다. 프로젝트 settings에 정상 설치된 경우도
`✗ 0/4 (none)`으로 뜬다. `cmd_setup`이 쓰는 위치와 `cmd_status`가 읽는
위치를 맞추거나, 출력에 스코프를 명시해야 한다.

### C. `docs/handoff/2026-08-29-result-slot-allocation.md` — 설계 대기

결과 슬롯 배분 재설계. 이번 세션에서 손대지 않았다.

### D. 설치본 드리프트의 항구적 방지 — 미착수

이번엔 사람이 알아채서 고쳤다. 다음에도 알아챈다는 보장이 없다.
`status`에 **설치본 버전 대 레포 버전** 비교 한 줄을 넣는 것이 최소 방어다.
`SCHEMA_VERSION`만 비교해도 이번 건은 잡혔다.

---

## 인용한 사실의 확인 시점

모든 수치·경로·코드 위치는 2026-08-31 세션에서 실측했다. 프로세스 ID와
`etime`은 그 시점 기준이므로 재시작 후 무효다.
