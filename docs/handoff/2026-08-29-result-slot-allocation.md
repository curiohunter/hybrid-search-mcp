# 결과 슬롯 배분 재설계

**작성** 2026-08-29 · **상태** 설계 대기 · **선행 커밋** `bdf9bbe`

---

## 한 줄 요약

`limit=10` 요청에 실제 코드·문서가 **2칸**만 들어간다. 보조 레인들이 각자
예산을 가지고 있는데 **아무도 합을 확인하지 않는다.**

---

## 증상

`valuein_homepage`에서 gold F5 — "변형 문제 variant problems 생성 로직" —
의 정답 문서가 top-10에 없다.

```
docs/plans/2026-04-15-variant-problems-plan.md   실제 순위 18
```

검색이 못 찾은 게 아니다. 그 문서 청크와 질문의 코사인은 **0.60 / 0.675 /
0.471**로 충분히 높다. **자리가 없어서 밀렸다.**

`limit=10` 실측 구성:

| 레인 | 예산 | 근거 위치 |
|---|---|---|
| 모듈 카드 + 멤버 | `limit // 2` = 5 | `_interleave_modules` |
| 메모리 (qa_log·card·commit 등) | `limit // 3` = 3 | `_merge_memory_results(head_limit=…)` + 일반 RRF |
| **보조 합계** | **8** | **비교하는 코드 없음** |
| 실제 청크 | **2** | |

`limit=40`에서 재보면 `module_member`가 11개다. 멤버가 특히 많이 샌다.

---

## 이것이 반복된 결함인 이유

2026-08-27에 같은 계열을 한 번 고쳤다 (커밋 `873f69e`):

> `_interleave_modules`는 "모듈이 슬롯의 절반을 넘지 않는다"를 문서화해
> 왔지만 강제하는 코드가 없었다. 카드는 `limit//2`, 멤버는 `limit//3`으로
> **각각 따로** 계산되고 서로를 비교하지 않았다.

그때는 **모듈 레인 안에서** 카드와 멤버가 따로 셈되는 문제였다. 지금은
**레인과 레인 사이에서** 같은 일이 벌어진다. 독립적으로 합리적인 예산들이
합쳐지면 주 레인을 압도한다.

**설계 원칙이 빠져 있다: 청크 레인의 최소 지분을 보장하는 곳이 없다.**

---

## 실패한 시도 세 가지 (반복하지 말 것)

전부 되돌렸다. 현재 `orchestrator.py`에는 아래 코드가 **없다.**

### 1. 응답 단계에서 자르기

`hybrid_search()` 끝, `_make_response()` 뒤에서 보조 행을 잘라냈다.

**실패:** 그 시점엔 `response.results`가 이미 `limit`으로 잘려 있다. 잘라낼
수는 있어도 **보충할 재료가 없다.** 결과가 10칸 → 6칸으로 줄었다.

### 2. 메모리 병합 직후 자르기

`_merge_memory_results()` 다음에 `chunk_results`를 잘랐다.

**실패:** 바로 뒤 `_interleave_modules()`가 **같은 풀에서 다시 채운다.**
잘라낸 만큼 그대로 되돌아왔다. 최종 qa 수가 변하지 않았다(4건 그대로).

### 3. `_interleave_modules` 채우기 루프에서 건너뛰기

fill 루프에서 보조 예산을 초과한 메모리 행을 skip하고 다음 청크로 넘어가게 했다.

**실패:** `deduped_chunks` 풀 자체가 메모리 행 위주여서, skip하면 채울 게
없어 6칸에서 끝났다. 그리고 카드 2 + 멤버 3 = 5로 예산이 이미 소진돼
**메모리가 통째로 사라졌다** — 사용자가 명시적으로 가치 있다고 한 맥락이
전부 날아갔다.

**교훈:** 이건 조립 파이프라인 여러 곳에 캡을 더하는 문제가 아니다.
**배분을 한 곳에서 결정해야 한다.**

---

## 설계 제약

### 반드시 지킬 것

1. **청크가 과반.** `limit` 요청 시 실제 검색된 청크가 최소 `ceil(limit/2)`.
2. **메모리는 0이 되면 안 된다.** 사용자 원문:

   > 코드를 물었을 때 그 얘기했던 기록도 같이 보는 이유는 그걸 왜 개발하게
   > 됐는지에 대한 맥락 이해 때문입니다.

   이 프로젝트의 존재 이유다. **자리를 줄이되 없애지 말 것.** 최소 1칸 보장.
3. **`memory_intent` 질의는 캡 대상이 아니다.** "지난번에 뭐라고 했지" 류에서는
   기록이 곧 답이다.
4. **결과 수를 줄이지 말 것.** 자르면 반드시 청크로 보충한다. 재료가 남아 있는
   단계에서 결정해야 가능하다.

### 우선순위 (실측 근거)

보조 예산 안에서의 순서는 **카드 → 메모리 → 멤버**를 제안한다.

- **카드**: ablation에서 검색 승리를 가져오는 건 카드였다. 모듈 레인을 끄면
  F2·F4를 못 찾는다 (19/25 → 17/25).
- **메모리**: 위 제약 2.
- **멤버**: `limit=40`에서 11개까지 새는 최대 오염원이고, ablation 기여는 S1
  1건뿐이었다.

---

## 참고 지점

| 대상 | 위치 |
|---|---|
| 슬롯 배치 로직 | `search/orchestrator.py::_interleave_modules` |
| 모듈 예산 | 같은 함수, `module_budget = max(1, limit // 2)` |
| 메모리 splice | `search/orchestrator.py::_merge_memory_results` (`head_limit`) |
| 메모리 노드 타입 | `_MEMORY_NODE_TYPES` (qa_log·memory_card·domain_term·episodic_example·commit) |
| 최종 조립 | `hybrid_search()` — interleave → graph card → recency head → `_make_response` → `_complete_supersession` |

**주의:** `_complete_supersession`이 `_make_response` **뒤에** 행을 덧붙인다.
최종 표시 집합은 그 뒤다.

---

## 검증 방법

```bash
# gold (현재 17/25)
HYBRID_SEARCH_TRANSLATION=0 .venv/bin/python benchmarks/confidence_eval.py \
  --cwd /Users/ian/project/claude_project/valuein_homepage \
  --gold benchmarks/valuein_gold.json --json

# 슬롯 구성 확인
# node_type별 개수를 세어 청크가 과반인지 볼 것
```

### 성공 기준

| 지표 | 현재 | 목표 |
|---|---|---|
| gold | 17/25 | **≥ 18/25** (F5 회복) |
| `limit=10` 청크 비율 | 2/10 | **≥ 5/10** |
| 메모리 행 | 3~4 | **1 이상, 과반 미만** |
| 모듈 순위 6쿼리 | 전부 1위 | **유지** |

무효 케이스는 점수에서 제외해 읽을 것: **R3**(타깃 문서 소실), **M1~M5**(이
저장소 개발 기록을 valuein에 묻는 케이스). 유효 19건 기준으로 보면 현재 16/19.

---

## 맥락: 직전 세션에서 끝난 것

이 문서를 낳은 작업의 결과 (전부 `main`에 반영됨):

- **임베딩 로컬 전환** — 맥미니 Ollama + `qwen3-embedding:0.6b`, Tailscale.
  요금 0원. 쿼리 18~38ms. gold 18/25 (Gemini 19/25와 사실상 동등)
- **모듈 이름 벡터** (`bdf9bbe`) — 이름과 내용을 따로 임베딩. 한국어 6쿼리
  기대 모듈이 전부 1위 (이전 15·25·14·9·1·1위).
  손으로 관리하던 56개 한↔영 별칭 맵은 **이제 불필요**하다 — 모델이 직접
  건넌다 (`문제은행` ↔ `problem-bank` 코사인 0.657). 정리는 미착수
- **훅 수정** (`ecae799`) — 워크트리 가드, `post-merge` 신설, 훅 버전 마커

### 이 문서와 별개로 남은 것

- **별칭 맵 은퇴** — `modules_search._ALIAS_MAP` 56개. 제거 후 gold로 검증
- **`generated_ratio` 경고 문구** — "index may need a rebuild"라고 하는데
  실제 원인은 슬롯 배분이다. 이 작업 후 문구 재검토
- **차원 불일치 로그** — `Failed to load vector index, starting fresh`가
  지문 불일치 상황에서 뜬다. 디스크는 안전하지만 오해를 부른다
- 나머지 6개 프로젝트 재빌드 (5,980청크, 0원)
