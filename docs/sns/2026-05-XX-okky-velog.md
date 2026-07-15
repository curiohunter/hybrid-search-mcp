# 한국어 OSS 포스트 초안 — OKKY 공유게시판 + velog

**Status:** DRAFT — 오너 검토 대기 (2026-07-15 v0.7.2 수치 반영: EN holdout 스토리, 로컬 임베딩 정정)
**Target:** okky.kr/community/share + velog.io/@curiohunter (또는 본인 계정)
**Posting window:** 평일 09:00–11:00 KST (한국 개발자 출근 직후 RSS/북마크
체크 시간대). 금요일 오후·주말 회피.
**Owner action:** 한 번 읽고, 본인 목소리로 ≤ 2줄 수정, 게시.
**Spacing:** EN 채널 (HN / Reddit) 게시 후 최소 3일 뒤. EN 반응이 있으면
"해외 반응" 한 줄 인용 가능, 없어도 글이 독립적으로 성립.

---

## 제목 후보

1. **그래피파이도 카파시 wiki도 못 감당한 valuein 코드베이스, 그래서 직접 메모리 레이어를 만들었습니다 (오픈소스)**
2. **Claude Code랑 Codex가 같은 기억을 공유하게 만들었습니다 — 1인 개발자의 메모리 레이어**
3. **결제 API 마이그레이션을 두 에이전트로 시키고, 둘에게 직접 점수 매기게 했습니다 (9점 vs 6.5점, 둘 다 공개)**

*추천: #2.* OKKY/velog 한국 개발자가 즉시 이해하는 가치 (이중 에이전트 +
공유 기억)를 정직하게 표현. #1은 후킹은 강하지만 다른 도구를 디스하는
느낌이 들 수 있고, #3은 클릭베이트로 보일 위험.

---

## 본문 (≈ 700자, velog는 그대로 / OKKY는 인용블록 정도만 조정)

### 풀려던 문제

저는 `valuein_homepage` 라는 Next.js + Supabase 프로덕션 서비스를 혼자
운영합니다. 1,307 파일, 708 커밋, 도메인 언어는 한국어 (결제선생,
paymint, 학생 숙제 제출…), 프레임워크 코드는 영어가 섞여 있는 평범한
한국 SaaS 코드베이스입니다.

이 규모, 이 언어 구성에서 유명한 도구들이 각자 다른 지점에서 미끄러졌습니다.

- **Graphify**의 one-shot 지식 그래프는 커밋 사이에 stale 해졌고, 최근
  변경에 대한 질문에 조용히 잘못된 답을 줬습니다.
- **Karpathy 스타일 LLM wiki**는 `결제선생`, `paymint` 같은 한국어
  도메인 용어에 "그럴듯하지만 틀린" 페이지를 내놨습니다. LLM이 모를 때
  모른다고 말할 신호가 없었으니까요.
- **Cursor / Aider repo-map**은 세션 단위로는 동작했지만, 어제의
  추론이 오늘 사라졌고, Claude Code와 Codex가 서로의 히스토리를 전혀
  볼 수 없었습니다.

다 좋은 도구들입니다. 다만 다국어 + 능동적으로 편집되는 프로덕션
코드베이스를 두 에이전트로 굴리는 1인 메인테이너용은 아니었습니다.

### 그래서 만든 것

[hybrid-search-mcp](https://github.com/curiohunter/hybrid-search-mcp) —
의도적으로 작은 3개 조각:

1. **하이브리드 retriever** (BM25 + vector + call-graph god-nodes).
   라우터가 쿼리 형태에 따라 가중치를 고릅니다.
2. **Closed-loop 메모리 레이어**. 답변된 모든 쿼리는 markdown qa-log로
   기록되고, 이후 모든 쿼리는 그 로그를 1급 search chunk로 취급합니다.
   "저장하기" 버튼 없음, 대시보드 없음 — Stop hook이 자동으로 씁니다.
3. **Dual-agent hooks**. Claude Code와 Codex가 같은
   `.hybrid-search/qa/` 디렉토리를 읽고 씁니다. 어제 Claude가 한 답변이
   오늘 Codex 턴에서 Codex가 검색하기 전에 컨텍스트로 들어옵니다.

### 측정한 것 — 좋은 결과와 실패한 결과 둘 다

**좋은 결과**: 지난 주에 결제선생 V1 → V2 마이그레이션을 두 에이전트로
시키고 점수를 매기게 했습니다. 30+ 파일 편집, 9개 라우트, edge function
포함.

- Claude Code (planning agent): **9/10** — 1.3초에 영향 범위 매핑.
- Codex (executor agent): **6.5/10** — hybrid-search 30% / 공식 문서
  40% / grep+read 30% 기여로 작업 완료.

둘 다 원문 그대로 [케이스 스터디](https://github.com/curiohunter/hybrid-search-mcp/blob/main/docs/case-studies/2026-05-20-payssam-v2-migration.md)에
공개. 점수 차이 자체가 라우터가 제 역할을 한다는 증거입니다 — Codex가
자주 `confidence: weak`를 받고 grep으로 fallback한 것은 시스템의 설계
의도대로 동작한 것.

**실패한 결과**: Phase 4에서 v1 routing 마커가 first-pick 도구 선택
정확도를 올릴 거라 가설을 세웠고, 직접 측정했습니다. 4 baseline + 4
treatment 세션. 결과: **delta = 0**. 가설이 틀렸습니다.

이 null result를 묻지 않고 [그대로 공개](https://github.com/curiohunter/hybrid-search-mcp/blob/main/benchmarks/router_replay_2026-05.md)했습니다.
오픈소스 마케팅 글에서 가장 드물게 보이는 줄이 "내 가설이 틀렸다"라고
생각합니다.

### 정직한 비용

- 프롬프트당 **~400 ms pre-fetch 오버헤드** (vs `grep` ~50 ms).
- **embedder는 OpenAI `text-embedding-3-small` 전용** — 로컬 임베딩은
  시도 후 의도적으로 뺐습니다 (M3 맥북 팬이 인덱싱 내내 멈추지 않았음).
  API 키 필수, 2,000파일급 전체 인덱싱에 몇십 센트. `backend` 설정
  필드는 예약돼 있고 ONNX 기여 환영.
- Python 설치 + setup 명령 한 번 — 단일 바이너리 아님.

### 누구한테 맞나

Claude Code를 주력으로, 가끔 Codex도 쓰는 1인 개발자. 한국어 + 영어
혼합 프로덕션 코드베이스. 다른 시나리오라면:

- 팀 단위 공유 메모리 → 이거 아님 (로컬 디렉토리 기반).
- API 키 없이 가고 싶다 → Aider repo-map 추천.
- `grep`만 잘 돼도 충분 → 그러면 그냥 grep.

---

**Repo**: https://github.com/curiohunter/hybrid-search-mcp (MIT)
**Case study**: [docs/case-studies/2026-05-20-payssam-v2-migration.md](https://github.com/curiohunter/hybrid-search-mcp/blob/main/docs/case-studies/2026-05-20-payssam-v2-migration.md)
**Bench**: README § Compounding benchmark — `python benchmarks/run_compounding_bench.py`로 재현 가능
**Bench v2**: README § Memory bench v2 — 메모리 도구들이 잘 공개 안 하는 실패 모드 측정. 사실이 바뀐 뒤 새 답이 옛 답 위에 오는가(supersession): 한국어 dev set **6/6**. 그런데 영어 오픈소스 레포로 블라인드 홀드아웃을 돌렸더니 **1/6** — 매처가 한국어 토큰 통계에 과적합돼 있었습니다. 언어 일반형으로 다시 쓰고(영어 스테밍 + 식별자 가중 + complete-link 그룹핑), 새 레포(ripgrep)로 다시 블라인드 테스트: synthetic **6/6**(한국어 질문→영어 메모리 프로브 포함), CHANGELOG 기반 planted 케이스 **3/5**, adversarial **2/3** — 1회 실행, 실패까지 그대로 공개. 코퍼스에 없는 주제 false-strong은 3개 코드베이스 27개 프로브에서 **0**, 답변당 토큰 **~3k**. (처음 측정 50% → mtime 버그 수정 → 한국어 6/6 → 영어 1/6 → 언어 일반화 — 실패 점수가 곧 개발 로그입니다)

질문 / 피드백 환영합니다. 특히 한국어 도메인 코드베이스에서 비슷한
문제를 겪은 분의 사용기가 가장 가치 있습니다.

---

## 예상 댓글 + 답글 초안

### "Mem0 / Letta 와 뭐가 다른가요?"

> 사용 결이 다릅니다. Mem0/Letta는 에이전트 메모리 프레임워크 — 코드에
> `add` / `search` / `update`를 직접 호출합니다. 이건 반대로, hybrid_search
> 호출만 하면 hook이 알아서 저장하고, 다음 질문할 때 hook이 알아서
> 컨텍스트로 주입합니다. "저장하기" 호출이 코드에 없습니다.
> 또 다른 차이: Mem0/Letta는 Claude Code / Codex 양쪽 hook을 안 제공해서
> 듀얼 에이전트 공유 메모리를 직접 wiring해야 합니다. 이게 더 좁은 도구.

### "valuein은 어떤 서비스인가요?"

> 학생 숙제 제출 + AI 분석 + 결제까지 들어가는 한국어 SaaS입니다 (별도
> 도메인). 케이스 스터디는 그 코드베이스에서 실제 결제 API 마이그레이션을
> 한 기록입니다. 코드는 비공개지만 마이그레이션 과정 + 점수 + Codex의
> verbatim 평가는 공개해뒀습니다.

### "한국어 검색 품질은 어떤가요?"

> BM25 (tantivy-py)는 토크나이저 레벨에서 언어 무관, OpenAI
> `text-embedding-3-small`은 한국어 native 지원. 케이스 스터디의
> 1.3초 매핑이 한국어 + 영어 혼합 쿼리 (`"결제선생 PaysSam API 도메인
> sandbox stg paymint 환경변수"`) 결과입니다. Top-5 잡음 0개.

---

## Acceptance

- [ ] 오너가 한 번 읽고 ≤ 2줄 본인 목소리로 수정
- [ ] OKKY 공유게시판 또는 velog 둘 중 최소 하나 게시
- [ ] 게시 후 24시간 댓글 모니터링
- [ ] EN 게시물 (HN/Reddit) 보다 최소 3일 늦게 게시
