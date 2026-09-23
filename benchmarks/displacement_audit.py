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


def label_for(labels: dict, chunk: str, successor: str | None) -> dict | None:
    """The label that still describes (chunk, successor), else None.

    A label judged one pair. Labels written before 2026-09-23 did not pin
    their successor and are trusted as before; a pinned one whose successor
    has since changed is stale — it would put a verdict on a pair nobody
    judged (found when a hand label's rationale described a successor the
    map no longer named).
    """
    label = labels.get(chunk)
    if not label:
        return None
    pinned = label.get("successor")
    if pinned and successor and pinned != successor:
        return None
    return label


def _owned_text(record: str) -> str:
    """The part of a qa record that is the record's OWN.

    Its question and its answer excerpt. The `## Top results` block below
    them is a dump of what retrieval showed at the time — a quotation of
    other chunks, each retrievable on its own terms — so a replacement
    that drops those words has destroyed nothing.

    Counting them destroyed the audit's own signal (2026-09-11): of the
    suspected-damage cases across two corpora, five were bare pre-fetch
    logs whose entire match lived in that dump, and every one of them was
    labelled legitimate by hand. A detector whose positives are mostly
    quotation noise cannot be read without the labels it was meant to
    save.
    """
    head = record.split("## Top results", 1)[0]
    return head


def _probe_set(db: StoreDB, project_id: str, sample: int, seed: int,
               since: str | None = None):
    """qa records that carry an answer, with the question they answered.

    Records with no ``## Answer excerpt`` are skipped: they are not what a
    probe should be able to find, so a miss would say nothing.

    ``since`` (ISO date) keeps only records written after it. That is what
    makes a repeated run a HOLDOUT rather than a re-read: the rules in
    effect were frozen before those records existed, so they cannot have
    been fitted to them. One corpus, one user, but a clean test set that
    refills itself every week.
    """
    probes = []
    for chunk in db.get_chunks_by_node_type(project_id, "qa_log"):
        content = chunk.content or ""
        if ss._is_machine_payload(content):
            continue
        if since:
            ts = ss._frontmatter_value(content, "timestamp") or ""
            if ts[:10] < since:
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
    ap.add_argument(
        "--since",
        help="ISO date; probe only qa records written on or after it. Turns a "
             "repeat run into a holdout — those records did not exist when the "
             "rules were frozen.",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--labels",
        help="JSON of hand-labelled displacements ({labels: {chunk_id: "
             "{verdict: legitimate|damage}}}), which turns the two rates "
             "below into an accuracy. Keep it OUTSIDE the repo — the "
             "rationales identify real turns (CLAUDE.md).",
    )
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
    probes = _probe_set(db, pinfo.id, args.sample, args.seed, args.since)
    saved = db.get_qa_superseding([c.id for c in
                                   db.get_chunks_by_node_type(pinfo.id, "qa_log")])
    db.close()
    if not probes:
        print("no answerable qa records to probe")
        return 1

    print(f"probes: {len(probes)} qa records (their own questions) · "
          f"supersession map: {len(saved)} entries"
          + (f" · holdout since {args.since}" if args.since else ""))

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

    labels = {}
    if args.labels:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))["labels"]

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
            # What took its place? Not "whatever the ON pass added" — the
            # map says exactly which chunk supersedes this one, and using
            # the first insertion instead pairs the wrong texts. That
            # mis-pairing showed up as replacements OLDER than what they
            # replaced, which supersession cannot produce, and it was only
            # visible because someone laid the cases out in a table
            # (2026-09-10). Read the report, not just the counts.
            added = [(c, t) for c, t in on if c not in {x for x, _ in off}]
            successor = saved.get(dis_id)
            paired = next((t for c, t in added if c == successor), None)
            if paired is None:
                paired = added[0][1] if added else ""
            # Damage is judged on what the displaced record OWNED. It
            # matched the query (`dis_terms`, above, over the whole
            # record), but only its own question and answer can be
            # destroyed by a replacement — see `_owned_text`.
            owned = dis_terms & _query_terms(_owned_text(dis_text))
            kept = _query_terms(_owned_text(paired))
            lost = owned - kept
            row["displaced"].append({
                "chunk": dis_id,
                "by_map": by_map,
                "label": (label_for(labels, dis_id, successor) or {}).get("verdict"),
                "matched_terms": sorted(dis_terms),
                "owned_matched_terms": sorted(owned),
                "terms_lost_by_replacement": sorted(lost),
                "suspected_damage": bool(lost) and by_map,
                "displaced_text": dis_text[:400],
                "replacement_chunk": successor,
                "replacement_text": paired[:400],
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
    if labels:
        # With labels the two rates above become one number: of the
        # displacements the map caused, how many destroyed an answer.
        seen = {d["chunk"]: d["label"]
                for r in rows for d in r["displaced"] if d["by_map"]}
        known = {c: v for c, v in seen.items() if v}
        dmg = sum(1 for v in known.values() if v == "damage")
        print("=== labelled accuracy (the two rates above, resolved) ===")
        print(f"  displacements labelled : {len(known)}/{len(seen)}")
        if known:
            print(f"  legitimate  : {len(known) - dmg}  "
                  f"({(len(known) - dmg) / len(known):.0%}) ← the feature working")
            print(f"  damage      : {dmg}  ({dmg / len(known):.0%}) ← an answer deleted")
        if len(known) < len(seen):
            print("  unlabelled displacements are in the report — label them "
                  "before trusting a comparison")
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
