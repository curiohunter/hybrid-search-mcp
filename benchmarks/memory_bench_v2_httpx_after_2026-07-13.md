# Memory bench v2 — httpx AFTER language-general supersession (burned dev set)

- Date: 2026-07-13, head `909b2b3` (feature/language-general-supersession)
- Scope: the httpx set was BURNED as a holdout when the 2026-07-13 run was
  used to diagnose the matcher; this is a dev/regression measurement, not
  holdout evidence. Cases are synthetic and hand-authored (n=6 / n=3).
- Before (main `ae26c94`): newer_first 1/6, adversarial exact_first 1/3.
- After (this PR): newer_first **6/6**, adversarial exact_first 2/3.
  ADV1's false grouping (fresh adjacent stealing the exact answer's slot)
  is gone. ADV3 is unchanged from BEFORE at identical ranks (exact@6,
  adjacent@5): a raw retrieval-score displacement, not a grouping error —
  supersession correctly refuses to reorder across topics, and this PR's
  scope excludes retrieval scoring.
- Axes: knowledge-update (6), adversarial recency (3), abstention (9 absent + 4 present), tokens, latency

## Knowledge-update (stale fact superseded by newer qa log)

| metric | value |
|---|---:|
| newer_found_rate (new qa in top-10) | 6/6 |
| newer_first_rate (new above old) | 6/6 |
| stale_only_rate (old surfaced, new missed — worst case) | 0/6 |

| id | topic | new rank | old rank | newer first |
|---|---|---:|---:|---|
| U1 | default request timeout | 2 | 5 | ✅ |
| U2 | connection pool limits | 2 | 5 | ✅ |
| U3 | transport retries | 4 | 5 | ✅ |
| U4 | HTTP/2 support | 2 | 5 | ✅ |
| U5 | outbound proxy | 2 | 4 | ✅ |
| U6 | TLS verification in staging | 2 | 4 | ✅ |

## Adversarial recency (old exact-topic vs fresh adjacent-topic)

Recency must never beat relevance across topics: the old answer that
exactly matches the probe has to stay above a fresher Q&A that merely
shares generic nouns.

exact_first: **2/3** — decomposed: exact found 3/3, both found 2/3, exact first given both 1/2, adjacent not retrieved 1/3

| id | exact (old) rank | adjacent (fresh) rank | exact first |
|---|---:|---:|---|
| ADV1 | 5 | — | ✅ |
| ADV2 | 2 | 4 | ✅ |
| ADV3 | 6 | 5 | ❌ |

## Abstention — full confidence distribution

An all-mixed classifier would score 0% on both headline error rates;
the matrix is what keeps the claim honest.

| probes | strong | mixed | weak |
|---|---:|---:|---:|
| verified-absent (n=9) | 0 | 0 | 9 |
| verified-present (n=4) | 0 | 4 | 0 |

| id | absent query | confidence |
|---|---|---|
| A1 | How does the Kafka consumer group handle rebalancing? | weak |
| A2 | Where is the GraphQL schema defined? | weak |
| A3 | Which gRPC services does the gateway expose? | weak |
| A4 | How are Celery background tasks scheduled? | weak |
| A5 | What Kubernetes resources does the deployment create? | weak |
| A6 | Where is the rate limiting middleware implemented? | weak |
| A7 | How does cursor-based pagination work in the list endpoints? | weak |
| A8 | Where are JWT tokens validated? | weak |
| A9 | Which SQLAlchemy models define the billing tables? | weak |

## Latency & cost

| metric | value |
|---|---:|
| end-to-end search latency p50 | 365 ms |
| end-to-end search latency p95 | 539 ms |
| expected embedding API calls (derived, whole run) | 44 (1 per search; compact+full = 2/case) |

## Tokens per answer (MCP wire payload, o200k_base)

| detail | mean | median |
|---|---:|---:|
| compact (default) | 2715 | 2576 |
| full | 5178 | 4203 |

compact/full ratio: **0.52** — progressive disclosure saving.
