# Memory bench v2 — httpx (English OSS HOLDOUT)

- Date: 2026-07-13
- Scope: HOLDOUT — external English-only OSS repo (encode/httpx @ b5addb64,
  2026-02-23, 892 chunks). Cases authored 2026-07-13 AFTER the algorithm was
  frozen (v0.7.1 / PR #1); single run, published as-is, no tuning against
  results. Update/adversarial cases are synthetic and hand-authored
  (n=6 / n=3) — treat rates as case counts, not population estimates.
- Development-set counterpart: memory_bench_v2_2026-07-12.md (valuein, Korean).
- Axes: knowledge-update (6), adversarial recency (3), abstention (9 absent + 4 present), tokens, latency

## Knowledge-update (stale fact superseded by newer qa log)

| metric | value |
|---|---:|
| newer_found_rate (new qa in top-10) | 6/6 |
| newer_first_rate (new above old) | 1/6 |
| stale_only_rate (old surfaced, new missed — worst case) | 0/6 |

| id | topic | new rank | old rank | newer first |
|---|---|---:|---:|---|
| U1 | default request timeout | 5 | 2 | ❌ |
| U2 | connection pool limits | 2 | 4 | ✅ |
| U3 | transport retries | 5 | 4 | ❌ |
| U4 | HTTP/2 support | 5 | 2 | ❌ |
| U5 | outbound proxy | 4 | 2 | ❌ |
| U6 | TLS verification in staging | 4 | 2 | ❌ |

## Adversarial recency (old exact-topic vs fresh adjacent-topic)

Recency must never beat relevance across topics: the old answer that
exactly matches the probe has to stay above a fresher Q&A that merely
shares generic nouns.

exact_first: **1/3** — decomposed: exact found 3/3, both found 3/3, exact first given both 1/3, adjacent not retrieved 0/3

| id | exact (old) rank | adjacent (fresh) rank | exact first |
|---|---:|---:|---|
| ADV1 | 5 | 2 | ❌ |
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
| end-to-end search latency p50 | 348 ms |
| end-to-end search latency p95 | 412 ms |
| expected embedding API calls (derived, whole run) | 44 (1 per search; compact+full = 2/case) |

## Tokens per answer (MCP wire payload, o200k_base)

| detail | mean | median |
|---|---:|---:|
| compact (default) | 2730 | 2576 |
| full | 5191 | 4246 |

compact/full ratio: **0.53** — progressive disclosure saving.

## Holdout diagnosis (read-only; no code was changed for this run)

Headline vs the Korean development set (valuein, 2026-07-12):

| axis | valuein (KO, dev) | httpx (EN, holdout) |
|---|---:|---:|
| knowledge-update newer_first | 6/6 | **1/6** |
| adversarial exact_first | 3/3 | **1/3** |
| abstention false-strong on absent | 0/9 | 0/9 |
| abstention weak on present | 0/4 | 0/4 |
| tokens compact/full ratio | 0.74 | 0.53 |

**What generalized:** the confidence contract. 9/9 verified-absent queries →
weak, 4/4 present controls → mixed, zero false-strong. The corpus-absent cap
is language-independent. Retrieval itself also held: new qa found in top-10
6/6, stale_only 0/6, both adversarial docs retrieved 3/3.

**What did not: topic-aware supersession is calibrated on Korean token
statistics.** Verified offline by running `_same_qa_topic` on the planted
pairs:

- Update pairs (should group): only U2 passes the query-overlap gate
  (q_ov 0.60); U1/U3–U6 sit at 0.20–0.33 vs the 0.40 threshold. The one
  pair that grouped is exactly the one pair that scored newer-first — the
  1/6 is fully explained by grouping, not by retrieval.
  Root cause: `_normalized_tokens` collapses Hangul tokens to a 2-char
  prefix, which acts as a stemmer for Korean ("배치는"/"배치가" → "배치");
  English gets no stemming, so "retries"/"retry", "raised"/"raise" never
  match and same-topic English questions under-group.
- Adversarial pairs (should NOT group): ADV1 wrongly groups (q_ov 0.40,
  a_ov 0.25) — English adjacent-topic answers share generic tokens
  ("unit", "test", "fixture") far above the ≤0.09 the thresholds were
  measured against on Korean answers. The newest-of-group head selection
  then hands the guaranteed slot to the fresh adjacent qa (adjacent@2,
  exact@5) — the exact failure mode the conservative thresholds were
  designed to prevent. ADV3's miss is unrelated to grouping (not grouped;
  displaced by one rank on raw score).

**Follow-up (do NOT tune on this set):** add English stemming to
`_normalized_tokens`, then recalibrate the overlap thresholds on a
bilingual gold set (already a PR #1 follow-up), and re-verify on a FRESH
holdout — this httpx set is now burned as a development signal for the
supersession thresholds.
