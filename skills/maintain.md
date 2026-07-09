---
name: maintain
description: "hybrid-search 인덱스와 wiki를 유지보수합니다. delta reindex + stale wiki 갱신(LLM synthesis 자동 실행) + wiki gaps 생성 + CLAUDE.md 최신화."
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent
---

# Maintain — 일상 유지보수

검색과 분리된 유지보수 스킬. delta reindex + synthesis 자동화 + wiki gaps를 한 번에 처리한다.
인덱스 자체가 깨졌거나 불일치가 의심되면 `/rebuild-index`를 대신 사용할 것.

**핵심 원칙**: post-commit hook이 `reindex --synthesize`를 자동 실행하므로
`_synthesis_input/*.md`는 이미 쌓여있을 수 있다. 이 스킬은 거기서부터 이어받아
**LLM synthesis → finalize**까지 자동으로 돌린다.

**needs_synthesis flag 관리**: 이 스킬이 Step 2 → Step 4를 완료하면
`.hybrid-search/needs_synthesis` flag가 자동으로 사라진다. Step 2의 reindex는
stale이 0이면 flag를 제거하고, Step 4의 finalize는 합성 후 남은 stale을 기준으로
flag를 갱신하거나 삭제한다. 수동으로 지울 필요 없음.

## Step 1: 상태 확인

```bash
PROJECT_ROOT=$(git rev-parse --show-toplevel)
# hybrid-search-mcp 파이썬 경로: setup이 기록한 MCP 등록에서 읽는다.
VENV=$(python3 -c "import json,pathlib;print(json.load(open(pathlib.Path.home()/'.claude.json'))['mcpServers']['hybrid-search']['command'])" 2>/dev/null)
if [ -z "$VENV" ]; then
  for dir in ~/project/claude_project/hybrid-search-mcp ~/projects/hybrid-search-mcp ~/hybrid-search-mcp; do
    [ -f "$dir/.venv/bin/python" ] && VENV="$dir/.venv/bin/python" && break
  done
fi
"$VENV" -m hybrid_search.cli status --cwd "$PROJECT_ROOT"
```

## Step 2: Delta Reindex + Synthesis Prepare

```bash
"$VENV" -m hybrid_search.cli reindex --synthesize --cwd "$PROJECT_ROOT"
```

- 변경된 파일만 재인덱싱 (delta)
- stale wiki 감지 → `_synthesis_input/*.md` 자동 생성
- hook이 이미 돌렸으면 "no changed files"로 빠르게 끝남

## Step 3: Synthesis 병렬 실행 (신규 — Sonnet 에이전트)

`.hybrid-search/wiki/_synthesis_input/`에 `.md` 파일이 있으면 **Sonnet 에이전트를 모듈당 1개씩 병렬 spawn**한다.

### 3-1. input 파일 목록 확인

```bash
ls "$PROJECT_ROOT/.hybrid-search/wiki/_synthesis_input/"*.md 2>/dev/null
```

파일이 없으면 Step 4로 건너뜀.

### 3-2. 병렬 Agent 호출

input 파일 각각에 대해 Agent를 **한 메시지에 병렬로** 호출한다.

```
Agent({
  description: "Synthesize wiki: <module>",
  subagent_type: "general-purpose",
  model: "sonnet",
  prompt: <아래 템플릿>
})
```

**에이전트 프롬프트 템플릿** (파일마다 경로만 교체):

```
다음 hybrid-search wiki synthesis 작업을 수행해줘.

[입력 파일] <ABSOLUTE_PATH>/.hybrid-search/wiki/_synthesis_input/<module>.md
[출력 파일] <ABSOLUTE_PATH>/.hybrid-search/wiki/_synthesis_output/<module>.md

작업 절차:
1. 입력 파일을 Read로 읽는다. 파일에는 이미 아래가 포함되어 있다:
   - SYNTHESIS_INSTRUCTIONS (반드시 따를 규칙)
   - Deterministic Wiki (구조 데이터 — 절대 부정하지 말 것)
   - Source Code (실제 코드 청크)
   - Related Module Summaries
2. 입력 파일 안의 "Rules"와 "Output ONLY these sections" 지시를 그대로 따른다.
3. 결과물(## Overview, ## Key Design Decisions, ## Data Flow, ## Caveats, ## Related Modules만)을
   출력 파일에 Write로 저장한다. 제목(# ...)이나 metadata는 포함하지 않는다.
4. 파일:라인 인용(`file.py:L42`)은 입력 파일의 Source Code 섹션에 실제로 나온 것만 사용한다.
   존재하지 않는 심볼이나 파일은 인용하지 않는다 (verify가 제거함).
5. 기존 wiki의 언어(한국어 / 영어)를 그대로 유지한다.

완료되면 간단히 "done: <module>" 만 보고. 추가 설명 불필요.
```

**병렬 실행 예시** (input 파일 3개가 있으면):

```
메시지 하나에 Agent 호출 3개를 담아 보낸다.
subagent_type="general-purpose", model="sonnet".
description은 "Synthesize wiki: <module-name>" 형태로 각기 다르게.
```

input 파일이 많으면(5개 초과) 최대 5개씩 batch로 묶어 순차 병렬 실행한다.
모든 에이전트 완료까지 기다린 후 Step 3-3으로 진행.

### 3-3. Output 파일 존재 확인

```bash
ls "$PROJECT_ROOT/.hybrid-search/wiki/_synthesis_output/"*.md 2>/dev/null
```

에이전트 하나라도 실패해서 output이 빠져있으면 해당 모듈만 재시도(다시 Agent spawn).

## Step 4: Finalize (검증 + DB 병합)

```bash
"$VENV" -m hybrid_search.cli synthesize-wiki --finalize --cwd "$PROJECT_ROOT"
```

- `_synthesis_output/*.md`를 읽어 `verify_references`(존재하지 않는 파일:라인 제거) + `verify_symbols` 실행
- `merge_synthesis_with_structure`로 deterministic wiki와 병합
- DB `wiki_pages` 업데이트 + `_raw/<module>.raw.md` 백업
- input/output 파일 자동 정리

## Step 5: 안전망 — verify + auto-fix

```bash
"$VENV" -m hybrid_search.cli verify-synthesis --fix --cwd "$PROJECT_ROOT"
```

병합된 wiki에서 잔존 bad refs를 한 번 더 제거.

## Step 6: Wiki Gaps 채우기

`.hybrid-search/wiki-gaps.txt`가 있으면:

1. gap 목록 확인 — wiki가 없는 모듈/디렉토리
2. 중요도 판단: 파일 3개 이상인 디렉토리만 대상
3. `/bootstrap-wiki`와 동일한 형식으로 새 wiki 페이지 생성
4. `index.md` 갱신

gap이 3개 이상이면 Sonnet Agent 병렬로 생성.

## Step 7: DB 동기화 (gaps 생성 시에만)

gap으로 새 wiki를 만들었으면:

```bash
"$VENV" -m hybrid_search.cli sync-wiki --cwd "$PROJECT_ROOT"
```

finalize는 이미 DB에 썼으니 이 단계는 gap 생성 시에만 필요.

## Step 8: CLAUDE.md 확인

프로젝트의 CLAUDE.md에 `<!-- hybrid-search -->` 마커가 있는지 확인.
- 있으면: 의도 기반 라우팅 표가 최신인지 확인
- 없으면: 새 라우팅 표 삽입

## Step 9: Confidence 재캘리브레이션 (인덱스가 크게 변했을 때만)

reindex 결과가 대규모(변경 파일이 수백 개 이상, 또는 force rebuild 직후)이면
confidence 임계값이 새 점수 분포와 어긋날 수 있다. 프로젝트용 gold 배터리가
있으면 재캘리브레이션한다:

```bash
# 배터리 탐색: 프로젝트 로컬 → hybrid-search-mcp 레포 순.
HSDIR=$(dirname "$(dirname "$(dirname "$VENV")")")
GOLD="$PROJECT_ROOT/.hybrid-search/router_gold.json"
[ -f "$GOLD" ] || GOLD=$(ls "$HSDIR"/benchmarks/router_calibration/*_gold.json 2>/dev/null | head -1)
[ -n "$GOLD" ] && "$VENV" -m hybrid_search.cli recalibrate \
  --cwd "$PROJECT_ROOT" --gold "$GOLD"
```

- `strong_score`/`weak_score`/`strong_gap` 백분위와 `cosine_anchor`(중앙값)를
  `~/.hybrid-search/config.toml`에 갱신한다.
- 배터리는 실제 사용자가 칠 법한 탐색형 질문이어야 함 — 쉬운 쿼리만 모으면
  임계값이 과대해져 false-weak이 늘어난다.
- 소규모 delta reindex 후에는 건너뛴다 (분포가 거의 안 변함).

## 결과 보고

```
Maintain 완료
- Reindex: +N added, ~N changed, -N deleted
- Synthesis: N개 모듈 LLM 갱신 (Sonnet 병렬)
- Finalize: N개 병합, K bad refs 제거
- Wiki gaps: N개 생성
- CLAUDE.md: OK
```

## 주의사항

- **Sonnet 모델 고정**: synthesis 작업은 Opus가 아니라 Sonnet으로. 컨텍스트 격리 + 속도 이득.
- **에이전트 병렬 호출은 단일 메시지**: Agent tool 호출 여러 개를 한 메시지에 담아야 실제로 병렬 실행됨.
- **finalize가 verify를 자동 수행**: 에이전트가 환각 ref를 써도 CLI가 제거하니 관대하게 가도 됨. 단 너무 많으면 재시도.
- **실패 모듈만 재시도**: output 파일이 없는 모듈만 Agent 재spawn. 전체 재실행 금지.
