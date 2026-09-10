"""Calibrate the topic-matcher thresholds against BOTH signals.

The gold set alone cannot calibrate them. It is 88 synthetic pairs, and
2026-09-09 showed twice that it stays green while corpus behavior moves
underneath it: first when 209 wrong supersession mappings disappeared
without shifting a single gold pair, then when the morphological backend
passed every slice while pairwise acceptance over the real corpus rose
10.6 -> 14.5 per 10k on junk pairs.

So the gold set is the RECALL FLOOR (a hard constraint: zero false groups
on adjacent/bridge, per-language floors on same) and the corpus is the
PRECISION SIGNAL (minimize accepted pairs, since the overwhelming
majority of random qa pairs are genuinely unrelated). Both are cheap to
sweep because the per-pair overlaps are computed once and cached; only
the comparisons against thresholds are repeated.

    python benchmarks/topic_threshold_sweep.py --config <cfg> --project <name>
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.memory import supersession as ss  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search import qa_topics as topics  # noqa: E402
from hybrid_search.storage.db import StoreDB  # noqa: E402
from hybrid_search.storage.indexes import IndexPaths, get_project_dir  # noqa: E402

GOLD = Path(__file__).parent / "topic_gold_set.json"
SAME_FLOOR = {"ko": 0.90, "en": 0.85, "mixed": 0.85}


def _features(a, b) -> tuple[float, float, int, int, bool]:
    """Everything a threshold decision needs, computed once per pair."""
    union_a = {**a[1], **a[0]}
    union_b = {**b[1], **b[0]}
    return (
        topics.weighted_overlap(a[0], b[0]),
        topics.weighted_overlap(a[1], b[1]),
        topics._distinctive_shared_count(union_a, union_b),
        topics._distinctive_shared_count(a[0], b[0]),
        bool(a[1] and b[1]),
    )


def _lenient(f, q_thr, a_thr, ao_thr) -> bool:
    """qa_topics.same_topic, expressed over cached features."""
    q_ov, a_ov, dist_union, _, both_answers = f
    if dist_union < topics._MIN_DISTINCTIVE_SHARED:
        return False
    if both_answers:
        if q_ov >= q_thr and a_ov >= a_thr:
            return True
        return a_ov >= ao_thr
    return q_ov >= topics._QUERY_ONLY_OVERLAP


def _strict(f, q_thr, a_thr, ao_thr) -> bool:
    """supersession._same_topic_strict over cached features."""
    if not _lenient(f, q_thr, a_thr, ao_thr):
        return False
    return f[3] >= topics._MIN_DISTINCTIVE_SHARED and f[0] >= q_thr


def _gold_features():
    pairs = json.loads(GOLD.read_text(encoding="utf-8"))["pairs"]
    def tok(side):
        return (topics.topic_tokens(side["query"]), topics.topic_tokens(side["answer"]))

    out = []
    for p in pairs:
        if p.get("known_limitation"):
            continue

        if p["relation"] == "bridge":
            items = [tok(i) for i in p["items"]]
            out.append(("bridge", p["lang"], p["id"],
                        [_features(items[0], items[1]),
                         _features(items[1], items[2]),
                         _features(items[0], items[2])]))
        else:
            out.append((p["relation"], p["lang"], p["id"],
                        [_features(tok(p["a"]), tok(p["b"]))]))
    return out


def _gold_verdict(gold, q, a, ao):
    """(passes_gate, same_pass, same_total, false_groups)."""
    same = {lang: [0, 0] for lang in SAME_FLOOR}
    false_groups = 0
    for rel, lang, _pid, feats in gold:
        if rel == "bridge":
            # endpoints must not be co-grouped; complete-link means the
            # endpoint pair itself decides.
            if _lenient(feats[2], q, a, ao):
                false_groups += 1
        elif rel == "adjacent":
            if _lenient(feats[0], q, a, ao):
                false_groups += 1
        else:
            same[lang][1] += 1
            same[lang][0] += _lenient(feats[0], q, a, ao)
    ok = false_groups == 0 and all(
        c[0] >= SAME_FLOOR[lang] * c[1] for lang, c in same.items() if c[1]
    )
    return ok, sum(c[0] for c in same.values()), sum(c[1] for c in same.values()), false_groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--pairs", type=int, default=100000)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    pinfo = reg.get_by_name(args.project)
    if pinfo is None:
        print(f"{args.project}: not registered")
        return 1
    db = StoreDB(IndexPaths(get_project_dir(cfg.projects_dir, pinfo.id)).store_db)
    chunks = [c for c in db.get_chunks_by_node_type(pinfo.id, "qa_log")
              if not ss._is_machine_payload(c.content or "")]
    db.close()
    items = [ss._topic_item(c.content or "") for c in chunks]

    rng = random.Random(20260909)
    n = len(items)
    corpus = []
    for _ in range(args.pairs):
        i, j = rng.randrange(n), rng.randrange(n)
        if i != j:
            corpus.append(_features(items[i], items[j]))
    gold = _gold_features()

    print(f"backend {topics.topic_backend()} · corpus n={n} · sampled {len(corpus)} pairs")
    print(f"current point: q={topics._QUERY_OVERLAP} a={topics._ANSWER_OVERLAP} "
          f"ao={topics._ANSWER_ONLY_OVERLAP}")
    rows = []
    for q in [round(0.28 + 0.02 * i, 2) for i in range(12)]:
        for a in [round(0.18 + 0.02 * i, 2) for i in range(10)]:
            for ao in [round(0.28 + 0.03 * i, 2) for i in range(12)]:
                ok, sp, st, fg = _gold_verdict(gold, q, a, ao)
                if not ok:
                    continue
                rate = sum(_strict(f, q, a, ao) for f in corpus) / len(corpus) * 1e4
                rows.append((rate, -sp, q, a, ao, sp, st))
    if not rows:
        print("no threshold point passes the gold gate")
        return 1
    rows.sort()
    print(f"\n{'corpus/10k':>10} {'gold same':>10}  {'q':>5} {'a':>5} {'ao':>5}"
          "   (gate PASS only, sorted by corpus acceptance)")
    for rate, _, q, a, ao, sp, st in rows[:args.top]:
        print(f"{rate:10.1f} {sp:6d}/{st:<4}  {q:5.2f} {a:5.2f} {ao:5.2f}")
    best = rows[0]
    print(f"\nlowest corpus acceptance while passing the gate: "
          f"q={best[2]} a={best[3]} ao={best[4]}  "
          f"({best[0]:.1f}/10k, same {best[5]}/{best[6]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
