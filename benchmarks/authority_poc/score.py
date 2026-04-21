"""Score labeled PoC results — NDCG@10 overall + per-query + per-type.

Input:
    label_me.tsv    with the `relevance` column filled (0/1/2) for every row.

Output (stdout):
    Per-query NDCG@10 for OFF / ON + delta.
    Per-type average (structural / keyword / semantic).
    Overall mean.

Note: n=10 queries is DIRECTIONAL only. Variance is large; ±0.05 has no
statistical meaning. Full L6 (30+ queries) re-verifies. Per-type delta
is the more interesting signal — look for "authority helps type X, hurts Y"
patterns to justify type-gated authority in the next iteration.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
LABEL_TSV_PATH = HERE / "label_me.tsv"


def dcg(relevances: list[int]) -> float:
    """DCG with the 2^rel - 1 gain formulation (standard for graded relevance)."""
    return sum(
        (2 ** rel - 1) / math.log2(i + 2)
        for i, rel in enumerate(relevances)
    )


def ndcg_at_k(relevances: list[int], k: int = 10) -> float:
    cut = relevances[:k]
    ideal = sorted(relevances, reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    if ideal_dcg == 0:
        return 0.0
    return dcg(cut) / ideal_dcg


def _load_labels():
    """Return dict[(id, mode)] → list of relevance ints ordered by rank."""
    rows = []
    with open(LABEL_TSV_PATH, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rows.append(row)

    buckets: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    types: dict[str, str] = {}
    queries: dict[str, str] = {}

    unlabeled = 0
    for row in rows:
        rel_raw = row.get("relevance", "").strip()
        if rel_raw == "":
            unlabeled += 1
            rel = 0
        else:
            try:
                rel = int(rel_raw)
            except ValueError:
                raise SystemExit(f"Non-integer relevance: {row!r}")
            if rel not in (0, 1, 2):
                raise SystemExit(f"Relevance must be 0/1/2, got {rel}: {row!r}")
        key = (row["id"], row["mode"])
        buckets[key].append((int(row["rank"]), rel))
        types[row["id"]] = row["type"]
        queries[row["id"]] = row["query"]

    # Sort each bucket by rank
    ordered = {
        key: [rel for _, rel in sorted(items, key=lambda x: x[0])]
        for key, items in buckets.items()
    }
    return ordered, types, queries, unlabeled


def main() -> None:
    if not LABEL_TSV_PATH.exists():
        raise SystemExit(f"{LABEL_TSV_PATH} not found. Run run.py first.")

    ordered, types, queries, unlabeled = _load_labels()
    if unlabeled:
        print(f"⚠ {unlabeled} unlabeled rows — treated as relevance=0.\n")

    query_ids = sorted(types.keys())

    print(f"{'ID':4s}  {'TYPE':10s}  {'OFF':>6s}  {'ON':>6s}  {'Δ':>7s}   QUERY")
    print("-" * 90)

    per_type_delta: dict[str, list[float]] = defaultdict(list)
    per_type_off: dict[str, list[float]] = defaultdict(list)
    per_type_on: dict[str, list[float]] = defaultdict(list)
    all_deltas: list[float] = []

    for qid in query_ids:
        off_rels = ordered.get((qid, "OFF"), [])
        on_rels = ordered.get((qid, "ON"), [])
        if not off_rels or not on_rels:
            print(f"{qid}  missing OFF or ON rows")
            continue
        off_ndcg = ndcg_at_k(off_rels)
        on_ndcg = ndcg_at_k(on_rels)
        delta = on_ndcg - off_ndcg
        qtype = types[qid]
        per_type_delta[qtype].append(delta)
        per_type_off[qtype].append(off_ndcg)
        per_type_on[qtype].append(on_ndcg)
        all_deltas.append(delta)
        arrow = "↑" if delta > 0.005 else ("↓" if delta < -0.005 else "·")
        print(
            f"{qid:4s}  {qtype:10s}  {off_ndcg:6.3f}  {on_ndcg:6.3f}  {arrow}{delta:+7.3f}  "
            f"{queries[qid][:50]}"
        )

    print()
    print("=" * 90)
    print(f"{'TYPE':10s}  {'N':>3s}  {'OFF':>6s}  {'ON':>6s}  {'Δ':>7s}")
    print("-" * 45)
    for qtype in ("structural", "keyword", "semantic"):
        if qtype not in per_type_delta:
            continue
        n = len(per_type_delta[qtype])
        off_avg = sum(per_type_off[qtype]) / n
        on_avg = sum(per_type_on[qtype]) / n
        d_avg = sum(per_type_delta[qtype]) / n
        print(f"{qtype:10s}  {n:>3d}  {off_avg:6.3f}  {on_avg:6.3f}  {d_avg:+7.3f}")

    if all_deltas:
        n = len(all_deltas)
        mean_d = sum(all_deltas) / n
        # Variance for sanity — no claim of significance at n=10.
        var = sum((d - mean_d) ** 2 for d in all_deltas) / n
        sd = math.sqrt(var)
        print("-" * 45)
        print(f"{'OVERALL':10s}  {n:>3d}  {'':>6s}  {'':>6s}  {mean_d:+7.3f}  (sd={sd:.3f})")

    print()
    print("Reminder: n=10 is DIRECTIONAL only. Full L6 (30+ queries) re-verifies.")
    print("Key signal: per-type delta spread — look for 'helps type X, hurts Y'.")


if __name__ == "__main__":
    main()
