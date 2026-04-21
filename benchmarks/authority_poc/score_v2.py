"""Full L6 scoring — per-query NDCG@10 + MRR@10, α comparison, bootstrap 95% CI.

Reads results_v2.json (produced by run_v2.py) and emits:
  1. Per-query NDCG@10 + MRR@10 for each α with OFF baseline.
  2. Per-type mean Δ(α) + 95% bootstrap CI (NDCG and MRR).
  3. "Δ > 0 probability" over bootstrap samples.
  4. Per-project breakdown (self-contained + external projects).

13회차 확장:
  - MRR@10 추가 (rank-weighted reciprocal rank over first relevant result).
  - external_queries.json 'projects' 배열 구조 지원 → 프로젝트별 별도 표.

Sample sizes:
  - self-contained hybrid-search-mcp: n=45 (structural 15 / keyword 15 / semantic 15)
  - external: valuein n=5, mathontonlogy n=5, breeze n=5 (proxy labels)
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
SELF_PROJECT = "hybrid-search-mcp"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


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


def mrr_at_k(relevances: list[int], k: int = 10) -> float:
    """Reciprocal rank of first relevant item (rel >= 1) in top-k. 0 if none."""
    for i, rel in enumerate(relevances[:k]):
        if rel >= 1:
            return 1.0 / (i + 1)
    return 0.0


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


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_metrics(rows: list[dict]):
    """Return {(query_id, project, alpha, mode): {'ndcg': x, 'mrr': y}}."""
    ranked_rel: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        key = (r["query_id"], r["project"], r["alpha"], r["mode"])
        ranked_rel[key].append((r["rank"], r["relevance"]))

    metrics: dict[tuple, dict] = {}
    for key, ranked in ranked_rel.items():
        rels = [rel for _, rel in sorted(ranked, key=lambda x: x[0])]
        metrics[key] = {
            "ndcg": ndcg_at_k(rels),
            "mrr": mrr_at_k(rels),
        }
    return metrics


def _query_meta(rows: list[dict]) -> dict[str, dict]:
    meta = {}
    for r in rows:
        meta[r["query_id"]] = {
            "type": r["qtype"],
            "project": r["project"],
            "query": r["query"],
        }
    return meta


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_section(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def _format_ci(mean: float, lo: float, hi: float) -> str:
    return f"{mean:+6.3f} [{lo:+6.3f},{hi:+6.3f}]"


def _per_query_table(
    qids: list[str],
    meta: dict,
    metrics: dict,
    metric_key: str,
    label: str,
) -> None:
    """Print per-query OFF + Δα table for a given metric ('ndcg'|'mrr')."""
    print(f"{'ID':5s} {'TYPE':10s}  {'OFF':>6s}  " + "  ".join(f"α={a:.1f}:Δ{label}" for a in ALPHAS))
    print("-" * (30 + len(ALPHAS) * 13))
    for qid in qids:
        m = meta[qid]
        proj = m["project"]
        off_entry = metrics.get((qid, proj, 0.0, "OFF"), {"ndcg": 0.0, "mrr": 0.0})
        off = off_entry[metric_key]
        cells = []
        for a in ALPHAS:
            on_entry = metrics.get((qid, proj, a, "ON"), {"ndcg": 0.0, "mrr": 0.0})
            d = on_entry[metric_key] - off
            arrow = "↑" if d > 0.005 else ("↓" if d < -0.005 else "·")
            cells.append(f"{arrow}{d:+6.3f}")
        print(f"{qid:5s} {m['type']:10s}  {off:6.3f}  " + "  ".join(cells))


def _collect_deltas(
    qids: list[str],
    meta: dict,
    metrics: dict,
    metric_key: str,
) -> tuple[dict[float, list[float]], dict[str, dict[float, list[float]]]]:
    """Return (overall_by_alpha, per_type_by_alpha) for the given metric."""
    overall: dict[float, list[float]] = defaultdict(list)
    per_type: dict[str, dict[float, list[float]]] = defaultdict(lambda: defaultdict(list))
    for qid in qids:
        m = meta[qid]
        proj = m["project"]
        off = metrics.get((qid, proj, 0.0, "OFF"), {metric_key: 0.0})[metric_key]
        for a in ALPHAS:
            on = metrics.get((qid, proj, a, "ON"), {metric_key: 0.0})[metric_key]
            d = on - off
            overall[a].append(d)
            per_type[m["type"]][a].append(d)
    return overall, per_type


def _print_overall_table(overall: dict[float, list[float]], metric_label: str) -> None:
    header = f"  {'α':>4s}  {'N':>3s}  {'Δ mean':>8s}  {'95% CI':>22s}  {'P(Δ>0)':>7s}"
    print(header + f"   — {metric_label}")
    print("  " + "-" * (len(header) - 2))
    for a in ALPHAS:
        samples = overall[a]
        if not samples:
            continue
        mean, lo, hi, p_pos = bootstrap_ci(samples)
        print(f"  {a:4.1f}  {len(samples):>3d}  {mean:+8.3f}  [{lo:+6.3f}, {hi:+6.3f}]  {p_pos:7.2f}")


def _print_per_type(per_type: dict[str, dict[float, list[float]]], metric_label: str) -> None:
    print(f"  {'TYPE':10s}  {'N':>3s}  " + "  ".join(f"α={a:.1f}: Δ (95% CI)        P(Δ>0)" for a in ALPHAS) + f"   — {metric_label}")
    print("  " + "-" * 110)
    for qtype in ("structural", "keyword", "semantic"):
        per_alpha = per_type.get(qtype, {})
        if not per_alpha:
            continue
        sample_len = len(next(iter(per_alpha.values())))
        row = [f"  {qtype:10s}  {sample_len:>3d}"]
        for a in ALPHAS:
            samples = per_alpha.get(a, [])
            mean, lo, hi, p_pos = bootstrap_ci(samples)
            row.append(f"{_format_ci(mean, lo, hi)}  {p_pos:4.2f}")
        print("  ".join(row))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    rows = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    meta = _query_meta(rows)
    metrics = _group_metrics(rows)

    # -- Partition query ids by project --
    qids_by_project: dict[str, list[str]] = defaultdict(list)
    for qid, m in meta.items():
        qids_by_project[m["project"]].append(qid)
    for project in qids_by_project:
        qids_by_project[project].sort()

    self_qids = qids_by_project.get(SELF_PROJECT, [])
    external_projects = sorted(p for p in qids_by_project if p != SELF_PROJECT)

    # ========================================================================
    # Self-contained
    # ========================================================================
    _print_section(f"SELF-CONTAINED ({SELF_PROJECT}, n={len(self_qids)}) — NDCG@10")
    _per_query_table(self_qids, meta, metrics, "ndcg", "NDCG")

    _print_section(f"SELF-CONTAINED ({SELF_PROJECT}, n={len(self_qids)}) — MRR@10")
    _per_query_table(self_qids, meta, metrics, "mrr", "MRR")

    self_overall_ndcg, self_per_type_ndcg = _collect_deltas(self_qids, meta, metrics, "ndcg")
    self_overall_mrr, self_per_type_mrr = _collect_deltas(self_qids, meta, metrics, "mrr")

    _print_section("PER-TYPE Δ NDCG@10 (self-contained)")
    _print_per_type(self_per_type_ndcg, "NDCG")

    _print_section("PER-TYPE Δ MRR@10 (self-contained)")
    _print_per_type(self_per_type_mrr, "MRR")

    _print_section("OVERALL (self-contained)")
    _print_overall_table(self_overall_ndcg, "NDCG@10")
    print()
    _print_overall_table(self_overall_mrr, "MRR@10")

    # ========================================================================
    # External per-project
    # ========================================================================
    combined_ext_ndcg: dict[float, list[float]] = defaultdict(list)
    combined_ext_mrr: dict[float, list[float]] = defaultdict(list)

    for project in external_projects:
        ext_qids = qids_by_project[project]
        _print_section(
            f"EXTERNAL — {project} (n={len(ext_qids)}) — proxy labels (directional only)"
        )
        _per_query_table(ext_qids, meta, metrics, "ndcg", "NDCG")
        print()
        _per_query_table(ext_qids, meta, metrics, "mrr", "MRR")

        ov_ndcg, _ = _collect_deltas(ext_qids, meta, metrics, "ndcg")
        ov_mrr, _ = _collect_deltas(ext_qids, meta, metrics, "mrr")
        print()
        print(f"  {'α':>4s}  {'Δ NDCG':>10s}  {'Δ MRR':>10s}")
        for a in ALPHAS:
            nd = sum(ov_ndcg[a]) / len(ov_ndcg[a]) if ov_ndcg[a] else 0.0
            mr = sum(ov_mrr[a]) / len(ov_mrr[a]) if ov_mrr[a] else 0.0
            print(f"  {a:4.1f}  {nd:+10.3f}  {mr:+10.3f}")

        for a in ALPHAS:
            combined_ext_ndcg[a].extend(ov_ndcg[a])
            combined_ext_mrr[a].extend(ov_mrr[a])

    # ========================================================================
    # External combined
    # ========================================================================
    if external_projects:
        total_ext = sum(len(qids_by_project[p]) for p in external_projects)
        _print_section(f"EXTERNAL COMBINED (n={total_ext}) — all external projects pooled")
        _print_overall_table(combined_ext_ndcg, "NDCG@10")
        print()
        _print_overall_table(combined_ext_mrr, "MRR@10")

    # ========================================================================
    # ALL COMBINED
    # ========================================================================
    all_qids = self_qids + [qid for p in external_projects for qid in qids_by_project[p]]
    all_ndcg, _ = _collect_deltas(all_qids, meta, metrics, "ndcg")
    all_mrr, _ = _collect_deltas(all_qids, meta, metrics, "mrr")
    _print_section(f"GRAND TOTAL (self + external, n={len(all_qids)})")
    _print_overall_table(all_ndcg, "NDCG@10")
    print()
    _print_overall_table(all_mrr, "MRR@10")

    print()
    print("Reminders:")
    print("  - Self-contained n=45 gives tighter CIs than 30 but still bootstrap-level.")
    print("  - External n=5 per project; proxy labels are directional only, not a decisive signal.")
    print("  - α with highest P(Δ>0) on structural+keyword in self is the production candidate.")
    print("  - MRR rewards getting ONE relevant item high; NDCG rewards full top-k ordering.")


if __name__ == "__main__":
    main()
