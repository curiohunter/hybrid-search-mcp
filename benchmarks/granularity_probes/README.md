# Granularity probes (2026-09-08)

Four offline probes that decide one question: **why does the conversation lane
fail to surface facts that only it holds, and which layer can fix it?**

They change nothing in production and nothing in the index. Each takes a frozen
index snapshot (see `--config` on the benchmark runners) and a gold set. The
gold sets are not committed — they quote a private corpus; see the docstring of
`../run_conv_bench.py` for how to write your own.

| probe | question | answer measured on valuein |
|---|---|---|
| `a_reranker_ceiling.py` | Is the target even in the retrieved pool? Is negation special? | pool ceiling **4/10**; negation is *not* special |
| `b_sentence_index.py` | Does indexing sentences and returning parent turns find it? | parent top-3 **6/10**, top-10 **8/10** (from 0/10, 3/10) |
| `c_return_window.py` | Does returning neighbouring turns help? | **no** — 0 change on one set, +1 query at 4x context on the other |
| `d_sentence_lexical_max.py` | Can sentence-level *rescoring* of the existing pool substitute? | **no** — reranking cannot beat the pool |

`claim_split.py` and its tests live here rather than in `src/` for the same
reason: the structure-aware splitter lost to plain sentence splitting under
identical conditions (`p6a`), so nothing in the search path uses it and
shipping it in the wheel would hand users dead weight. It stays so the
comparison remains reproducible.

Read them in that order. `a` bounds what reranking can ever do, `b` is the only
intervention that moved the number, and `c`/`d` are the cheap alternatives that
were tried first and did not work.

Costs measured for `b` on one project: 84,550 unique sentences from 3,081
conversation turns, 46 minutes of local embedding, ~346 MB of float32 vectors
against a 224 MB existing index.
