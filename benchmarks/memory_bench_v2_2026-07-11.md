# Memory bench v2 — valuein_homepage

- Date: 2026-07-11
- Scope: ONE production codebase; update/adversarial cases are synthetic
  and hand-authored (n=6 / n=3) — treat rates as case counts,
  not population estimates.
- Axes: knowledge-update (6), adversarial recency (3), abstention (9 absent + 4 present), tokens, latency

## Knowledge-update (stale fact superseded by newer qa log)

| metric | value |
|---|---:|
| newer_found_rate (new qa in top-10) | 6/6 |
| newer_first_rate (new above old) | 6/6 |
| stale_only_rate (old surfaced, new missed — worst case) | 0/6 |

| id | topic | new rank | old rank | newer first |
|---|---|---:|---:|---|
| U1 | 정산 배치 시각 | 4 | 6 | ✅ |
| U2 | 포털 세션 만료 | 6 | — | ✅ |
| U3 | 결제 PG사 | 2 | 4 | ✅ |
| U4 | 출결 알림 채널 | 6 | — | ✅ |
| U5 | 테스트 러너 | 2 | — | ✅ |
| U6 | 숙제 제출 저장소 | 2 | 6 | ✅ |

## Adversarial recency (old exact-topic vs fresh adjacent-topic)

Recency must never beat relevance across topics: the old answer that
exactly matches the probe has to stay above a fresher Q&A that merely
shares generic nouns.

exact_first: **3/3**

| id | exact (old) rank | adjacent (fresh) rank | exact first |
|---|---:|---:|---|
| ADV1 | 2 | 6 | ✅ |
| ADV2 | 4 | — | ✅ |
| ADV3 | 6 | — | ✅ |

## Abstention — full confidence distribution

An all-mixed classifier would score 0% on both headline error rates;
the matrix is what keeps the claim honest.

| probes | strong | mixed | weak |
|---|---:|---:|---:|
| verified-absent (n=9) | 0 | 1 | 8 |
| verified-present (n=4) | 0 | 4 | 0 |

| id | absent query | confidence |
|---|---|---|
| A1 | 우리 GraphQL 스키마는 어떻게 구성돼 있어? | weak |
| A2 | Kafka 컨슈머 그룹 설정 알려줘 | weak |
| A3 | Elasticsearch 인덱스 매핑이 어떻게 되지? | weak |
| A4 | Firebase 푸시 토큰 관리 로직 설명해줘 | weak |
| A5 | gRPC 서비스 정의 파일 어디 있어? | weak |
| A6 | 배송 추적 기능은 어떻게 구현돼 있나? | weak |
| A7 | 쿠폰 발급과 사용 처리 흐름 정리해줘 | weak |
| A8 | 포인트 적립 정책이 어떻게 되지? | weak |
| A9 | 구독 결제 갱신 로직 설명해줘 | mixed |

## Latency & cost

| metric | value |
|---|---:|
| search latency p50 | 539 ms |
| search latency p95 | 659 ms |
| embedding API calls (whole run) | 44 (1 per search; compact+full = 2/case) |

## Tokens per answer (MCP wire payload, o200k_base)

| detail | mean | median |
|---|---:|---:|
| compact (default) | 3526 | 3577 |
| full | 4628 | 4543 |

compact/full ratio: **0.76** — progressive disclosure saving.
