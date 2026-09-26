# 질문으로 판정한다 — 쌍 라벨 대신 "이 결과가 그 질문에 답하나"

**상태**: 계획. 구현은 별도 세션에서. 이 문서만 읽고 시작할 수 있게 썼다.
**결정 (2026-09-25, 주인)**: 게이트(B) 채택 판정을 **쌍 라벨의 damage 0** 대신
**실제 질문 기반 LLM 판정**으로 한다. 사람 판정은 쓰지 않는다.

---

## 1. 왜 바꾸나

**쌍 라벨은 목적이 빠진 판정이다.** 지금까지 supersession 의 품질은 "옛 기록 A 를 새
기록 B 로 바꿔 끼운 것이 맞나"를 쌍 단위로 라벨해서 쟀다. 2026-09-24 에 주인에게 37쌍을
판정해 달라고 브라우저 시트를 띄웠고, 주인의 답은 요지가 이랬다: 판정은 **어떤 목적으로
검색했고 그 결과가 목적에 맞는가**를 LLM 이 직접 봐야 의미가 있다, 무엇을 찾던 검색인지
모르는 사람이 쌍을 보고 매길 수는 없다.

맞다. 쌍만 보면 **무엇을 찾던 검색인지**가 없다. 9/12 에도 "주인은 사용자지 채점 직원이
아니다"로 정리했는데 같은 실수를 반복했다. 게다가 쌍 판정자는 믿을 수 없었다 — DeepSeek
κ 0.80 → 0.53, 로컬 모델은 사람 legitimate 2/2 를 damage 로(연구 문서 §16·§22).

**질문 기반 지표는 이미 B 가 낫다고 말한다.** B 를 막는 것은 쌍 라벨 하나뿐이었다.

| 지표 (얼린 스냅샷, 2026-09-23~24) | main (A + 재정렬 실물 규칙) | B + 재정렬 실물 규칙 |
|---|---|---|
| Set A found · top3 · mrr | 0.80 · 0.70 · 0.6238 | 0.80 · **0.75 · 0.6738** |
| Set B found · top3 | 0.122 · 0.0244 | 0.122 · 0 (`B35` 2위→8위, 정보 손실 아님) |
| 코드축 top5 · recall@10 · memory@3 | 0.92 · 0.77 · 0.16 | 동일 |
| 밀어냄 감사 공통 573 프로브 자기회수 | 554 | **561** |
| 쌍 라벨 damage (감사 창 안에서 발동한 것) | 0 | **1** ← B 를 막던 유일한 것 |

그리고 그 "damage 0" 도 허상이었다 — 지금 맵에 사람 라벨 damage 쌍 4개가 **잠복**해 있고,
감사 창 안에서 발동하지 않았을 뿐이다(§22).

## 2. 무엇을 만드나 — 질문 기반 쌍대 판정

**한 줄:** 과거에 실제로 던진 질문을 두 버전(main / B)에 검색하고, 상위 10이 달라지는
질문만 골라, LLM 에게 "이 질문에 답하려면 어느 목록이 낫나"를 가려서 묻는다.

### 2.1 질문 집합
- **밀어냄 감사 프로브** — `benchmarks/displacement_audit.py` 의 `_probe_set`: 코퍼스의
  답 있는 qa 레코드의 질문(`query:`). valuein 홀드아웃(2026-09-12 이후) 573개.
  사용자가 실제로 친 질문이다.
- 보조: Set A (20) · Set B (41) 골드 질문 — `~/.hybrid-search/benchmarks/valuein_conv_gold*.json`.
  이들은 이미 문구 기반 채점이 있으니 **판정자 보정용**으로 쓴다(§2.4).

### 2.2 두 갈래
- **M** = main 코드, 스냅샷 사본 그대로 (supersession 재계산만).
- **G** = B 브랜치 코드(main 병합 후), 스냅샷 사본을 이주(`_purge_withheld_memory`) 후
  supersession 재계산.
- 둘 다 같은 얼린 스냅샷 `~/.hybrid-search/.cycle-snapshot` 에서 **사본**을 떠서 쓴다.
  원본 스냅샷과 cycle 기록은 건드리지 않는다. 두 갈래는 **같은 시간대에** 돌린다
  (최근 활동 레인이 라이브 qa 파일을 읽는다).

### 2.3 판정 대상 추리기
- 질문마다 M · G 의 상위 10 을 뽑는다 (`limit=10`, `HYBRID_SEARCH_IN_FLIGHT=0`,
  `clock.pin_to_snapshot`).
- **상위 10 의 구성원 또는 순서가 다른 질문만** 판정한다. 같으면 무승부로 센다(판정 안 함).
- 예상 규모: 수십~백여 개. 전부 판정한다(표본 추출 없음).

### 2.4 판정자
- **로컬만.** 코퍼스는 비공개이고 사람 이름이 섞여 있다. 외부 API(DeepSeek, Jev 등)로
  보내지 않는다. 이 레포 세션의 모델을 서브에이전트로 띄워 판정시킨다(원문이 메인 대화에
  쌓이지 않게).
- **입력:** 질문 + 목록 X + 목록 Y. 각 목록 항목은 `node_type`, 경로, 스니펫(또는 qa 의
  질문·답 앞부분 ~400자). **어느 쪽이 G 인지 가린다.** X/Y 배정은 질문마다 시드 고정
  난수(`20260925`)로 섞는다.
- **출력:** `{"q": id, "better": "X|Y|same", "why": "≤25자"}`. "same" 허용 — 억지로 고르지 않게.
- **판정문 (초안):**

  > 사용자가 아래 질문으로 과거 기록을 검색했다. 두 검색 결과 목록 X, Y 가 있다.
  > 이 질문에 답하는 데 필요한 내용을 **더 위에, 더 온전히** 보여주는 쪽을 골라라.
  > 판단 근거: (1) 질문에 직접 답하는 기록이 있는가, 몇 위인가 (2) 질문과 다른 일을 다룬
  > 기록이 답을 밀어냈는가 (3) 낡은 답보다 최신 답이 먼저인가 — 단, 같은 일에 대한
  > 최신일 때만. 비슷하면 same. 출력은 JSON 배열만.

- **판정자 보정 (측정 전 필수).** Set A 20 · Set B 41 골드 질문에서 M · G 목록이 다른
  질문에 대해 판정자의 선호가 **문구 기반 채점(첫 정답 순위)의 선호와 일치하는 비율**을
  잰다. **일치율 ≥ 0.80 이어야** 프로브 판정을 채택 근거로 쓴다. 미달이면 판정문을
  고치고 다시 잰다 — 고친 판정문은 문서에 남긴다. (쌍 판정 때 κ 를 안 재고 쓰다가
  두 번 틀렸다.)

### 2.5 순서 편향 확인
- 같은 질문을 X/Y 를 **뒤바꿔** 한 번 더 판정해서, 판정이 뒤집히지 않는 비율을 보고한다.
  뒤집히는 질문은 "same" 으로 센다.

## 3. 사전 등록 — 측정 전에 이 절을 확정하고 커밋할 것

**채택 (B, 전부 충족):**
1. 판정자 보정 일치율 ≥ 0.80 (§2.4).
2. 프로브 판정에서 **G 승 ≥ M 승** (same·무승부 제외). 승패 수와 비율을 같이 보고.
3. Set A found · top3 · mrr ≥ main, 코드축 세 지표 ≥ main.
4. Set B found ≥ main. top3 는 **0.05 이상 떨어지지 않을 것** (결정적 지표라 spread 0,
   한 문항 = 0.0244).

**기각:** 위 하나라도 미달. 특히 2 에서 G 가 지면, 진 질문들을 읽고 원인을 적는다 —
"쓰레기가 가림막이었다"(게이트 문서 §9.3) 같은 숨은 결함이 또 있을 수 있다.

**보고만 (기준 아님):** 쌍 라벨 damage 수(참고치로 격하), 밀어냄 감사 자기회수, 판정자가
"same" 으로 둔 비율, 순서 뒤집기 불일치율, 진 질문 표본의 원인 분류.

**예상:** G 승이 많다 — Set A 와 자기회수가 이미 G 쪽이다. 예상이 틀리면 그것도 결과다.

## 4. 구현 절차

### 4.1 B 브랜치를 main 에 맞춘다 — 반드시 linked worktree 에서
`feature/answerless-memory-gate` 에는 PR #19 (재정렬 실물 규칙)·#20 이 아직 없다.

```bash
git worktree add ../hsm-gate feature/answerless-memory-gate
cd ../hsm-gate && git merge main          # worktree 안에서 병합·커밋·측정 전부
```

**main 체크아웃에서 B 브랜치를 절대 체크아웃하지 말 것.** 이 레포에는 post-checkout ·
post-merge · post-commit 훅이 깔려 있고, main 체크아웃에서 돌면 **그때 체크아웃된 코드로**
라이브 인덱스를 재인덱싱한다. B 코드가 체크아웃된 채 훅이 돌면 라이브 인덱스가 이주된다
— 2026-09-23 에 `git checkout feature/answerless-memory-gate` 한 번으로 그렇게 됐다.
세 훅 모두 linked worktree 에서는 즉시 빠져나가므로 worktree 안의 작업은 안전하다.

이주됐는지 확인:

```bash
sqlite3 ~/.hybrid-search/projects/<project_hash>/store.db \
  "select value from index_meta where key='memory_gate'"   # 결과가 1 이면 이주됨
```

복원: `files` 에서 `chunk_count=0` 이고 경로가 `.hybrid-search/qa/%` 또는
`.hybrid-search/memory/cards/%` 인 행과 `index_meta` 의 `memory_gate` 를 지운 뒤, main
체크아웃에서 `hybrid-search-mcp index .` (다시 임베딩 ~100 파일, 수십 원).

### 4.2 스냅샷 사본 (갈래마다)
```bash
cp -R ~/.hybrid-search/.cycle-snapshot <scratch>/snap-<label>
sed "s#data_dir = \"$HOME/.hybrid-search/.cycle-snapshot\"#data_dir = \"<scratch>/snap-<label>\"#" \
  ~/.hybrid-search/.cycle-snapshot/config.toml > <scratch>/snap-<label>/config.toml
```
사본 config 의 `data_dir` 이 바뀌었는지 반드시 확인(안 바뀌면 원본을 덮는다). 사본 하나 ~400MB.

### 4.3 G 사본 이주 (B worktree 에서, 삭제만 · 비용 0)
```python
# B worktree 에서 실행 (sys.path 에 그 worktree 의 src). 임베더는 차원만 얻고 호출하지 않는다.
cfg = load_config(Path("<scratch>/snap-g/config.toml"))
dim = Embedder(cfg.embedding, cfg.models_dir).embedding_dim
for p in ProjectRegistry(cfg.global_dir).list_all():
    idx = IndexPaths(get_project_dir(cfg.projects_dir, p.id))
    if not idx.store_db.exists(): continue
    db, vec, bm25 = StoreDB(idx.store_db), VectorEngine(idx.vectors_dir, dim), BM25Engine(idx.tantivy_dir)
    IndexingPipeline._purge_withheld_memory(None, db, vec, bm25, p.id)
    bm25.commit(); vec.save(); db.close()
```
그다음 각 갈래에서 `python benchmarks/recompute_supersession.py --config <사본>/config.toml`.

### 4.4 러너
- 새 러너 `benchmarks/query_judge.py` (러너만 커밋, 산출물은 커밋 금지 — CLAUDE.md
  "공개물에 코퍼스를 인용하지 말 것"). 산출물은 `~/.hybrid-search/benchmarks/` 아래.
- 한 프로세스가 두 코드 버전을 동시에 못 싣는다 → 갈래마다 **자기 코드 체크아웃에서**
  목록을 뽑아 JSON 으로 쓰는 단계(`--dump`)와, 두 덤프를 읽어 다른 질문만 추려 판정 사례
  파일을 만드는 단계(`--pair`)로 나눈다. 판정은 서브에이전트, 집계는 `--score`.
- 덤프는 `displacement_audit._probe_set(db, pid, sample=600, seed=…, since="2026-09-12")`
  를 재사용해 두 갈래가 **같은 질문 집합**을 쓰게 한다. 단 G 사본에서는 답 없는 레코드가
  빠져 프로브 집합이 달라질 수 있으니, **M 에서 뽑은 질문 목록을 파일로 고정**해 G 에도
  그대로 쓴다.

### 4.5 판정 실행
- 사례 파일을 서브에이전트에 주고 판정문(§2.4)으로 판정, 결과를 로컬 파일로만.
  원문을 메인 대화나 최종 보고에 인용하지 않게 지시한다.
- 보정(골드) → 확정 → 프로브 → 뒤바꿔 재판정 순.

## 5. 이전 세션이 밟은 함정 (반복하지 말 것)

- **부분 문자열로 기록 종류를 판정하지 말 것.** qa 레코드는 다른 레코드를 인용한다.
  `LIKE '%trigger: reflector%'` 로 노트를 세다가 인용된 문자열을 잡았다. 답 소유는
  `memory/qa_shape.owns_answer`, 노트는 frontmatter 첫 블록의 `trigger: reflector`.
- **frontmatter 파서는 여러 줄 질문의 첫 줄만 읽는다.** 붙여넣은 긴 질문의 본문은
  `# Q:` 줄부터 `- **query_type**` 전까지다.
- **`pgrep -f "<패턴>"` 대기 루프는 자기 명령줄에 걸린다** — 끝나지 않는다.
- **main 체크아웃에서 B 브랜치를 체크아웃하지 말 것** (§4.1).
- **측정 중에 `src/` 를 고치지 말 것.** 러너는 단계마다 새 프로세스로 코드를 다시 읽는다.
- **추적 해제 커밋을 pull/checkout 하면 로컬 파일이 지워진다** — 벤치 골드 파일은
  무시 패턴에 걸린 로컬 파일이니 브랜치 전환 전에 확인.

## 6. 산출물

- `benchmarks/query_judge.py` (러너) + 테스트(순수 함수: 목록 차이 추리기, X/Y 배정,
  집계). 산출물·판정 결과는 커밋하지 않는다.
- 결과 절: 연구 문서 `docs/studies/2026-09-23-distillate-vs-raw-log.md` §23,
  게이트 문서 `docs/plans/2026-09-23-answerless-memory-gate.md` §11 갱신.
- 채택이면: B 브랜치 PR — 라이브 인덱스 이주가 배포 효과임을 PR 본문에 적는다
  (Stop 훅 없는 MCP 전용 호스트는 기억이 사라진다 — 게이트 문서 §9.5-2).

## 7. 이 방식이 이후에도 기본이 된다

쌍 라벨(`*_displacement_labels.json`, `judge_displacements.py`)은 참고치로 남기고, 앞으로
검색 품질 변경의 판정은 **질문 → 결과 → 판정**으로 한다. 이번 러너가 그 첫 도구다.
주기 측정(`benchmarks/cycle.py`)에 넣을지는 이번 결과를 보고 따로 정한다.

## 관련

- 게이트 계획·결과: `docs/plans/2026-09-23-answerless-memory-gate.md` (§9 결과, §11 순서)
- 연구 문서: `docs/studies/2026-09-23-distillate-vs-raw-log.md` §20~§22
- B 코드: `feature/answerless-memory-gate` (청커 게이트 · 이주)
- 재정렬 실물 규칙: PR #19 · 답 소유 정본: PR #18
