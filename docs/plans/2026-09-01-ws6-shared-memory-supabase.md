# WS6 — 기계 간 공유 메모리: 사용자 소유 Supabase 백엔드

**작성** 2026-09-01 · **상태** 라운드1 심사 대기 · **대체**
마스터 플랜 §8.5의 초기 스케치(맥미니 서비스안)를 이 문서가 대체한다.

---

## 0. 질문과 답

"남는 Supabase Pro 슬롯으로 중앙 인덱스를 하는 게 이득 아닌가?"
(2026-09-01, 사용자). 2-에이전트 조사(Supabase 기술 실사 + A/B/C 대안
비교)의 수렴 답:

**부분 채택 — 메모리 레인만 옮긴다 (C안).** 코드 검색을 통째로 옮기는
것(B안)과 맥미니 상시 서비스(A안 단독)는 기각한다.

| 안 | 판정 | 한 줄 근거 |
|---|---|---|
| A. 맥미니 검색 서비스 | 기각(단독) | 슬립/재연결/버전스큐/조용한 장애라는 운영 표면을 새로 만들며, 얻는 것은 C가 더 싸게 준다 |
| B. 전체 Supabase 포팅 | 기각 | 한국어 BM25가 성립 안 함(아래) + 검증된 랭킹 전체 재작성 + 코드 검색의 네트워크 의존화는 "70%는 grep이 빠름" 현실과 역행 |
| **C. 메모리 레인만** | **채택** | cross-machine 가치가 있는 데이터(git이 나르지 않는 메모리)만, 이미 결제 중인 자원으로 |

## 1. 왜 B(전체 포팅)는 안 되는가 — 조사 사실

- **한국어 BM25 부재**: pg_search(ParadeDB, 아이러니하게 Tantivy 기반)는
  Supabase allow-list에 없음(공식 논의상 계획 없음). 유일한 대안
  PGroonga는 한국어를 형태소 사전 없이 TokenBigram으로만 처리하고
  BM25 랭킹이 아니다. 한국어 실증 자료 전무 — 현행 Tantivy 레인 대비
  다운그레이드 위험 실재.
- **벡터 레인은 문제없음**: 25k×1024-dim은 Supabase 기준 초소형.
  halfvec+HNSW, Micro~Small 티어로 충분(공식 벤치: Small 50k 벡터
  평균 31ms). — 이 사실이 C안을 가능하게 한다.
- 코드 검색의 오프라인 불가·매질의 네트워크 의존은 로컬-퍼스트
  정체성과 정면 충돌. HN 정서 실사: 사용자 소유 DB로의 opt-in은
  수용되나, 하드코딩된 클라우드 의존은 dealbreaker.

## 2. 데이터 지도 — 무엇이 기계 로컬인가 (실측)

`.hybrid-search/`는 전체가 git-ignore다. 즉 git이 나르지 않는 메모리:

| 데이터 | 위치 | cross-machine 가치 |
|---|---|---|
| 대화 청크(.conversations, conv 인덱스) | 기계 로컬 | **최고** — "맥미니에서 어제 뭐 했지"가 맥북에서 안 나옴 |
| qa 로그 + 통합 노트(Reflector) | 기계 로컬 | 높음 — 같은 질문을 기계마다 다시 함 |
| 메모리 카드 / selfeval·harvested | 기계 로컬 | 중간 |
| 코드·문서·커밋·위키·콜그래프 | 레포(git)에서 파생 | 없음 — pull+델타 재인덱싱으로 이미 동기화(ecae799) |

규모 실측: 메모리 레인 청크는 전체의 11~17%(두 주력 프로젝트 합계
~2.9k 행), 벡터 포함 ~15MB. Micro 티어로도 남는 규모.

## 3. 아키텍처 (C안)

```
맥북 훅/세션                       맥미니 훅/세션
   │ 쓰기: 로컬 파일(현행 그대로)       │
   ▼                                  ▼
로컬 outbox (SQLite, 트랜잭션)    로컬 outbox
   │  백그라운드 릴레이(훅 아님)       │
   ▼                                  ▼
        Supabase (서울 ap-northeast-2, 사용자 소유)
        memory_chunks(project, machine_id, session_id,
          chunk_hash, kind, content, ts, provenance)
        + halfvec(1024) HNSW + pgroonga bigram
        + hybrid_memory_search() RPC (RRF — 공식 가이드 패턴)
   ▲
   │ 읽기: 메모리 레인 검색만 RPC 1콜 (PostgREST)
   │ 짧은 타임아웃 → 로컬 스냅샷 폴백
맥북/맥미니의 hybrid_search (코드 레인은 전부 로컬 그대로)
```

### 설계 규칙 (조사에서 도출, 위반 시 반려)

1. **훅은 원격에 직접 쓰지 않는다.** UserPromptSubmit은 블로킹이고
   타임아웃 시 additionalContext가 통째로 폐기된다(공식 문서 + 실사례
   조사). 훅은 지금처럼 로컬 파일에 쓰고, outbox 릴레이(비훅 경로:
   Stop 훅의 기존 detached spawn 패턴 재사용)가 배치 upsert한다.
2. **멱등 쓰기**: `(machine_id, session_id, chunk_hash)` 유니크 +
   `ON CONFLICT DO NOTHING`. 두 기계는 각자 자기 세션만 쓰는
   "파티션된 멀티라이터"라 행 경합이 없다 — 큐·조정자 불필요.
3. **읽기 폴백 필수**: 원격 타임아웃(예산 ~1s) 시 마지막 동기화된
   로컬 스냅샷으로 응답하고 freshness를 표기한다. 오프라인에서 잃는
   것은 "다른 기계의 최신 메모리"뿐, 나머지 전부 현행 유지.
4. **기본값은 로컬**: Supabase 연결은 opt-in 설정(config.toml
   `[shared_memory]`). 미설정 시 현행 동작 100% 유지. OSS 포지셔닝은
   "local-first, with optional bring-your-own-Postgres sync".
5. **새 MCP 도구 금지**(마스터 플랜 불변식 6) — 릴레이는 CLI 서브커맨드
   + 기존 훅 편승, 검색은 기존 도구의 내부 경로 교체.
6. **provenance 유지**(불변식 4) — 행마다 machine_id·session_id·원본
   경로. Reflector 통합 노트도 sources 그대로 동반.

### 융합 방식

메모리 레인 원격 결과는 현행 메모리 레인과 같은 자리에 들어간다 —
SlotPlanner의 memory 슬롯 계약(≥1, 과반 미만, memory_intent 해제)을
그대로 소비하며, 점수는 rank-bounded 스플라이스(비교불가 점수 병합
금지 — 기존 conv 레인 규약 재사용).

## 4. 단계

| 단계 | 내용 | 게이트 |
|---|---|---|
| P0 | 스키마 + outbox + 릴레이(쓰기만). 두 기계가 밀어넣기 시작 | 멱등성 테스트, 유실 0(재시도), 릴레이 실패가 훅/검색에 무영향 |
| P1 | 메모리 레인 읽기를 RPC로 전환(타임아웃+로컬 폴백) | **한국어 메모리 holdout**: 현행 로컬 메모리 레인 vs pgroonga+pgvector RRF 동일 질의 비교 — 하락 시 P1 보류하고 벡터 단독 융합으로 재시도. p50 레이턴시 실측 ≤ 현행+200ms |
| P2 | in-flight 대화 공유("지금 저 기계에서 뭐 하는 중") | 별도 설계 — verified handoff 쐐기의 확장 |

## 5. 비용·한도 (조사 실측)

- 한계비용: 조직 크레딧 소진 여부에 따라 $0~15/mo (Micro $10, Small
  $15). 데이터 ~15MB — 디스크 8GB·egress 250GB 한도와 무관한 규모.
- Spend cap 기본 on — 폭주 시 과금 대신 제한(read 폴백이 받쳐줌).

## 6. 하지 않을 것

- 코드·위키·콜그래프 레인의 원격화 (B안 기각 사유 그대로)
- `~/.hybrid-search` 파일 동기화/네트워크 공유 (손상 경로 — §8.5 유지)
- 훅 경로의 원격 직쓰기/직읽기 (규칙 1·3)
- Supabase 전용 기능 종속(Edge Functions, Realtime 등) — 표준
  Postgres+pgvector+pgroonga만 사용해 BYO-Postgres 호환 유지

## 7. 미해결 질문 → 라운드1에서 확정된 답

1. **정본은 로컬 파일이다.** Supabase는 읽기 확장용 사본. supersession은
   각 기계의 reindex가 로컬 계산한 결과를 `superseded_by` 컬럼 갱신
   이벤트로 릴레이한다 — **P0 범위에 포함**(라운드1 D3).
2. **임베딩 "이중화"는 프레이밍 오류였다.** 폴백은 별도 스냅샷이
   아니라 **현행 로컬 메모리 레인 그대로**다(원격 실패 = 오늘과 동일
   동작). Supabase 벡터는 순수 원격 검색용. 모델 지문
   (embedding_fingerprint)을 스키마에 포함해 모델 교체 시 공간 불일치를
   감지한다 — 로컬 인덱스가 이미 쓰는 규약 재사용.
3. **릴레이는 Stop 훅 편승 + launchd 주기(5분) 폴백.** 훅 비활성 기간
   분은 outbox에 쌓였다가 다음 실행에 일괄 전송.

---

## 8. 라운드1 외부 심사 반영 (DeepSeek, 2026-09-01)

판정: **수정 후 착수**. 처리표:

| # | 지적 | 처리 | 반영 |
|---|---|---|---|
| S1 | RLS/키 관리 누락 — anon key 노출 시 전체 메모리 유출 | **수용(최중요)** | P0 게이트에 추가: RLS 전행 잠금 + service role key만 사용, 키는 macOS 키체인 보관, anon key 경로 자체를 만들지 않음. 읽기 RPC는 service key 필수 |
| S2 | 삭제 전파 미정의 | **수용** | 소프트 삭제(`deleted_at`) + 로컬 qa-prune/integrity archive가 삭제 이벤트를 outbox로 전파. 하드 삭제는 주기 정리 작업 |
| S3 | supersession 전파를 P0에 | **수용** | §7-1에 반영 |
| S4 | 스키마 버전 관리 부재 | **수용** | `schema_version` 테이블 + 클라이언트가 버전 불일치 시 읽기 전용 폴백(구버전 클라이언트가 신스키마에 쓰지 않음) |
| S5 | outbox 보존 정책/무한 성장 | **수용** | 성공 행 즉시 삭제, 크기 캡(10MB) 초과 시 최고(最古) 행부터 드롭하며 경고 — 정본이 로컬이므로 드롭은 "다른 기계 공유 지연"일 뿐 유실이 아님 |
| S6 | pgroonga는 P1에서 제외, 벡터 단독 시작 | **수용** | 우리 메모리 레인의 KOREAN_NL은 이미 bm25_weight 0.15로 벡터 지배적. P1은 pgvector 단독 + 로컬 대비 holdout, BM25 필요성이 실측되면 pgroonga를 P1.5로 |
| S7 | P2(세션 공유)가 P0 스키마를 깨뜨림 | **수용(명확화)** | 멱등 키는 (machine_id, session_id, chunk_hash) 유지 — P2에서도 쓰기 주체는 기계이므로 키가 깨지지 않고, 공유는 읽기 시점 결합으로 처리. 스키마 노트에 명시 |
| S8 | 폴백 스냅샷의 병합 규칙 모호 | **수용(§7-2로 해소)** | 별도 스냅샷 저장소를 두지 않는다 — 폴백 = 현행 로컬 경로. 병합 문제 자체가 소멸 |
| S9 | "pgroonga 가용성 미확인" | **기각** | 조사 F1-1에서 공식 문서로 확인됨(supabase.com/docs/guides/database/extensions/pgroonga). 문서에 출처 누락이 원인 — 보강함 |
| S10 | "opt-in이면 기본 가치 0" | **기각** | 이 기능의 1차 고객은 2대 기계를 쓰는 사용자 본인이고, OSS 기본값이 로컬이어야 한다는 것이 설계 의도(조사: 하드코딩된 클라우드 의존만이 dealbreaker) |
| S11 | BYO-Postgres 호환 과잉 주장 | **부분 수용** | S6 반영으로 P1은 표준 pgvector만 사용 — 호환 주장이 오히려 강화됨. pgroonga 도입 시점에 재평가 조항 추가 |
| S12 | 현행 Tantivy의 한국어 처리 방식 미기재 | **수용(문서 보강)** | P1 holdout이 어차피 현행 대비 A/B라 게이트가 흡수하지만, 비교의 전제를 문서에 명시할 것 |
