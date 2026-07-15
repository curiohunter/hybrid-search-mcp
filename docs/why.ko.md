# 왜 만들었나

메인테이너가 무엇이 필요했고, 무엇이 작동하지 않았고, 무엇을 만들었으며,
그 비용이 무엇인지 — 사실만 짧게. 출생 이야기는 없다. 이 도구가 당신에게
맞는지 판단하는 데 필요한 부분만.

---

## 풀려던 문제

메인테이너는 `valuein_homepage`를 운영한다 — 유료 사용자가 쓰는 Next.js
+ Supabase 프로덕션 코드베이스. 1,307 파일, 708 커밋, 도메인 언어는
한국어, 프레임워크 코드는 영어가 섞여 있다.

이 규모, 이 언어 조합에서 유명한 기존 옵션들은 각자 다른 지점에서
미끄러졌다:

- **Graphify**의 one-shot 지식 그래프는 커밋 사이에 stale해졌고,
  최근 변경에 대한 질문에 조용히 잘못된 답을 주었다.
- **Karpathy 스타일 LLM wiki**는 `결제선생`, `paymint`, `학생 숙제
  제출` 같은 한국어 도메인 용어에 대해 "그럴듯하지만 틀린" 페이지를
  생성했다. LLM이 모를 때 모른다고 말할 신호가 없었기 때문.
- **Cursor / Aider repo-map**은 세션 단위로는 동작했지만, 어제의
  추론은 오늘 사라졌고, Claude Code와 Codex가 서로의 히스토리를 전혀
  볼 수 없었다.

이 도구들이 나쁘다는 게 아니다. 단지 다국어 + 능동적으로 편집되는
프로덕션 코드베이스에서 두 에이전트를 같이 쓰는 1인 메인테이너를
위해 설계되지 않았을 뿐이다.

## 만든 것 — 그리고 그 비용

의도적으로 작게, 세 조각:

1. **하이브리드 retriever** (BM25 + vector + call-graph god-nodes).
   라우터가 쿼리 형태에 따라 가중치를 고른다 — 단일 weight 아님.
2. **Closed-loop 메모리 레이어**: 답변된 모든 쿼리는 markdown qa-log로
   기록되고, 이후 모든 쿼리는 그 로그를 1급 search chunk로 취급한다.
   "저장하기" 명령 없음, 대시보드 없음.
3. **Dual-agent hooks**: Claude Code와 Codex가 같은
   `.hybrid-search/qa/` 디렉토리를 읽고 쓴다. 어제 Claude가 한 답변이
   오늘 Codex 턴에서 Codex가 검색하기 전에 surface된다.

정직한 비용:

- **프롬프트당 ~400 ms pre-fetch 오버헤드** (vs `grep` ~50 ms).
- **embedder는 OpenAI `text-embedding-3-small` 전용.** 로컬 임베딩은
  시도 후 의도적으로 뺐다 (전체 인덱싱 동안 M3 팬이 멈추지 않았음).
  `backend` 설정 필드는 예약돼 있고 ONNX 기여 환영.
- **Python 설치 + setup 명령 한 번** — 단일 바이너리 아님.

## 증거는 어디에 있나

- **프로덕션 케이스 스터디** — `docs/case-studies/2026-05-20-payssam-
  v2-migration.md`. 실제 결제 API 마이그레이션 후 두 에이전트가
  독립적으로 자가 채점: Claude Code 9/10, Codex 6.5/10. 둘 다 원문 그대로
  공개. 이 점수 차이 자체가 라우터가 제 역할을 한다는 증거다.
- **마케팅 가설의 반증** — `benchmarks/router_replay_2026-05.md`.
  v1 routing 마커가 first-pick 도구 선택 정확도를 실제로 올리는지
  측정했다. 안 올렸다 (delta = 0 vs baseline). 그 결과를 묻지 않고
  공개했다.
- **Compounding 벤치마크** — 20쌍 Cold→Warm 테스트, README의
  Compounding benchmark 섹션. Identity recall 80→95 %, paraphrase
  75→95 %, non-leaky 부분집합 73→100 %.
- **우리를 잡아낸 블라인드 홀드아웃** — README의 Language generality
  섹션. stale-fact supersession이 한국어 dev set에선 6/6이었는데,
  영어 오픈소스 레포 블라인드 테스트에서 1/6 — 매처가 한국어에
  과적합돼 있었다. v0.7.2가 그 언어 일반화 수정판: 한국어 6/6 유지,
  새 홀드아웃(ripgrep, 1회 실행, 그대로 공개) synthetic 6/6 ·
  CHANGELOG 기반 planted 3/5 · adversarial 2/3, 세 코드베이스
  27개 verified-absent 프로브에서 false-`strong` 0.

이 네 가지가 당신에게 필요한 것과 맞는다면, 설치는
[README](../README.md#quick-start)에 있다.

---

*Maintained by [@curiohunter](https://github.com/curiohunter) — karw79@gmail.com*
