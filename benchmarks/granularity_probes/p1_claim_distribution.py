"""P1 — what does the structure-aware splitter actually produce?

The plan claimed "5~8 claims per turn". That number was invented. This measures
it: claims per parent (median / p90 / max), characters per claim, the share of
parents that yield nothing, and how much the conversation quality gate removes
afterwards. The plan's decision rule: a median at or above 15 means the
structure split is no different from splitting on punctuation, and the "a
quarter as many children" cost argument is withdrawn.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics as st
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.claim_split import body_for_claims, split_claims  # noqa: E402
from hybrid_search.memory import quality  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--node-types", default="conv_turn")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    reg = ProjectRegistry(cfg.global_dir)
    pinfo = reg.get_by_path(args.project)
    conn = sqlite3.connect(
        f"file:{Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)

    for node_type in args.node_types.split(","):
        rows = [r[0] or "" for r in conn.execute(
            "select content from chunks where project_id=? and node_type=?",
            (pinfo.id, node_type))]
        if not rows:
            print(f"\n=== {node_type}: no chunks")
            continue
        random.seed(args.seed)
        sample = random.sample(rows, min(args.sample, len(rows)))

        counts, lengths, empty, kinds = [], [], 0, Counter()
        gated = 0
        for text in sample:
            claims = split_claims(body_for_claims(text, node_type))
            # Order fixed by the plan: evidence stripped inside the splitter,
            # then claims, then the quality gate.
            kept = [c for c in claims if not quality.is_harness_noise(c.text)]
            gated += len(claims) - len(kept)
            if not kept:
                empty += 1
            counts.append(len(kept))
            lengths.extend(len(c.text) for c in kept)
            kinds.update(c.kind for c in kept)

        total = sum(counts)
        print(f"\n=== {node_type}  (sample {len(sample)} of {len(rows)} chunks)")
        print(f"claims/chunk   median {st.median(counts):.0f} · "
              f"p90 {sorted(counts)[int(0.9*len(counts))-1]} · max {max(counts)} · "
              f"mean {total/len(counts):.1f}")
        if lengths:
            print(f"chars/claim    median {st.median(lengths):.0f} · "
                  f"p90 {sorted(lengths)[int(0.9*len(lengths))-1]}")
        print(f"empty chunks   {empty}/{len(sample)} ({empty/len(sample):.0%})")
        print(f"gate removed   {gated} claims ({gated/max(total+gated,1):.1%})")
        print("kinds          " + " ".join(f"{k}={v}" for k, v in kinds.most_common()))
        print(f"projected total for this node_type: "
              f"{total/len(sample)*len(rows):,.0f} claims from {len(rows):,} chunks")


if __name__ == "__main__":
    main()
