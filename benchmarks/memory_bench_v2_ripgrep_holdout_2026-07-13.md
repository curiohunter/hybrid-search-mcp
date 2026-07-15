# Memory bench v2 — ripgrep (FRESH HOLDOUT, single run, published as-is)

- Date: 2026-07-13, head `ab8505e` (language-general supersession, frozen
  before case authoring; no code or threshold changes after this run)
- Corpus: BurntSushi/ripgrep @ `d5b85d44` — different org and domain than
  the burned httpx dev set. 500 commits + CHANGELOG indexed.
- Cases: U1–U6 synthetic team-decisions (probes U5/U6 in KOREAN → EN
  corpus), R1–R5 planted pairs whose facts come from REAL changes verified in CHANGELOG/git history
  (deprecated flag swaps, hyperlink introduction, MSRV bump, empty -vf
  semantics). Treat rates as case counts, not population estimates.

## Holdout verdict

- Synthetic update: **6/6 newer-first**, including both KO→EN probes —
  the language-general matcher generalizes to an unseen English corpus
  and to cross-language probes.
- CHANGELOG-derived planted update cases: **3/5** (facts taken from real
  CHANGELOG changes, but the old/new Q&A pairs are planted synthetics).
  R1 is the one true stale-fact failure
  (stale_only): the probe phrasing matches the OBSOLETE answer verbatim
  (old qa at #1) while the correction gets crowded out of top-10 by real
  corpus hits — a qa-lane exposure gap, not a grouping error. R5 is a
  benign double-miss: both planted qa lose to the real CHANGELOG chunk,
  which itself states the current behavior.
- Adversarial: 2/3. ADV3 (Korean probe) never retrieved the exact
  English qa at all (cross-language retrieval gap). No false grouping
  was observed, but both_found was 0/3 — this slice never actually
  exercised the grouping decision, so it is evidence of absence of
  grouping errors only in the weak sense.
- Abstention: absent 9/9 weak incl. 2 Korean probes, present 4/4 mixed
  incl. 1 Korean — zero false-strong, fully language-general.
- Clean-tree reproduction (2026-07-14, after the vector.py fix landed
  as `fe0f9a2` so the head SHA fully describes the executed code):
  `memory_bench_v2_ripgrep_cleanrepro_2026-07-14.*` — every update and
  adversarial row identical down to individual ranks; absent 9/9 weak
  identical; one borderline flip (present P4, the Korean probe,
  mixed → weak on embedding nondeterminism).
- Follow-ups (NOT fixed in this cycle, by holdout rule): qa-lane
  exposure under corpus crowding (R1), cross-language qa retrieval for
  KO probes over EN memories (ADV3).
- Axes: knowledge-update (11), adversarial recency (3), abstention (9 absent + 4 present), tokens, latency

## Knowledge-update (stale fact superseded by newer qa log)

| metric | value |
|---|---:|
| newer_found_rate (new qa in top-10) | 9/11 |
| newer_first_rate (new above old) | 9/11 |
| stale_only_rate (old surfaced, new missed — worst case) | 1/11 |

| id | topic | new rank | old rank | newer first |
|---|---|---:|---:|---|
| U1 | max-columns default in team config | 4 | 5 | ✅ |
| U2 | case sensitivity alias | 2 | 4 | ✅ |
| U3 | ripgrep config file location | 2 | 5 | ✅ |
| U4 | custom type filters in search script | 2 | 5 | ✅ |
| U5 | thread cap | 6 | — | ✅ |
| U6 | binary file handling | 4 | 5 | ✅ |
| R1 | REAL: auto PCRE2 fallback flag (CHANGELOG 12.0.0) | — | 1 | ❌ |
| R2 | REAL: pcre2 unicode flag rename (CHANGELOG 12.0.0) | 4 | 5 | ✅ |
| R3 | REAL: terminal hyperlinks introduced (CHANGELOG 14.0.0) | 4 | 5 | ✅ |
| R4 | REAL: minimum supported Rust version bump (CHANGELOG 11.0.0) | 2 | 5 | ✅ |
| R5 | REAL: empty -vf pattern file semantics (CHANGELOG 15.0.0) | — | — | ❌ |

## Adversarial recency (old exact-topic vs fresh adjacent-topic)

Recency must never beat relevance across topics: the old answer that
exactly matches the probe has to stay above a fresher Q&A that merely
shares generic nouns.

exact_first: **2/3** — decomposed: exact found 2/3, both found 0/3, exact first given both N/A (both found 0/3 — grouping competition not exercised), adjacent not retrieved 2/3

| id | exact (old) rank | adjacent (fresh) rank | exact first |
|---|---:|---:|---|
| ADV1 | 5 | — | ✅ |
| ADV2 | 4 | — | ✅ |
| ADV3 | — | 2 | ❌ |

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
| A2 | Where is the OAuth token refresh implemented? | weak |
| A3 | Where are JWT tokens validated? | weak |
| A4 | What Kubernetes resources does the deployment create? | weak |
| A5 | How does the websocket reconnect logic work? | weak |
| A6 | How does cursor-based pagination work in the list API? | weak |
| A7 | How are webhook signatures verified? | weak |
| A8 | 결제 PG 이중화 구성이 어떻게 되어 있지? | weak |
| A9 | 커넥션 풀 제한 설정 알려줘 | weak |

## Latency & cost

| metric | value |
|---|---:|
| end-to-end search latency p50 | 346 ms |
| end-to-end search latency p95 | 436 ms |
| expected embedding API calls (derived, whole run) | 54 (1 per search; compact+full = 2/case) |

## Tokens per answer (MCP wire payload, o200k_base)

| detail | mean | median |
|---|---:|---:|
| compact (default) | 2778 | 3044 |
| full | 4461 | 4383 |

compact/full ratio: **0.62** — progressive disclosure saving.
