# Memory bench v2 — valuein_homepage

- Date: 2026-07-11
- Axes: knowledge-update (6 cases), abstention (9 absent + 4 present), tokens-per-answer

## Knowledge-update (stale fact superseded by newer qa log)

| metric | value |
|---|---:|
| newer_found_rate (new qa in top-10) | 100.00% |
| newer_first_rate (new above old) | 100.00% |
| stale_only_rate (old surfaced, new missed — worst case) | 0.00% |

| id | topic | new rank | old rank | newer first |
|---|---|---:|---:|---|
| U1 | 정산 배치 시각 | 4 | 6 | ✅ |
| U2 | 포털 세션 만료 | 6 | — | ✅ |
| U3 | 결제 PG사 | 2 | 4 | ✅ |
| U4 | 출결 알림 채널 | 2 | 6 | ✅ |
| U5 | 테스트 러너 | 2 | — | ✅ |
| U6 | 숙제 제출 저장소 | 2 | 6 | ✅ |

## Abstention (confidence contract on absent topics)

| metric | value | target |
|---|---:|---|
| weak_on_absent_rate | 77.78% | high (correct refusal) |
| strong_on_absent_rate | 11.11% | 0% (false confidence) |
| weak_on_present_rate | 0.00% | low (not just pessimistic) |

| id | absent query | confidence |
|---|---|---|
| A1 | 우리 GraphQL 스키마는 어떻게 구성돼 있어? | weak |
| A2 | Kafka 컨슈머 그룹 설정 알려줘 | weak |
| A3 | Elasticsearch 인덱스 매핑이 어떻게 되지? | weak |
| A4 | Firebase 푸시 토큰 관리 로직 설명해줘 | weak |
| A5 | gRPC 서비스 정의 파일 어디 있어? | weak |
| A6 | 배송 추적 기능은 어떻게 구현돼 있나? | weak |
| A7 | 쿠폰 발급과 사용 처리 흐름 정리해줘 | strong |
| A8 | 포인트 적립 정책이 어떻게 되지? | weak |
| A9 | 구독 결제 갱신 로직 설명해줘 | mixed |

## Tokens per answer (MCP wire payload, o200k_base)

| detail | mean | median |
|---|---:|---:|
| compact (default) | 3447 | 3365 |
| full | 4565 | 4471 |

compact/full ratio: **0.76** — progressive disclosure saving.
