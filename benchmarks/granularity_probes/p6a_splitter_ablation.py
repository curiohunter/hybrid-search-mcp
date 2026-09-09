"""P6a — is the structure-aware splitter worth its code?

Round-2 review, point #9: the quality gate removes 0.0-0.4% of claims, which
means the splitter's "structural noise removal" almost never fires. If so, the
gain measured in P2 may come from nothing more than a smaller retrieval unit,
and plain sentence splitting would do the same job with none of the rules.

Builds two scratch BM25 indexes over the same parents — one cut by the
structure splitter, one cut on punctuation — and runs the identical fold-and-
rank comparison against both. If they tie, the splitter should be deleted.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.claim_split import body_for_claims, split_claims  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.bm25 import BM25Engine  # noqa: E402

PARENTS = ("conv_turn", "qa_log", "memory_card")
_SENT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def sentence_units(text: str, node_type: str) -> list[str]:
    """The naive baseline: punctuation only, same length bounds as claims."""
    out = []
    for s in _SENT_RE.split(text or ""):
        s = s.strip()
        if 12 <= len(s) <= 400 and re.search(r"[가-힣A-Za-z]", s):
            out.append(s)
    return out


def build(engine_dir: Path, rows, mode: str) -> tuple[dict[str, str], int]:
    if engine_dir.exists():
        shutil.rmtree(engine_dir)
    engine = BM25Engine(engine_dir)
    parent_of: dict[str, str] = {}
    for cid, node_type, content in rows:
        if mode == "claim":
            units = [c.text for c in
                     split_claims(body_for_claims(content or "", node_type))]
        else:
            units = sentence_units(content or "", node_type)
        for i, text in enumerate(units):
            uid = f"{cid}#u{i:03d}"
            parent_of[uid] = cid
            engine.add(uid, mode, uid, text)
    engine.commit()
    return parent_of, engine.count


def evaluate(engine_dir, parent_of, parent_text, gold, depth, parents_kept):
    engine = BM25Engine(engine_dir, read_only=True)
    ranks = []
    for q in gold["queries"]:
        hits = engine.search(q["query"], limit=depth)
        best: dict[str, float] = {}
        for h in hits:
            p = parent_of.get(h.chunk_id)
            if p and p not in best:
                best[p] = h.score
        ranked = sorted(best, key=lambda p: -best[p])[:parents_kept]
        ranks.append(next((i for i, p in enumerate(ranked, 1)
                           if any(ph in parent_text.get(p, "")
                                  for ph in q["any_of"])), None))
    return ranks


def report(label, ranks, n):
    found = [r for r in ranks if r]
    top3 = sum(1 for r in found if r <= 3)
    lo, hi = wilson(top3, n)
    print(f"{label:12s} found {len(found)/n:.2f}  top3 {top3/n:.2f} "
          f"[{lo:.2f},{hi:.2f}]  MRR {sum(1.0/r for r in found)/n:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--gold", required=True, action="append")
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--depth", type=int, default=100)
    ap.add_argument("--parents", type=int, default=50)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    golds = [json.loads(Path(g).read_text(encoding="utf-8")) for g in args.gold]
    pinfo = reg.get_by_path(golds[0]["project_path"])
    conn = sqlite3.connect(
        f"file:{Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)
    rows = conn.execute(
        "select id, node_type, content from chunks where project_id=? "
        f"and node_type in ({','.join('?'*len(PARENTS))})",
        (pinfo.id, *PARENTS)).fetchall()
    parent_text = {r[0]: r[2] or "" for r in rows}

    scratch = Path(args.scratch)
    built = {}
    for mode in ("claim", "sentence"):
        pof, n_units = build(scratch / f"abl_{mode}", rows, mode)
        built[mode] = pof
        print(f"{mode:9s} units {n_units:,}")
    print()

    for gold in golds:
        n = len(gold["queries"])
        print(f"=== set n={n} (depth {args.depth}) ===")
        for mode in ("claim", "sentence"):
            ranks = evaluate(scratch / f"abl_{mode}", built[mode], parent_text,
                             gold, args.depth, args.parents)
            report(mode, ranks, n)
        print()


if __name__ == "__main__":
    main()
