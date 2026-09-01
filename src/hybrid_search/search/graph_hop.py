"""Graph-hop candidate expansion (WS2) — the LARGER pattern, locally.

Multi-hop code questions ("who calls this", "what does the flow touch")
fail on lexical+vector retrieval when the connecting file shares no
vocabulary with the query. LARGER (arXiv 2605.16352) showed the cheap
fix: don't build a graph index or search the graph — take the lexical
hits you already trust and pull their 1-hop call-graph neighbors into
the candidate pool, then let normal fusion rank everything.

We already store call edges (delta reindex keeps them fresh — verified
2026-09-01) and already read them for graph_card. This module reuses
both. Design constraints from the master plan:

- Neighbors are ordinary candidates with a derived score, not a lane
  with its own budget — fusion decides the final order (RANGER: graphs
  complement retrieval, they don't replace it).
- Reliability filtering reuses the edge-confidence ladder already
  calibrated for graph_card (``inferred`` and up). No new thresholds.
- Hard caps: expand from the top ``_EXPAND_FROM_TOP`` code hits, at most
  ``_NEIGHBORS_PER_HIT`` neighbors each — a hub function must not flood
  the pool.
"""

from __future__ import annotations

# Expand from this many of the best already-ranked code hits.
_EXPAND_FROM_TOP = 5
# Per-hit neighbor cap: callers + callees combined, best edge confidence
# first. Keeps hub nodes (a logger called by 200 sites) from flooding.
_NEIGHBORS_PER_HIT = 3
# A neighbor enters at half its source's RRF score: below the hit that
# vouched for it, above the deep tail. Scores stay on the RRF scale so
# downstream boosts/caps treat neighbors like any other row.
_SCORE_DECAY = 0.5

# Node types that can own call edges. Everything else (docs, memory,
# module cards) would just issue empty lookups.
_EXPANDABLE_NODE_TYPES = frozenset(
    {"function", "method", "class", "interface", "merged", "variable", "block"}
)

# Marker appended to a neighbor's trust_meta / snippet header so agents
# (and the bench) can see the row arrived by graph, not by retrieval.
GRAPH_HOP_NOTE = "graph 1-hop"


def collect_neighbor_ids(
    db,
    project_id: str,
    hits: list,
    *,
    exclude_ids: set[str],
    total_cap: int,
) -> list[tuple[str, float]]:
    """(chunk_id, derived_score) pairs for 1-hop neighbors of top hits.

    ``hits`` is the enriched, score-ordered chunk stream. Lookup failures
    degrade to no expansion — never break the search on its enhancement.
    """
    pairs: list[tuple[str, float]] = []
    claimed: set[str] = set(exclude_ids)
    expanded = 0
    for hit in hits:
        if expanded >= _EXPAND_FROM_TOP or len(pairs) >= total_cap:
            break
        if getattr(hit, "node_type", None) not in _EXPANDABLE_NODE_TYPES:
            continue
        expanded += 1
        try:
            callers = db.get_callers(hit.chunk_id, project_id, min_confidence="inferred")
            callees = db.get_callees(hit.chunk_id, project_id, min_confidence="inferred")
        except Exception:
            continue
        neighbors: list[tuple[float, str]] = []
        for row in callers:
            cid = row.get("caller_chunk_id")
            if cid:
                neighbors.append((row.get("confidence_score") or 0.0, cid))
        for row in callees:
            cid = row.get("callee_chunk_id")
            if cid:
                neighbors.append((row.get("confidence_score") or 0.0, cid))
        neighbors.sort(reverse=True)
        taken = 0
        for _, cid in neighbors:
            if taken >= _NEIGHBORS_PER_HIT or len(pairs) >= total_cap:
                break
            if cid in claimed:
                continue
            claimed.add(cid)
            pairs.append((cid, hit.rrf_score * _SCORE_DECAY))
            taken += 1
    return pairs


def merge_by_score(existing: list, additions: list) -> list:
    """Insert ``additions`` by rrf_score without reordering ``existing``.

    The chunk stream's order is not strictly score-descending (in-flight
    overlay, earlier splices), so a full re-sort would disturb rows that
    other stages positioned deliberately. Each addition lands before the
    first existing row it outscores; ties keep existing rows first.
    """
    out = list(existing)
    for add in sorted(additions, key=lambda r: -r.rrf_score):
        at = len(out)
        for i, r in enumerate(out):
            if r.rrf_score < add.rrf_score:
                at = i
                break
        out = [*out[:at], add, *out[at:]]
    return out
