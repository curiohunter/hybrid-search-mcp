"""What did the map STOP correcting? — the other half of the audit.

benchmarks/displacement_audit.py measures precision: of the records the
supersession map removes from results, how many deserved it. Driving that
to 100% is easy in the wrong way — map nothing and nothing is destroyed.
This is the counterweight: of the corrections the map COULD make, how
many do the current bars refuse, and were they real?

The comparison is the current map against the same algorithm with the
index-time bars disabled (`_MIN_QUESTION_MASS`,
`_MIN_SYMMETRIC_QUESTION_OVERLAP`). Everything the
bars cut is listed with the bar that cut it, so an over-tight bar is
visible as a bar, not as an aggregate.

Labels are the same ones the displacement audit uses, under the same
question: does the successor answer substantially the same question as
the record it would have replaced? A cut `legitimate` mapping is a
correction lost — recall damage. A cut `damage` mapping is the bars doing
their job. Keep the label file OUTSIDE the repo: its rationales identify
real turns (CLAUDE.md).

    # 1. list what the bars cut, sampled, with full text to read
    python benchmarks/supersession_recall.py --config $SNAP/config.toml \
        --project <name> --sample 24 --out /tmp/cut.json

    # 2. label them, then score
    python benchmarks/supersession_recall.py --config $SNAP/config.toml \
        --project <name> --labels ~/.hybrid-search/benchmarks/<name>_cut_labels.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_search import clock  # noqa: E402
from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.memory.qa_shape import answer_excerpt  # noqa: E402
from hybrid_search.memory import supersession as ss  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search import qa_topics as topics  # noqa: E402
from hybrid_search.storage.db import StoreDB  # noqa: E402
from hybrid_search.storage.indexes import IndexPaths, get_project_dir  # noqa: E402

_BARS = ("_MIN_QUESTION_MASS", "_MIN_SYMMETRIC_QUESTION_OVERLAP")


def _which_bar(a, b) -> str:
    """The first index-time bar that rejects this pair, or "other".

    Checked in the order `_same_topic_strict` applies them, so the answer
    is the bar a reader would blame.
    """
    answerless = not (a[1] and b[1])
    if answerless:
        if min(sum(a[0].values()), sum(b[0].values())) < ss._MIN_QUESTION_MASS:
            return "mass (answer-less)"
        return "other (answer-less path)"
    query_thr = topics._active_thresholds()[0]
    if topics.weighted_overlap(a[0], b[0]) < query_thr:
        return "question overlap"
    if ss._symmetric_question_overlap(a[0], b[0]) < ss._MIN_SYMMETRIC_QUESTION_OVERLAP:
        return "symmetric overlap"
    return "other"


def _parts(content: str) -> tuple[str, str, str]:
    q = re.search(r'query:\s*"([^"]*)', content or "")
    ts = re.search(r"timestamp:\s*(\S{10})", content or "")
    answer = answer_excerpt(content)
    return (q.group(1) if q else "")[:260], (ts.group(1) if ts else "?"), answer[:420]


def _probe_compare(config, registry, pinfo, current, loose, chunks,
                   sample: int, seed: int, labels: dict) -> None:
    """Does the recall the bars cut ever reach a result list?

    A mapping the bars refuse costs nothing unless a search actually
    surfaces the record it would have corrected. Both maps are written
    into the (snapshot!) store in turn and the same probes run against
    each; a probe "loses a correction" when the loose map splices in a
    successor the current map does not.
    """
    import os

    os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")
    from hybrid_search.index.embedder import Embedder
    from hybrid_search.search.orchestrator import SearchOrchestrator

    store = IndexPaths(get_project_dir(config.projects_dir, pinfo.id)).store_db
    rng = random.Random(seed)
    probes = [(c.id, ss._frontmatter_value(c.content or "", "query") or "")
              for c in chunks.values()
              if not ss._is_machine_payload(c.content or "")
              and len(ss._frontmatter_value(c.content or "", "query") or "") >= 8]
    rng.shuffle(probes)
    probes = probes[:sample]
    embedder = Embedder(config.embedding, config.models_dir)

    def run(mapping):
        db = StoreDB(store)
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, pinfo.id, mapping)
        finally:
            db.close()
        orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
        out = {}
        for cid, question in probes:
            resp = orch.hybrid_search(query=question, cwd=pinfo.path, limit=10)
            out[cid] = {r.chunk_id for r in resp.results}
        return out

    print(f"\n=== probe-level recall ({len(probes)} probes, both maps) ===")
    try:
        cur_res = run(current)
        loose_res = run(loose)
    finally:
        db = StoreDB(store)
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, pinfo.id, current)
        finally:
            db.close()
        print("  current map restored")

    gained, gained_legit = 0, 0
    for cid, _q in probes:
        extra = loose_res[cid] - cur_res[cid]
        # Only successors the LOOSE map would have spliced count as recall.
        spliced = {v for k, v in loose.items() if v in extra and current.get(k) != v}
        if not spliced:
            continue
        gained += 1
        keys = [k for k, v in loose.items() if v in spliced and current.get(k) != v]
        if any((labels.get(k) or {}).get("verdict") == "legitimate" for k in keys):
            gained_legit += 1
    print(f"  probes where the loose map surfaces a correction the current one "
          f"does not: {gained}/{len(probes)} ({gained / len(probes):.0%})")
    if labels:
        print(f"  … of those, the correction is a LABELLED real one: {gained_legit}")
        print("  (unlabelled ones are neither counted nor dismissed — label them)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--sample", type=int, default=24,
                    help="cut mappings to dump for reading, stratified by bar")
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--labels")
    ap.add_argument("--out")
    ap.add_argument(
        "--probe", type=int, default=0,
        help="also run N probes (a record's own question) under both maps and "
             "count where the loose one surfaces a correction the current one "
             "does not — map-level recall only matters if it reaches results",
    )
    args = ap.parse_args()

    clock.pin_to_snapshot(args.config)
    config = load_config(Path(args.config))
    registry = ProjectRegistry(config.global_dir)
    pinfo = registry.get_by_name(args.project)
    if pinfo is None:
        print(f"{args.project}: not registered")
        return 1
    db = StoreDB(IndexPaths(get_project_dir(config.projects_dir, pinfo.id)).store_db)
    chunks = {c.id: c for c in db.get_chunks_by_node_type(pinfo.id, "qa_log")}
    entries = [(c.id, c.content or "") for c in chunks.values()]

    current = ss.compute_supersession(entries, project_name=args.project)
    saved = tuple(getattr(ss, name) for name in _BARS)
    for name in _BARS:
        setattr(ss, name, 0.0)
    try:
        loose = ss.compute_supersession(entries, project_name=args.project)
    finally:
        for name, value in zip(_BARS, saved):
            setattr(ss, name, value)
    db.close()

    demote = topics.project_identity_tokens(args.project)
    cut = []
    for old_id, new_id in loose.items():
        if current.get(old_id) == new_id:
            continue
        a = ss._topic_item(chunks[old_id].content or "", demote)
        b = ss._topic_item(chunks[new_id].content or "", demote)
        cut.append({
            "superseded": old_id,
            "would_be_superseded_by": new_id,
            "bar": _which_bar(a, b),
            "retargeted_to": current.get(old_id),
        })

    by_bar: dict[str, list] = {}
    for row in cut:
        by_bar.setdefault(row["bar"], []).append(row)

    print(f"map: {len(current)} mappings · without the index-time bars: {len(loose)}")
    print(f"cut by the bars: {len(cut)}")
    for bar, rows in sorted(by_bar.items(), key=lambda kv: -len(kv[1])):
        print(f"  {bar:26s} {len(rows):4d}")

    labels = {}
    if args.labels:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))["labels"]
        print()
        print("=== labelled recall (of the corrections the bars refuse) ===")
        for bar, rows in sorted(by_bar.items(), key=lambda kv: -len(kv[1])):
            known = [labels[r["superseded"]]["verdict"] for r in rows
                     if r["superseded"] in labels]
            if not known:
                print(f"  {bar:26s} unlabelled")
                continue
            legit = sum(1 for v in known if v == "legitimate")
            print(f"  {bar:26s} labelled {len(known):3d}  "
                  f"real corrections lost: {legit} ({legit / len(known):.0%})")
        allknown = [labels[r["superseded"]]["verdict"] for r in cut
                    if r["superseded"] in labels]
        if allknown:
            legit = sum(1 for v in allknown if v == "legitimate")
            print(f"  {'TOTAL':26s} labelled {len(allknown):3d}  "
                  f"real corrections lost: {legit} ({legit / len(allknown):.0%})")

    if args.probe:
        _probe_compare(config, registry, pinfo, current, loose, chunks,
                       args.probe, args.seed, labels)

    if args.out:
        rng = random.Random(args.seed)
        dump = []
        # Stratify: every bar gets read, not just the biggest one.
        per_bar = max(1, args.sample // max(1, len(by_bar)))
        for bar, rows in by_bar.items():
            for row in rng.sample(rows, min(per_bar, len(rows))):
                oq, ots, oa = _parts(chunks[row["superseded"]].content or "")
                nq, nts, na = _parts(
                    chunks[row["would_be_superseded_by"]].content or "")
                dump.append({**row,
                             "superseded_query": oq, "superseded_ts": ots,
                             "superseded_answer": oa,
                             "successor_query": nq, "successor_ts": nts,
                             "successor_answer": na})
        Path(args.out).write_text(
            json.dumps({"project": args.project, "cut_total": len(cut),
                        "by_bar": {k: len(v) for k, v in by_bar.items()},
                        "sample": dump}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        print(f"\nsample of {len(dump)} for reading: {args.out}")
        print("read them, label by `superseded` chunk id, then re-run with --labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
