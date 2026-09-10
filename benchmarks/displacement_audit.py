"""What did retrieval REMOVE? — the failure mode no other bench can see.

Every benchmark in this directory asks "did the answer come back". None of
them asks "did the pipeline delete an answer it already had", and that is
the failure that has hurt this project most:

  · 2026-09-09 §12 — a consolidated note superseded another consolidated
    note, and at full capacity the splice REPLACED the note that answered.
  · 2026-09-09 §8 — a shell-command paste superseded a real answer on
    nothing but the project's own name. The answer left the results.

Both were found by hand, both were invisible to Set A/Set B/the code axis,
and one of them survived 209 wrong mappings being deleted without moving a
single gold rank. A 20-question gold set cannot cover a failure that can
strike any of 2,000 records — and writing 20 more questions does not fix
that, because the next instance will land somewhere else.

So this audit does not use a gold set at all. Two ideas replace it:

**The corpus is its own probe set.** Every qa record carries the question
it answered. Ask it back. If a record cannot be found by its OWN question,
that is a defect with no labelling required, and there are as many probes
as there are records.

**Damage is a diff, not a score.** Run each probe twice — once with the
index-time supersession map in place, once with it emptied — and compare
the result sets. Anything present without the map and missing with it was
DISPLACED by a correction. Displacement is by design; losing the query's
own words in the process is not. A displacement where the replacement no
longer carries the query terms the displaced chunk matched is reported as
suspected damage, with both texts, for a human to judge.

Nothing here is committed but the runner: the probes are the measured
project's own turns (see CLAUDE.md). Run it against a FROZEN snapshot —
the audit empties the supersession table for its second pass and restores
it afterwards, which you do not want to do to a live index.

    python benchmarks/displacement_audit.py --config $SNAP/config.toml \
        --project <name> --sample 150 --out /tmp/displacement.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.memory import supersession as ss  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search import qa_topics as topics  # noqa: E402
from hybrid_search.search.orchestrator import SearchOrchestrator  # noqa: E402
from hybrid_search.storage.db import StoreDB  # noqa: E402
from hybrid_search.storage.indexes import IndexPaths, get_project_dir  # noqa: E402


def _query_terms(text: str) -> set[str]:
    """Distinctive tokens of a query — the words whose disappearance from
    the results is worth reporting. Reuses the topic matcher's own
    normalization so "matched the query" means the same thing here as it
    does everywhere else in the pipeline."""
    return {t for t, w in topics.topic_tokens(text).items() if w >= topics._W_NORMAL}


def _probe_set(db: StoreDB, project_id: str, sample: int, seed: int):
    """qa records that carry an answer, with the question they answered.

    Records with no ``## Answer excerpt`` are skipped: they are not what a
    probe should be able to find, so a miss would say nothing.
    """
    probes = []
    for chunk in db.get_chunks_by_node_type(project_id, "qa_log"):
        content = chunk.content or ""
        if ss._is_machine_payload(content):
            continue
        question = ss._frontmatter_value(content, "query") or ""
        if len(question) < 8:
            continue
        if not ss._topic_item(content)[1]:
            continue
        probes.append((chunk.id, question, content))
    rng = random.Random(seed)
    rng.shuffle(probes)
    return probes[:sample]


def _run_pass(orch, project_path: str, probes, limit: int) -> dict[str, list]:
    """{probe chunk_id: [(result chunk_id, content)]}"""
    out = {}
    for i, (cid, question, _content) in enumerate(probes, 1):
        resp = orch.hybrid_search(query=question, cwd=project_path, limit=limit)
        out[cid] = [(r.chunk_id, r.content or "") for r in resp.results]
        if i % 25 == 0:
            print(f"    … {i}/{len(probes)}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # The overlays read the working tree and the running session, so leaving
    # them on makes the audit depend on what another session is doing.
    os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")

    config = load_config(Path(args.config))
    registry = ProjectRegistry(config.global_dir)
    pinfo = registry.get_by_name(args.project)
    if pinfo is None:
        print(f"{args.project}: not registered")
        return 1
    store_path = IndexPaths(get_project_dir(config.projects_dir, pinfo.id)).store_db
    db = StoreDB(store_path)
    probes = _probe_set(db, pinfo.id, args.sample, args.seed)
    saved = db.get_qa_superseding([c.id for c in
                                   db.get_chunks_by_node_type(pinfo.id, "qa_log")])
    db.close()
    if not probes:
        print("no answerable qa records to probe")
        return 1

    print(f"probes: {len(probes)} qa records (their own questions) · "
          f"supersession map: {len(saved)} entries")

    embedder = Embedder(config.embedding, config.models_dir)
    orch = SearchOrchestrator(config=config, registry=registry, embedder=embedder)

    print("  pass 1/2 — supersession map ON")
    with_map = _run_pass(orch, pinfo.path, probes, args.limit)

    # Pass 2 needs the map gone. The snapshot is a copy, and the table is
    # rebuilt from the qa content by benchmarks/recompute_supersession.py,
    # so this is reversible — but never point this at a live index.
    db = StoreDB(store_path)
    try:
        with db.transaction() as conn:
            conn.execute("DELETE FROM qa_supersession WHERE project_id = ?",
                         (pinfo.id,))
    finally:
        db.close()
    try:
        print("  pass 2/2 — supersession map OFF")
        orch2 = SearchOrchestrator(config=config, registry=registry, embedder=embedder)
        without_map = _run_pass(orch2, pinfo.path, probes, args.limit)
    finally:
        db = StoreDB(store_path)
        try:
            with db.transaction() as conn:
                db.replace_qa_supersession(conn, pinfo.id, saved)
        finally:
            db.close()
        print("  supersession map restored")

    rows = []
    for cid, question, content in probes:
        on = with_map.get(cid, [])
        off = without_map.get(cid, [])
        on_ids = {c for c, _ in on}
        terms = _query_terms(question)
        displaced = [(c, text) for c, text in off if c not in on_ids]
        row = {
            "probe": cid,
            "query": question[:200],
            "self_found_on": cid in on_ids,
            "self_found_off": cid in {c for c, _ in off},
            "displaced": [],
        }
        for dis_id, dis_text in displaced:
            dis_terms = terms & _query_terms(dis_text)
            if not dis_terms:
                continue  # it never matched the question; losing it is no loss
            # Two ways a chunk can be in one pass and not the other, and
            # only one of them is this audit's subject:
            #
            #   · the map named it superseded, so the splice REPLACED it
            #     — the destructive path, and `saved` proves it;
            #   · it sat at the edge of the limit and a rank wobble pushed
            #     it out — noise, and calling that damage would drown the
            #     signal in it.
            by_map = dis_id in saved
            # What took its place? Everything the ON pass has that the OFF
            # pass did not — the splice's insertions.
            added = [(c, t) for c, t in on if c not in {x for x, _ in off}]
            kept = set()
            for _c, t in added:
                kept |= _query_terms(t)
            lost = dis_terms - kept
            row["displaced"].append({
                "chunk": dis_id,
                "by_map": by_map,
                "matched_terms": sorted(dis_terms),
                "terms_lost_by_replacement": sorted(lost),
                "suspected_damage": bool(lost) and by_map,
                "displaced_text": dis_text[:400],
                "replacement_text": (added[0][1][:400] if added else ""),
            })
        rows.append(row)

    n = len(rows)
    self_on = sum(r["self_found_on"] for r in rows)
    self_off = sum(r["self_found_off"] for r in rows)
    disp_rows = [r for r in rows if any(d["by_map"] for d in r["displaced"])]
    damage_rows = [r for r in rows
                   if any(d["suspected_damage"] for d in r["displaced"])]
    wobble_rows = [r for r in rows
                   if r["displaced"] and not any(d["by_map"] for d in r["displaced"])]
    print()
    print("=== self-retrieval (can a record be found by its own question?) ===")
    print(f"  map ON  : {self_on}/{n}  ({self_on / n:.0%})")
    print(f"  map OFF : {self_off}/{n}  ({self_off / n:.0%})")
    if self_off > self_on:
        print(f"  ⚠ the supersession map COSTS {self_off - self_on} self-retrievals")
    print("=== displacement (matched the query, then the map removed it) ===")
    print(f"  probes losing a match TO THE MAP : {len(disp_rows)}/{n}"
          f"  ({len(disp_rows) / n:.0%})")
    print(f"  … where the replacement dropped those query terms : "
          f"{len(damage_rows)}  ({len(damage_rows) / n:.0%})  ← suspected damage")
    print(f"  (rank wobble at the limit, not the map : {len(wobble_rows)} — "
          f"reported, not counted)")
    print()
    print("suspected-damage examples are in the report; read them, do not "
          "trust the count alone.")

    Path(args.out).write_text(
        json.dumps({
            "project": args.project,
            "probes": n,
            "self_found_with_map": self_on,
            "self_found_without_map": self_off,
            "probes_with_displacement": len(disp_rows),
            "probes_with_suspected_damage": len(damage_rows),
            "probes_with_rank_wobble_only": len(wobble_rows),
            "rows": rows,
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
