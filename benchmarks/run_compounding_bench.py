"""Compounding benchmark — does qa_log make retrieval smarter over time?

Methodology (inspired by LongMemEval / LoCoMo, session-separated Q1a→Q1b):

  Cold phase:
    - Move aside .hybrid-search/qa/, run full reindex so index has zero qa_log chunks
    - For each pair, run Q1b. Score with gold expected_files/primary_target.

  Plant phase:
    - For each pair, run Q1a with qa_log SYNCHRONOUS write so qa markdown files
      land on disk deterministically before the next reindex.

  Warm phase:
    - Run reindex so the new qa files become searchable chunks
    - For each pair, run Q1b again. Score with the same gold.

Delta (warm - cold) is the compounding signal. Non-leaky pairs are the honest
slice: Q1a's logged answer string does not literally contain Q1b's target
file names, so a warm win must come from memory-aware retrieval boosting
related chunks — not identity match on the new qa_log file itself.

The whole run restores the original qa directory on completion (or on SIGINT).
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.memory import qa_log
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator


@dataclass
class Score:
    primary_hit_rank: int | None   # rank of gold primary_target (or module match)
    any_hit_rank: int | None       # rank of first expected_files hit
    recall_at_10: float            # strict gold recall (expected_files only)
    qa_primary_rank: int | None    # rank of THIS pair's planted qa_log (warm only)
    answer_found: bool             # True if any gold or qa_log hit in top-10
    mrr: float
    hits: int
    total_expected: int


def matches_expected(result_path: str, expected_entry: str) -> bool:
    if expected_entry.endswith("/"):
        return result_path.startswith(expected_entry) or result_path == expected_entry.rstrip("/")
    return result_path == expected_entry or result_path.endswith("/" + expected_entry)


def rank_of(results: list[str], expected: str) -> int | None:
    for i, rp in enumerate(results, start=1):
        if matches_expected(rp, expected):
            return i
    return None


def score_query(
    result_paths: list[str],
    module_names: list[str | None],
    pair: dict,
    qa_log_path: str | None = None,
) -> Score:
    """Score a single query's top-k paths against gold.

    ``qa_log_path`` is the relative path of this pair's planted qa file — only
    passed in the warm phase. When a qa_log chunk for *this pair* appears in
    the results it is treated as a primary hit: surfacing the memory of Q1a's
    answer is the compounding win, even when the gold primary_target file is
    displaced further down.
    """
    expected = pair.get("expected_files", [])
    primary = pair.get("primary_target")
    acceptable_modules = set(pair.get("acceptable_module_names", []))

    file_primary_rank = rank_of(result_paths, primary) if primary else None
    module_primary_rank: int | None = None
    if acceptable_modules:
        for i, mname in enumerate(module_names, start=1):
            if mname and mname in acceptable_modules:
                module_primary_rank = i
                break
    qa_primary_rank: int | None = None
    if qa_log_path:
        tail = qa_log_path.split("/")[-1]
        for i, rp in enumerate(result_paths, start=1):
            if rp == qa_log_path or rp.endswith(tail):
                qa_primary_rank = i
                break

    # Strict primary rank = gold only (code/docs). Used for "does the system
    # find the source of truth" metrics.
    strict_candidates = [r for r in (file_primary_rank, module_primary_rank) if r is not None]
    primary_rank = min(strict_candidates) if strict_candidates else None

    any_rank = None
    hits = 0
    for exp in expected:
        r = rank_of(result_paths, exp)
        if r is not None:
            hits += 1
            if any_rank is None or r < any_rank:
                any_rank = r
    gold_recall = hits / max(1, len(expected))
    mrr = (1.0 / any_rank) if any_rank else 0.0

    # answer_found: did the user get a useful result in top-10?
    # Either a gold file (any_rank set) or the planted qa_log (memory surface).
    # Binary, monotone — unlike recall_with_memory, the denominator doesn't
    # change between cold and warm, so cold→warm deltas are directly meaningful.
    answer_found = (any_rank is not None) or (qa_primary_rank is not None)

    return Score(
        primary_hit_rank=primary_rank,
        any_hit_rank=any_rank,
        recall_at_10=gold_recall,
        qa_primary_rank=qa_primary_rank,
        answer_found=answer_found,
        mrr=mrr,
        hits=hits,
        total_expected=len(expected),
    )


def run_query(orch: SearchOrchestrator, project_name: str, query: str, limit: int = 10):
    """Return (paths, module_names, qa_hit_count, response) for one query."""
    resp = orch.hybrid_search(query=query, project=project_name, limit=limit)
    seen: list[str] = []
    module_names: list[str | None] = []
    qa_hit_count = 0
    _MODULE_LIKE = {"module", "module_member"}
    for hit in resp.results:
        if hit.node_type == "qa_log":
            qa_hit_count += 1
        if hit.file_path in seen:
            continue
        seen.append(hit.file_path)
        module_names.append(hit.name if hit.node_type in _MODULE_LIKE else None)
    return seen, module_names, qa_hit_count, resp


def clear_qa_dir(project_root: Path) -> Path | None:
    """Move .hybrid-search/qa/ aside so cold runs on an empty memory.

    Returns the backup path (to restore later), or None if no qa dir existed.
    """
    qa_root = project_root / ".hybrid-search" / "qa"
    if not qa_root.exists():
        return None
    backup = project_root / ".hybrid-search" / f"qa.backup-{int(time.time())}"
    shutil.move(str(qa_root), str(backup))
    return backup


def restore_qa_dir(project_root: Path, backup: Path | None) -> None:
    """Restore the original qa directory, dropping the run's synthetic logs."""
    qa_root = project_root / ".hybrid-search" / "qa"
    if qa_root.exists():
        shutil.rmtree(qa_root)
    if backup is not None and backup.exists():
        shutil.move(str(backup), str(qa_root))


def reindex(project_path: Path) -> None:
    """Run hybrid-search reindex via subprocess to pick up qa file changes.

    Subprocess isolates tantivy writer locks — the in-process orchestrator keeps
    a reader open, and writer/reader coexistence is the safest across platforms.
    """
    cmd = [sys.executable, "-m", "hybrid_search.cli", "reindex", "--cwd", str(project_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"reindex failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )


def rebuild_orchestrator() -> tuple[SearchOrchestrator, str]:
    """Fresh orchestrator so tantivy readers pick up the new index state."""
    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    return SearchOrchestrator(config=config, registry=registry, embedder=embedder), config.global_dir


def plant_q1a(
    orch: SearchOrchestrator,
    project_name: str,
    project_path: Path,
    pairs: list[dict],
) -> dict[str, str]:
    """Run every Q1a with synchronous qa_log write.

    Returns ``{pair_id: relative_qa_path}`` so the warm scorer can credit a
    retrieval that surfaces a pair's own qa_log chunk — hitting the memory IS
    hitting the answer Q1a produced.
    """
    planted: dict[str, str] = {}
    for pair in pairs:
        resp = orch.hybrid_search(query=pair["q1a"], project=project_name, limit=10)
        path = qa_log.record(
            query=pair["q1a"],
            response=resp,
            cwd=str(project_path),
            async_write=False,
        )
        if path is not None:
            try:
                rel = str(path.relative_to(project_path))
            except ValueError:
                rel = str(path)
            planted[pair["id"]] = rel
    return planted


def aggregate(rows: list[dict]) -> dict:
    """Mean/top-1/top-5 over a list of per-query score dicts."""
    n = len(rows) or 1
    return {
        "n": len(rows),
        "primary_hit_rate": sum(1 for s in rows if s["primary_hit_rank"] is not None) / n,
        "primary_top1": sum(1 for s in rows if s["primary_hit_rank"] == 1) / n,
        "primary_top5": sum(1 for s in rows if s["primary_hit_rank"] is not None and s["primary_hit_rank"] <= 5) / n,
        "recall_at_10_mean": mean(s["recall_at_10"] for s in rows) if rows else 0.0,
        "mrr_mean": mean(s["mrr"] for s in rows) if rows else 0.0,
        "qa_hit_rate": sum(1 for s in rows if s.get("qa_hit_count", 0) > 0) / n,
        "qa_top1_rate": sum(1 for s in rows if s.get("qa_top1")) / n,
        "memory_primary_rate": sum(1 for s in rows if s.get("qa_primary_rank") is not None) / n,
        "answer_found_rate": sum(1 for s in rows if s.get("answer_found")) / n,
    }


def format_markdown_report(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Compounding benchmark — {report['project']}")
    lines.append("")
    lines.append(f"- Date: {report['date']}")
    lines.append(f"- Pairs: {report['total_pairs']}  (non-leaky: {report['non_leaky_pairs']}, leaky: {report['leaky_pairs']})")
    lines.append(f"- Cold qa chunks: {report['cold_qa_chunk_count']}")
    lines.append(f"- Warm qa chunks: {report['warm_qa_chunk_count']}  (+{report['warm_qa_chunk_count'] - report['cold_qa_chunk_count']})")
    lines.append("")

    def table(title: str, cold: dict, warm: dict) -> list[str]:
        rows = [
            f"## {title}",
            "",
            "| metric | cold | warm | Δ |",
            "|---|---:|---:|---:|",
        ]
        metrics = [
            ("answer_found_rate", "{:.2%}"),
            ("memory_primary_rate", "{:.2%}"),
            ("primary_hit_rate", "{:.2%}"),
            ("primary_top1", "{:.2%}"),
            ("primary_top5", "{:.2%}"),
            ("recall_at_10_mean", "{:.3f}"),
            ("mrr_mean", "{:.3f}"),
        ]
        for key, fmt in metrics:
            c = cold[key]
            w = warm[key]
            d = w - c
            rows.append(f"| {key} | {fmt.format(c)} | {fmt.format(w)} | {fmt.format(d) if '%' not in fmt else '{:+.2%}'.format(d)} |")
        rows.append("")
        return rows

    lines.append("## Track A: identity re-query (user asks the same question again)")
    lines.append("")
    lines.append("Upper bound for memory recall — no wording variation.")
    lines.append("")
    lines += table("Identity (Q1a repeated)", report["identity"]["overall"]["cold"], report["identity"]["overall"]["warm"])

    lines.append("## Track B: paraphrased follow-up (same topic, different wording)")
    lines.append("")
    lines.append("Realistic follow-up scenario. Measures whether the memory boost can surface a past Q&A when the new query keeps the principal noun phrases but rewords the rest.")
    lines.append("")
    lines += table("Paraphrase — overall", report["paraphrase"]["overall"]["cold"], report["paraphrase"]["overall"]["warm"])
    if report["paraphrase"]["non_leaky"]:
        lines += table("Paraphrase — non-leaky subset", report["paraphrase"]["non_leaky"]["cold"], report["paraphrase"]["non_leaky"]["warm"])
    if report["leaky_pairs"] > 0 and report["paraphrase"]["leaky"]:
        lines += table("Paraphrase — leaky subset (transparency)", report["paraphrase"]["leaky"]["cold"], report["paraphrase"]["leaky"]["warm"])

    lines.append("## Per-pair details")
    lines.append("")
    lines.append("| id | leakage | cold rank | warm rank | Δ rank | cold R@10 | warm R@10 | qa top-10 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for row in report["per_pair"]:
        cold_rank = row["cold"]["primary_hit_rank"] or "—"
        warm_rank = row["warm"]["primary_hit_rank"] or "—"
        drank = ""
        if isinstance(cold_rank, int) and isinstance(warm_rank, int):
            drank = f"{cold_rank - warm_rank:+d}"
        elif cold_rank == "—" and isinstance(warm_rank, int):
            drank = f"new@{warm_rank}"
        elif isinstance(cold_rank, int) and warm_rank == "—":
            drank = "lost"
        lines.append(
            f"| {row['id']} | {row['leakage_risk']} | {cold_rank} | {warm_rank} | {drank} | "
            f"{row['cold']['recall_at_10']:.2f} | {row['warm']['recall_at_10']:.2f} | "
            f"{row['warm']['qa_hit_count']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=str(Path(__file__).parent / "compounding_pairs.json"))
    ap.add_argument("--out-json", default=str(Path(__file__).parent / "compounding_results.json"))
    ap.add_argument("--out-md", default=None, help="Markdown report path (default: compounding_report_YYYY-MM-DD.md)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument(
        "--skip-restore",
        action="store_true",
        help="Leave qa dir populated with Q1a plants instead of restoring backup",
    )
    args = ap.parse_args()

    with open(args.pairs) as f:
        spec = json.load(f)

    project_name = spec["project"]
    project_path = Path(spec["project_path"])
    pairs: list[dict] = spec["pairs"]
    total_pairs = len(pairs)
    leaky = [p for p in pairs if p.get("leakage_risk") == "high"]
    non_leaky = [p for p in pairs if p.get("leakage_risk") != "high"]
    print(f"Project: {project_name}  ({project_path})")
    print(f"Pairs: {total_pairs}  (non-leaky: {len(non_leaky)}, leaky: {len(leaky)})")

    backup_path: Path | None = None
    restored = {"done": False}

    def do_restore():
        if restored["done"]:
            return
        restore_qa_dir(project_path, backup_path)
        restored["done"] = True
        print("Restored original qa directory.")

    def sigint_handler(signum, frame):
        print("\nInterrupted — restoring qa dir …")
        do_restore()
        sys.exit(130)

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        # ── Cold phase ────────────────────────────────────────────
        print("\n[cold] moving aside qa dir + reindexing (empty memory) …")
        backup_path = clear_qa_dir(project_path)
        reindex(project_path)

        orch, _ = rebuild_orchestrator()
        cold_qa_chunk_count = _count_qa_chunks(project_name)
        print(f"[cold] qa chunks in index: {cold_qa_chunk_count}")

        cold_rows: list[dict] = []
        cold_identity_rows: list[dict] = []
        for pair in pairs:
            # Paraphrase (Q1b)
            paths, modnames, qa_hits, _ = run_query(orch, project_name, pair["q1b"], limit=args.limit)
            s = score_query(paths, modnames, pair)
            cold_rows.append({
                "id": pair["id"],
                "leakage_risk": pair.get("leakage_risk", "low"),
                "category": pair["category"],
                "primary_hit_rank": s.primary_hit_rank,
                "any_hit_rank": s.any_hit_rank,
                "recall_at_10": s.recall_at_10,
                "answer_found": s.answer_found,
                "qa_primary_rank": s.qa_primary_rank,
                "mrr": s.mrr,
                "qa_hit_count": qa_hits,
                "qa_top1": qa_hits > 0 and any(p.endswith(".md") for p in paths[:1]),
                "top_paths": paths[:5],
            })
            # Identity (Q1a) — cold baseline for the upper-bound comparison
            paths2, modnames2, qa_hits2, _ = run_query(orch, project_name, pair["q1a"], limit=args.limit)
            s2 = score_query(paths2, modnames2, pair)
            cold_identity_rows.append({
                "id": pair["id"],
                "leakage_risk": pair.get("leakage_risk", "low"),
                "category": pair["category"],
                "primary_hit_rank": s2.primary_hit_rank,
                "any_hit_rank": s2.any_hit_rank,
                "recall_at_10": s2.recall_at_10,
                "answer_found": s2.answer_found,
                "qa_primary_rank": s2.qa_primary_rank,
                "mrr": s2.mrr,
                "qa_hit_count": qa_hits2,
                "qa_top1": qa_hits2 > 0 and any(p.endswith(".md") for p in paths2[:1]),
                "top_paths": paths2[:5],
            })
            print(f"  [{pair['id']} cold] para rank={s.primary_hit_rank} R@10={s.recall_at_10:.2f} | identity rank={s2.primary_hit_rank}")

        # ── Plant phase ──────────────────────────────────────────
        print("\n[plant] running Q1a × {} with sync qa_log writes …".format(len(pairs)))
        planted = plant_q1a(orch, project_name, project_path, pairs)
        print(f"[plant] planted {len(planted)} qa files")

        # ── Warm phase ───────────────────────────────────────────
        print("\n[warm] reindexing to absorb qa files …")
        reindex(project_path)
        orch, _ = rebuild_orchestrator()
        warm_qa_chunk_count = _count_qa_chunks(project_name)
        print(f"[warm] qa chunks in index: {warm_qa_chunk_count}")

        warm_rows: list[dict] = []
        for pair in pairs:
            paths, modnames, qa_hits, _ = run_query(orch, project_name, pair["q1b"], limit=args.limit)
            s = score_query(paths, modnames, pair, qa_log_path=planted.get(pair["id"]))
            warm_rows.append({
                "id": pair["id"],
                "leakage_risk": pair.get("leakage_risk", "low"),
                "category": pair["category"],
                "primary_hit_rank": s.primary_hit_rank,
                "any_hit_rank": s.any_hit_rank,
                "recall_at_10": s.recall_at_10,
                "answer_found": s.answer_found,
                "qa_primary_rank": s.qa_primary_rank,
                "mrr": s.mrr,
                "qa_hit_count": qa_hits,
                "qa_top1": qa_hits > 0 and any(p.endswith(".md") for p in paths[:1]),
                "top_paths": paths[:5],
            })
            print(f"  [{pair['id']} warm] rank={s.primary_hit_rank} R@10={s.recall_at_10:.2f} qa_hits={qa_hits}")

        # ── Identity phase ───────────────────────────────────────
        # Upper bound: user asks the exact same question again. This is the
        # cleanest test of "did we remember?" — no paraphrase noise. Shares
        # the warm qa_log state with the paraphrase phase above, so no extra
        # plant/reindex needed.
        print("\n[identity] re-querying with Q1a verbatim (upper bound) …")
        identity_rows: list[dict] = []
        for pair in pairs:
            paths, modnames, qa_hits, _ = run_query(orch, project_name, pair["q1a"], limit=args.limit)
            s = score_query(paths, modnames, pair, qa_log_path=planted.get(pair["id"]))
            identity_rows.append({
                "id": pair["id"],
                "leakage_risk": pair.get("leakage_risk", "low"),
                "category": pair["category"],
                "primary_hit_rank": s.primary_hit_rank,
                "any_hit_rank": s.any_hit_rank,
                "recall_at_10": s.recall_at_10,
                "answer_found": s.answer_found,
                "qa_primary_rank": s.qa_primary_rank,
                "mrr": s.mrr,
                "qa_hit_count": qa_hits,
                "qa_top1": qa_hits > 0 and any(p.endswith(".md") for p in paths[:1]),
                "top_paths": paths[:5],
            })
            print(f"  [{pair['id']} identity] rank={s.primary_hit_rank} qa_hits={qa_hits}")

        # ── Aggregate ───────────────────────────────────────────
        by_id_cold = {r["id"]: r for r in cold_rows}
        by_id_warm = {r["id"]: r for r in warm_rows}
        non_leaky_ids = {p["id"] for p in non_leaky}
        leaky_ids = {p["id"] for p in leaky}

        overall_cold_agg = aggregate(cold_rows)
        overall_warm_agg = aggregate(warm_rows)
        non_leaky_cold_agg = aggregate([r for r in cold_rows if r["id"] in non_leaky_ids]) if non_leaky_ids else None
        non_leaky_warm_agg = aggregate([r for r in warm_rows if r["id"] in non_leaky_ids]) if non_leaky_ids else None
        leaky_cold_agg = aggregate([r for r in cold_rows if r["id"] in leaky_ids]) if leaky_ids else None
        leaky_warm_agg = aggregate([r for r in warm_rows if r["id"] in leaky_ids]) if leaky_ids else None

        by_id_cold_identity = {r["id"]: r for r in cold_identity_rows}
        by_id_warm_identity = {r["id"]: r for r in identity_rows}
        per_pair = [
            {
                "id": p["id"],
                "leakage_risk": p.get("leakage_risk", "low"),
                "category": p["category"],
                "q1a": p["q1a"],
                "q1b": p["q1b"],
                "cold": by_id_cold[p["id"]],
                "warm": by_id_warm[p["id"]],
                "cold_identity": by_id_cold_identity[p["id"]],
                "warm_identity": by_id_warm_identity[p["id"]],
            }
            for p in pairs
        ]

        # Identity subset aggregates (Q1a used as Q1b — upper bound)
        overall_identity_cold_agg = aggregate(cold_identity_rows)
        overall_identity_warm_agg = aggregate(identity_rows)

        report = {
            "date": time.strftime("%Y-%m-%d"),
            "project": project_name,
            "total_pairs": total_pairs,
            "non_leaky_pairs": len(non_leaky),
            "leaky_pairs": len(leaky),
            "cold_qa_chunk_count": cold_qa_chunk_count,
            "warm_qa_chunk_count": warm_qa_chunk_count,
            "paraphrase": {
                "overall": {"cold": overall_cold_agg, "warm": overall_warm_agg},
                "non_leaky": {"cold": non_leaky_cold_agg, "warm": non_leaky_warm_agg} if non_leaky_ids else None,
                "leaky": {"cold": leaky_cold_agg, "warm": leaky_warm_agg} if leaky_ids else None,
            },
            "identity": {
                "overall": {"cold": overall_identity_cold_agg, "warm": overall_identity_warm_agg},
            },
            "per_pair": per_pair,
        }

        out_json = Path(args.out_json)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nJSON report → {out_json}")

        out_md = Path(args.out_md) if args.out_md else Path(__file__).parent / f"compounding_report_{report['date']}.md"
        out_md.write_text(format_markdown_report(report))
        print(f"Markdown report → {out_md}")

        # ── Headline numbers ────────────────────────────────────
        i_cold = overall_identity_cold_agg
        i_warm = overall_identity_warm_agg
        p_cold = overall_cold_agg
        p_warm = overall_warm_agg
        print("\n── Headline ──")
        print("Track A (identity — user repeats the same question) — upper bound:")
        print(f"  answer_found:        {i_cold['answer_found_rate']:.2%} → {i_warm['answer_found_rate']:.2%}  (Δ{i_warm['answer_found_rate'] - i_cold['answer_found_rate']:+.2%})")
        print(f"  memory surface rate: {i_cold['memory_primary_rate']:.2%} → {i_warm['memory_primary_rate']:.2%}  (Δ{i_warm['memory_primary_rate'] - i_cold['memory_primary_rate']:+.2%})")
        print(f"  gold recall@10:      {i_cold['recall_at_10_mean']:.3f} → {i_warm['recall_at_10_mean']:.3f}  (Δ{i_warm['recall_at_10_mean'] - i_cold['recall_at_10_mean']:+.3f})  [no regression target]")
        print("Track B (paraphrase — same topic, different wording) — realistic case:")
        print(f"  answer_found:        {p_cold['answer_found_rate']:.2%} → {p_warm['answer_found_rate']:.2%}  (Δ{p_warm['answer_found_rate'] - p_cold['answer_found_rate']:+.2%})")
        print(f"  memory surface rate: {p_cold['memory_primary_rate']:.2%} → {p_warm['memory_primary_rate']:.2%}  (Δ{p_warm['memory_primary_rate'] - p_cold['memory_primary_rate']:+.2%})")
        print(f"  gold recall@10:      {p_cold['recall_at_10_mean']:.3f} → {p_warm['recall_at_10_mean']:.3f}  (Δ{p_warm['recall_at_10_mean'] - p_cold['recall_at_10_mean']:+.3f})  [no regression target]")
        if non_leaky_cold_agg:
            n_cold = non_leaky_cold_agg
            n_warm = non_leaky_warm_agg
            print("Track B non-leaky subset (no primary_target literal in Q1b):")
            print(f"  answer_found:        {n_cold['answer_found_rate']:.2%} → {n_warm['answer_found_rate']:.2%}  (Δ{n_warm['answer_found_rate'] - n_cold['answer_found_rate']:+.2%})")
            print(f"  memory surface rate: {n_cold['memory_primary_rate']:.2%} → {n_warm['memory_primary_rate']:.2%}")

    finally:
        if not args.skip_restore:
            do_restore()


def _count_qa_chunks(project_name: str) -> int:
    """Count all qa_log chunks in the project's store DB (bypass search top-k)."""
    import sqlite3
    from hybrid_search.storage.indexes import IndexPaths, get_project_dir

    try:
        config = load_config()
        registry = ProjectRegistry(config.global_dir)
        p = registry.get_by_name(project_name)
        if p is None:
            return 0
        pdir = get_project_dir(config.projects_dir, p.id)
        idx = IndexPaths(pdir)
        conn = sqlite3.connect(str(idx.store_db))
        try:
            cur = conn.execute("SELECT COUNT(*) FROM chunks WHERE node_type = 'qa_log'")
            return int(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


if __name__ == "__main__":
    main()
