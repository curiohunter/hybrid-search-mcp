"""Retro-rescore stored selfeval events under v1.1 and v1.2 rules — read only.

Plan: docs/plans/2026-09-27-selfeval-v1.2.md §3. For each project it reads
``events.jsonl``, finds each row's turn again in the Claude transcripts, and
scores that turn twice: with the v1.1 rules (to check the replay reproduces
the stored verdict) and with v1.2 (shell reads + quoted adoption).

Nothing under the project is written. Output goes outside every repo:
``~/.hybrid-search/benchmarks/selfeval-rescore/<project>-<date>.json`` holds
the per-row detail (it quotes queries — never commit it); stdout prints only
counts.

    python benchmarks/selfeval_rescore.py --project . --project ../other
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from hybrid_search.hooks import _extract_user_text
from hybrid_search.index.transcript_source import claude_slug_for
from hybrid_search.memory import selfeval
from hybrid_search.memory.selfeval_evidence import (
    _legacy_is_search,
    is_quoted_path,
    shell_actions,
)

_OUT_DIR = Path.home() / ".hybrid-search" / "benchmarks" / "selfeval-rescore"
_QA_SECTION = re.compile(r"^### (\d+)\.\s+`([^`]+)`[^\n]*\n(.*?)(?=^### |\Z)", re.M | re.S)


# ── v1.1 rules, frozen for the comparison ─────────────────────────────


def _legacy_followups(name: str, ti: dict) -> tuple[list[str], list[str]]:
    if name == "Grep":
        return [], [(ti.get("pattern") or "").strip()]
    if name == "Bash":
        cmd = (ti.get("command") or "").strip()
        return [], ([cmd[:120]] if _legacy_is_search(cmd) else [])
    opened = selfeval._open_target(name, ti)
    return ([opened] if opened else []), []


@contextmanager
def _rules(version: str):
    """Swap the followup classifier; everything else is the shipped code."""
    if version == "v1.2":
        yield
        return
    shipped = selfeval._followups
    selfeval._followups = _legacy_followups
    try:
        yield
    finally:
        selfeval._followups = shipped


# ── Locating turns ───────────────────────────────────────────────────


def _transcript_dirs(project: Path) -> list[Path]:
    root = Path.home() / ".claude" / "projects"
    names = {claude_slug_for(project), str(project.resolve()).replace("/", "-")}
    return [root / n for n in sorted(names) if (root / n).is_dir()]


def _read_records(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _turn_index(project: Path) -> tuple[dict, dict]:
    """(turn_key → turn slice, normalized prompt → turn slice)."""
    by_key: dict[str, dict] = {}
    by_prompt: dict[str, dict] = {}
    for tdir in _transcript_dirs(project):
        for tpath in sorted(tdir.glob("*.jsonl")):
            records = _read_records(tpath)
            starts = [
                (i, _extract_user_text(r))
                for i, r in enumerate(records)
                if r.get("type") == "user" and _extract_user_text(r) is not None
            ]
            for n, (i, prompt) in enumerate(starts):
                end = starts[n + 1][0] if n + 1 < len(starts) else len(records)
                rec = records[i]
                session = rec.get("sessionId") or tpath.stem
                turn = {"prompt": prompt, "records": records[i + 1 : end]}
                tid = rec.get("promptId") or rec.get("uuid")
                by_key.setdefault(selfeval.turn_key(session, prompt, tid), turn)
                # The hook falls back to the prompt when it has no id.
                by_key.setdefault(selfeval.turn_key(session, prompt, None), turn)
                by_prompt.setdefault(selfeval._match_key(prompt), turn)
    return by_key, by_prompt


def _qa_served(project: Path, qa_record_id: str) -> dict | None:
    """Served paths (≤10) and quoted excerpts from the pre-fetch qa log."""
    if not qa_record_id:
        return None
    hits = list((project / ".hybrid-search" / "qa").rglob(f"{qa_record_id}.md"))
    if not hits:
        return None
    text = hits[0].read_text(encoding="utf-8", errors="replace")
    paths: list[str] = []
    quoted: list[dict] = []
    for m in _QA_SECTION.finditer(text):
        rank, path, body = int(m.group(1)), m.group(2), m.group(3)
        base = re.sub(r":\d+(-\d+)?$", "", path)
        if base not in paths:
            paths.append(base)
        if is_quoted_path(base):
            excerpt = " ".join(
                ln[1:].strip() for ln in body.splitlines() if ln.startswith(">")
            )
            if excerpt:
                quoted.append({"rank": rank, "excerpt": excerpt})
    return {"paths": paths[: selfeval._MAX_TRACKED_PATHS], "quoted": quoted}


# ── Scoring one row ──────────────────────────────────────────────────


def _is_external(path: str, project: Path) -> bool:
    """An absolute path outside the project (scratchpads, /tmp, tool output)."""
    return path.startswith(("/", "~")) and not path.startswith(str(project) + "/")


def _shell_components(records: list[dict], paths: list[str], project: Path) -> Counter:
    """Which shell commands changed meaning between v1.1 and v1.2."""
    c: Counter = Counter()
    for rec in records:
        if rec.get("type") != "assistant":
            continue
        for block in (rec.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("name") != "Bash":
                continue
            cmd = ((block.get("input") or {}).get("command") or "").strip()
            files, searches = shell_actions(cmd)
            legacy = _legacy_is_search(cmd)
            for f in files:
                in_results = any(selfeval._paths_match(f, p) for p in paths)
                key = "in_results" if in_results else (
                    "outside_external" if _is_external(f, project) else "outside"
                )
                c[f"shell_read_{key}{'_was_search' if legacy else '_was_silent'}"] += 1
            if searches:
                c["shell_search_kept" if legacy else "shell_search_new"] += 1
    return c


def _score(row: dict, turn: dict, served: dict | None, version: str) -> dict:
    with _rules(version):
        if (row.get("source") or "tool") == "prefetch":
            pending = {
                "query": row.get("query") or turn["prompt"],
                "top_paths": (served or {}).get("paths") or row.get("top_paths") or [],
                "quoted": (served or {}).get("quoted") or [],
            }
            event = selfeval.extract_prefetch_event(turn["records"], pending)
            if not event["excluded"].startswith(turn["prompt"]):
                event["excluded"] = turn["prompt"] + "\n" + event["excluded"]
        else:
            events = selfeval.extract_turn_events(turn["records"])
            seq = row.get("turn_seq", 0)
            if seq >= len(events):
                return {}
            event = events[seq]
            event["excluded"] = turn["prompt"] + "\n" + event["excluded"]
        scored = selfeval.score_event(event)
        scored["_event_paths"] = event["paths"]
        return scored


def rescore(project: Path) -> dict:
    events_path = project / ".hybrid-search" / "selfeval" / "events.jsonl"
    rows = selfeval._dedup_rows(selfeval._read_jsonl(events_path))
    by_key, by_prompt = _turn_index(project)

    out_rows = []
    for row in rows:
        lane = row.get("source") or "tool"
        turn = by_key.get(row.get("turn_key") or "")
        if turn is None and lane == "prefetch":
            turn = by_prompt.get(selfeval._match_key(row.get("query") or ""))
        detail = {"lane": lane, "stored": row.get("verdict"), "located": turn is not None}
        if turn is not None:
            served = (
                _qa_served(project, row.get("qa_record_id") or "")
                if lane == "prefetch" else None
            )
            old = _score(row, turn, served, "v1.1")
            new = _score(row, turn, served, "v1.2")
            if old and new:
                detail.update(
                    old=old["verdict"], new=new["verdict"],
                    quoted_served=new["quoted_served"],
                    quoted_adopted_rank=new["quoted_adopted_rank"],
                    components=dict(
                        _shell_components(turn["records"], new["_event_paths"], project)
                    ),
                    query=(row.get("query") or "")[:200],
                    turn_prompt=turn["prompt"][:200],
                )
            else:
                detail["located"] = False
        out_rows.append(detail)
    return {"project": project.name, "rows": out_rows, "summary": _summarize(out_rows)}


def _summarize(rows: list[dict]) -> dict:
    summary: dict = {}
    for lane in ("tool", "prefetch"):
        lane_rows = [r for r in rows if r["lane"] == lane]
        located = [r for r in lane_rows if r["located"]]
        comps: Counter = Counter()
        for r in located:
            comps.update(r.get("components") or {})
        summary[lane] = {
            "rows": len(lane_rows),
            "located": len(located),
            "stored": dict(Counter(r["stored"] for r in lane_rows)),
            "stored_located": dict(Counter(r["stored"] for r in located)),
            "v1_1": dict(Counter(r["old"] for r in located)),
            "v1_2": dict(Counter(r["new"] for r in located)),
            "fidelity": (
                round(sum(r["old"] == r["stored"] for r in located) / len(located), 3)
                if located else None
            ),
            "transitions": dict(Counter(f"{r['old']}->{r['new']}" for r in located)),
            "quoted_served_rows": sum(1 for r in located if r.get("quoted_served")),
            "quoted_adopted_rows": sum(
                1 for r in located if r.get("quoted_adopted_rank") is not None
            ),
            "shell_components": dict(comps),
        }
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", action="append", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=_OUT_DIR)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for project in args.project:
        project = project.resolve()
        result = rescore(project)
        dest = args.out / f"{project.name}-{date.today().isoformat()}.json"
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"== {project.name} → {dest}")
        print(json.dumps(result["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
