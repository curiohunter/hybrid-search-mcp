"""All-pairs topic-matcher audit over a directory of qa notes.

Point --dir at a project's .hybrid-search/qa/consolidated (or any
directory of qa markdown). Nothing about the corpus is committed here —
this is the runner; what you measure stays with the project measured.

Prints every pair the matcher calls the same topic, under both the
query-time predicate (qa_topics.same_topic) and the index-time strict
one (supersession._same_topic_strict), with the scores that decided it.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hybrid_search.memory import supersession
from hybrid_search.search import qa_topics


def load(path: pathlib.Path):
    content = path.read_text(encoding="utf-8", errors="replace")
    query = supersession._frontmatter_value(content, "query") or ""
    item = supersession._topic_item(content)
    return {"path": path, "id": path.stem, "query": query, "item": item}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    notes = [load(p) for p in sorted(pathlib.Path(args.dir).glob("*.md"))]
    print(f"notes: {len(notes)}  pairs: {len(notes)*(len(notes)-1)//2}\n")

    rows = []
    for a, b in itertools.combinations(notes, 2):
        qa_, ab_ = a["item"], b["item"]
        lenient = qa_topics.same_topic(qa_, ab_)
        strict = supersession._same_topic_strict(qa_, ab_)
        if not (lenient or strict):
            continue
        q_ov = qa_topics.weighted_overlap(qa_[0], ab_[0])
        a_ov = qa_topics.weighted_overlap(qa_[1], ab_[1])
        shared_q = sorted(
            t for t in qa_[0].keys() & ab_[0].keys()
            if min(qa_[0][t], ab_[0][t]) >= qa_topics._W_NORMAL
        )
        union_a = {**qa_[1], **qa_[0]}
        union_b = {**ab_[1], **ab_[0]}
        shared_union = sorted(
            t for t in union_a.keys() & union_b.keys()
            if min(union_a[t], union_b[t]) >= qa_topics._W_NORMAL
        )
        rows.append({
            "a": a["id"], "b": b["id"],
            "lenient": lenient, "strict": strict,
            "q_ov": round(q_ov, 3), "a_ov": round(a_ov, 3),
            "shared_q": shared_q,
            "shared_union_n": len(shared_union),
            "shared_union": shared_union[:25],
            "qa": a["query"], "qb": b["query"],
        })

    rows.sort(key=lambda r: (-r["strict"], -r["a_ov"]))
    for r in rows:
        tag = []
        if r["strict"]:
            tag.append("STRICT(index)")
        if r["lenient"]:
            tag.append("lenient(query)")
        print(f"=== {'+'.join(tag)}  q_ov={r['q_ov']} a_ov={r['a_ov']}")
        print(f"  A {r['a']}: {r['qa'][:110]}")
        print(f"  B {r['b']}: {r['qb'][:110]}")
        print(f"  shared_q(distinctive)={r['shared_q']}")
        print(f"  shared_union(distinctive, n={r['shared_union_n']})={r['shared_union']}")
        print()

    print(f"total flagged pairs: {len(rows)}  "
          f"(strict {sum(r['strict'] for r in rows)}, "
          f"lenient {sum(r['lenient'] for r in rows)})")

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
