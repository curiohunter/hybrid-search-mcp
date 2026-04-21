"""Full L6 scoring — per-query NDCG@10, α comparison, bootstrap 95% CI.

Reads results_v2.json (produced by run_v2.py) and emits:
  1. Per-query NDCG@10 for each α with OFF baseline.
  2. Per-type mean Δ(α) + 95% bootstrap CI.
  3. "Δ > 0 probability" over bootstrap samples — answers "how confident
     are we that authority helps on average?".
  4. Per-project breakdown (self vs external).

Sample sizes (self=30, external=5) are noted in each section. n=30 starts
to narrow the CI but is still small — treat trends with caution.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
RESULTS_PATH = HERE / "results_v2.json"

ALPHAS = [0.2, 0.3, 0.5]
BOOTSTRAP_N = 1000
RNG_SEED = 20260421


def dcg(relevances: list[int]) -> float:
    return sum(
        (2 ** rel - 1) / math.log2(i + 2)
        for i, rel in enumerate(relevances)
    )


def ndcg_at_k(relevances: list[int], k: int = 10) -> float:
    cut = relevances[:k]
    ideal = sorted(relevances, reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    return dcg(cut) / ideal_dcg if ideal_dcg else 0.0


def bootstrap_ci(samples: list[float], n_boot: int = BOOTSTRAP_N, seed: int = RNG_SEED):
    """Percentile bootstrap 95% CI + P(mean > 0)."""
    if not samples:
        return (0.0, 0.0, 0.0, 0.0)
    rng = random.Random(seed)
    means = []
    n = len(samples)
    for _ in range(n_boot):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[int(0.975 * n_boot)]
    p_positive = sum(1 for m in means if m > 0) / n_boot
    observed_mean = sum(samples) / len(samples)
    return observed_mean, lo, hi, p_positive


def _group_ndcg(rows: list[dict]):
    """Return {(query_id, project, alpha, mode): [rel_at_rank1, ... rel_at_rank10]}."""
    out: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        key = (r["query_id"], r["project"], r["alpha"], r["mode"])
        out[key].append((r["rank"], r["relevance"]))

    ndcg: dict[tuple, float] = {}
    for key, ranked in out.items():
        rels = [rel for _, rel in sorted(ranked, key=lambda x: x[0])]
        ndcg[key] = ndcg_at_k(rels)
    return ndcg


def _query_meta(rows: list[dict]) -> dict[str, dict]:
    meta = {}
    for r in rows:
        meta[r["query_id"]] = {"type": r["qtype"], "project": r["project"], "query": r["query"]}
    return meta


def _print_section(title: str) -> None:
    print()
    print("=" * 90)
    print(title)
    print("=" * 90)


def main() -> None:
    rows = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    meta = _query_meta(rows)
    ndcg = _group_ndcg(rows)

    self_qids = sorted(qid for qid, m in meta.items() if m["project"] == "hybrid-search-mcp")
    ext_qids = sorted(qid for qid, m in meta.items() if m["project"] == "valuein_homepage")

    # -- Self-contained per-query table --
    _print_section(f"SELF-CONTAINED (hybrid-search-mcp, n={len(self_qids)})")
    print(f"{'ID':5s} {'TYPE':10s}  {'OFF':>6s}  " + "  ".join(f"α={a:.1f}:Δ" for a in ALPHAS))
    print("-" * 70)

    per_type_deltas: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    all_deltas: dict[float, list[float]] = defaultdict(list)

    for qid in self_qids:
        m = meta[qid]
        proj = m["project"]
        off = ndcg.get((qid, proj, 0.0, "OFF"), 0.0)
        deltas_str = []
        for a in ALPHAS:
            on = ndcg.get((qid, proj, a, "ON"), 0.0)
            d = on - off
            per_type_deltas[m["type"]][a].append(d)
            all_deltas[a].append(d)
            arrow = "↑" if d > 0.005 else ("↓" if d < -0.005 else "·")
            deltas_str.append(f"{arrow}{d:+6.3f}")
        print(f"{qid:5s} {m['type']:10s}  {off:6.3f}  " + "  ".join(deltas_str))

    # -- Per-type summary --
    _print_section("PER-TYPE Δ NDCG@10 (self-contained, α sweep)")
    print(f"{'TYPE':10s}  {'N':>3s}  " + "  ".join(f"α={a:.1f}: Δ (95% CI)    P(Δ>0)" for a in ALPHAS))
    print("-" * 90)
    for qtype in ("structural", "keyword", "semantic"):
        per_alpha = per_type_deltas.get(qtype, {})
        if not per_alpha:
            continue
        row = [f"{qtype:10s}  {len(next(iter(per_alpha.values()))):>3d}"]
        for a in ALPHAS:
            samples = per_alpha.get(a, [])
            mean, lo, hi, p_pos = bootstrap_ci(samples)
            row.append(f"{mean:+6.3f} [{lo:+6.3f},{hi:+6.3f}]  {p_pos:4.2f}")
        print("  ".join(row))

    # -- Overall --
    _print_section("OVERALL (self-contained)")
    print(f"{'α':>4s}  {'N':>3s}  {'Δ mean':>8s}  {'95% CI':>22s}  {'P(Δ>0)':>7s}")
    print("-" * 60)
    for a in ALPHAS:
        samples = all_deltas[a]
        mean, lo, hi, p_pos = bootstrap_ci(samples)
        print(f"{a:4.1f}  {len(samples):>3d}  {mean:+8.3f}  [{lo:+6.3f}, {hi:+6.3f}]  {p_pos:7.2f}")

    # -- External spot check --
    _print_section(f"EXTERNAL SPOT CHECK (valuein_homepage, n={len(ext_qids)})")
    print("Proxy labels (file_path match → rel=2, same dir → 1, else 0). Directional only.")
    print()
    print(f"{'ID':5s} {'TYPE':10s}  {'OFF':>6s}  " + "  ".join(f"α={a:.1f}:Δ" for a in ALPHAS))
    print("-" * 70)
    ext_deltas: dict[float, list[float]] = defaultdict(list)
    for qid in ext_qids:
        m = meta[qid]
        proj = m["project"]
        off = ndcg.get((qid, proj, 0.0, "OFF"), 0.0)
        deltas_str = []
        for a in ALPHAS:
            on = ndcg.get((qid, proj, a, "ON"), 0.0)
            d = on - off
            ext_deltas[a].append(d)
            arrow = "↑" if d > 0.005 else ("↓" if d < -0.005 else "·")
            deltas_str.append(f"{arrow}{d:+6.3f}")
        print(f"{qid:5s} {m['type']:10s}  {off:6.3f}  " + "  ".join(deltas_str))

    print()
    print(f"{'α':>4s}  {'Δ mean (external)':>20s}")
    for a in ALPHAS:
        samples = ext_deltas[a]
        if samples:
            mean = sum(samples) / len(samples)
            print(f"{a:4.1f}  {mean:+20.3f}")

    print()
    print("Reminder:")
    print("  - Self-contained n=30. Still small; trust CI width more than point estimates.")
    print("  - External n=5 with proxy labels — sanity check only, not a decisive signal.")
    print("  - α with highest P(Δ>0) on structural+keyword is the candidate for production.")


if __name__ == "__main__":
    main()
