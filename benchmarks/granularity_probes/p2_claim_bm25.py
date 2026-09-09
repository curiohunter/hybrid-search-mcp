"""P2 — how far does claim-level BM25 alone get, with zero embedding?

Probe (b) split turns into sentences and embedded all of them: 46 minutes and
346 MB for one project. It never checked the lexical half. Adding claims to a
tantivy index costs no embedding at all, so if most of the gain is lexical the
design loses its price tag.

Builds a throwaway BM25 index over claims in a scratch directory (production
index untouched), then for each gold query: retrieve top-N claims, fold them
into their parent chunks by the parent's best claim score, and report where the
parent that holds the answer lands. The chunk-level arm is the same query
against the parents themselves, so the two are comparable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from hybrid_search.config import load_config  # noqa: E402
from claim_split import body_for_claims, split_claims  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.bm25 import BM25Engine  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def report(label: str, ranks: list[int | None], n: int) -> None:
    found = [r for r in ranks if r]
    top3 = sum(1 for r in found if r <= 3)
    mrr = sum(1.0 / r for r in found) / n
    lo, hi = wilson(len(found), n)
    lo3, hi3 = wilson(top3, n)
    print(f"{label:16s} found {len(found)/n:.2f} [{lo:.2f},{hi:.2f}]   "
          f"top3 {top3/n:.2f} [{lo3:.2f},{hi3:.2f}]   MRR {mrr:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--gold", required=True, action="append")
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--node-types", default="conv_turn,qa_log,memory_card")
    ap.add_argument("--depth", type=int, default=300)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    scratch = Path(args.scratch)
    node_types = args.node_types.split(",")

    gold_sets = [json.loads(Path(g).read_text(encoding="utf-8")) for g in args.gold]
    project_path = gold_sets[0]["project_path"]
    pinfo = reg.get_by_path(project_path)
    conn = sqlite3.connect(
        f"file:{Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)

    claim_dir = scratch / "claim_bm25"
    if args.rebuild and claim_dir.exists():
        shutil.rmtree(claim_dir)
    build = not claim_dir.exists()
    engine = BM25Engine(claim_dir)
    parent_of: dict[str, str] = {}
    parent_text: dict[str, str] = {}

    q = ("select id, node_type, content from chunks where project_id=? "
         f"and node_type in ({','.join('?' * len(node_types))})")
    n_claims = 0
    for cid, node_type, content in conn.execute(q, (pinfo.id, *node_types)):
        parent_text[cid] = content or ""
        claims = split_claims(body_for_claims(content or "", node_type))
        for i, c in enumerate(claims):
            claim_id = f"{cid}#c{i:03d}"
            parent_of[claim_id] = cid
            n_claims += 1
            if build:
                engine.add(claim_id, c.kind, claim_id, c.text)
    if build:
        engine.commit()
    print(f"claims indexed: {n_claims:,}  (engine rows {engine.count:,})\n")

    chunk_engine = BM25Engine(
        Path(cfg.projects_dir) / pinfo.id / "tantivy", read_only=True)

    for gold in gold_sets:
        name = Path(gold.get("_name", "gold")).name
        rows_claim: list[int | None] = []
        rows_chunk: list[int | None] = []
        for query in gold["queries"]:
            phrases = query["any_of"]

            hits = engine.search(query["query"], limit=args.depth)
            best: dict[str, float] = {}
            for h in hits:
                p = parent_of.get(h.chunk_id)
                if p and p not in best:
                    best[p] = h.score
            ranked = sorted(best, key=lambda p: -best[p])
            rows_claim.append(next(
                (i for i, p in enumerate(ranked, 1)
                 if any(ph in parent_text.get(p, "") for ph in phrases)), None))

            ch = chunk_engine.search(query["query"], limit=args.depth)
            rows_chunk.append(next(
                (i for i, h in enumerate(ch, 1)
                 if any(ph in parent_text.get(h.chunk_id, "") for ph in phrases)), None))

        n = len(gold["queries"])
        print(f"--- {gold.get('label', 'set')} (n={n}) ---")
        report("chunk BM25", rows_chunk, n)
        report("claim BM25", rows_claim, n)
        gained = sum(1 for a, b in zip(rows_chunk, rows_claim)
                     if (b and b <= 3) and not (a and a <= 3))
        lost = sum(1 for a, b in zip(rows_chunk, rows_claim)
                   if (a and a <= 3) and not (b and b <= 3))
        print(f"top3 gained {gained} · lost {lost}\n")


if __name__ == "__main__":
    main()
