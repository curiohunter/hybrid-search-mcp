"""Did the turn use what the pre-fetch injected? — cases for a judge, and the tally.

selfeval calls a pre-fetch turn ``no_followup`` when the agent opened no file
and ran no search afterwards. That is most turns, and it says nothing: the
injected snippets may have been the answer, or may have gone unread. This
runner turns those turns into cases a judge can read.

The transcript keeps everything needed. The injected block is an
``attachment`` record (``hook_additional_context`` from ``UserPromptSubmit``)
right after the prompt, and the turn's answer follows it.

Three kinds of case, shuffled so the judge cannot tell them apart:

- ``N`` — no_followup turns: the question being asked.
- ``P`` — turns that opened a served file: the judge should say yes.
- ``X`` — a no_followup turn's answer paired with ANOTHER turn's injected
  block: the judge should say no. This is the leniency check.

    python benchmarks/prefetch_use_judge.py --cases --project <path> --out <dir>
    (judges write <dir>/verdict-NN.json: [{"id", "used", "evidence"}])
    python benchmarks/prefetch_use_judge.py --score --out <dir>

A "yes" whose evidence phrase already appears earlier in the same session is
counted as ``already_in_session`` — the turn did not need the injection for it.

Everything written here quotes real prompts and answers (CLAUDE.md "공개물에
코퍼스를 인용하지 말 것"). Keep ``--out`` under ``~/.hybrid-search/benchmarks/``
and judge locally only.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hybrid_search.hooks import _extract_user_text  # noqa: E402
from hybrid_search.index.transcript_source import discover_claude_transcripts  # noqa: E402
from hybrid_search.memory import selfeval  # noqa: E402
from hybrid_search.memory.quality import is_harness_noise  # noqa: E402

SEED = 20261004
_PREFETCH_MARK = "[hybrid-search pre-fetch]"
_SERVED_PATH = re.compile(r"`([^`\s]+?)(?::\d+(?:-\d+)?)?`")
_BATCH = 10
_MAX_QUESTION = 1500
_MAX_INJECTED = 4000
_MAX_ANSWER = 3000
_MAX_ACTIONS = 15
_MIN_EVIDENCE = 6


def _read_records(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
    except OSError:
        return []
    return records


def _injected_text(rec: dict) -> str:
    """The pre-fetch block a UserPromptSubmit hook attached, or ''."""
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("hookEvent") != "UserPromptSubmit":
        return ""
    content = att.get("content")
    parts = content if isinstance(content, list) else [content]
    text = "\n".join(p for p in parts if isinstance(p, str))
    return text if _PREFETCH_MARK in text else ""


def _clip(text: str, limit: int) -> str:
    """Keep the head and the tail — an answer's conclusion is at the end."""
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head] + "\n…\n" + text[-(limit - head):]


def turns(records: list[dict]) -> list[dict]:
    """One dict per user turn that received a pre-fetch block."""
    out: list[dict] = []
    current: dict | None = None
    for position, rec in enumerate(records):
        if rec.get("type") == "user":
            prompt = _extract_user_text(rec)
            if prompt is not None:
                current = {"position": position, "prompt": prompt, "injected": "",
                           "reads": [], "searches": [], "actions": [], "answer": []}
                out.append(current)
                continue
        if current is None:
            continue
        injected = _injected_text(rec)
        if injected:
            current["injected"] = injected
            continue
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                current["answer"].append(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name") or ""
                tool_input = block.get("tool_input") or block.get("input") or {}
                opened, searched = selfeval._followups(name, tool_input)
                current["reads"].extend(opened)
                current["searches"].extend(searched)
                target = (opened or searched or [""])[0]
                current["actions"].append(f"{name} {target}".strip())
    return [t for t in out if t["injected"] and not is_harness_noise(t["prompt"])]


def kind_of(turn: dict) -> str:
    """``no_followup`` / ``adopted`` / ``other`` — by what the agent did next."""
    served = _SERVED_PATH.findall(turn["injected"])
    if any(selfeval._paths_match(read, path) for read in turn["reads"] for path in served):
        return "adopted"
    if not turn["reads"] and not turn["searches"]:
        return "no_followup"
    return "other"


def _case(case_id: str, turn: dict, injected: str) -> dict:
    return {
        "id": case_id,
        "question": _clip(turn["prompt"], _MAX_QUESTION),
        "injected": _clip(injected, _MAX_INJECTED),
        "actions": turn["actions"][:_MAX_ACTIONS],
        "answer": _clip("\n".join(turn["answer"]), _MAX_ANSWER),
    }


def build_cases(pool: list[dict], n: int, x: int, seed: int = SEED) -> tuple[list[dict], dict]:
    """(shuffled cases, key). ``pool`` items carry ``kind`` and ``source``."""
    rng = random.Random(seed)
    quiet = [t for t in pool if t["kind"] == "no_followup" and t["answer"]]
    rng.shuffle(quiet)
    picked_n, picked_x = quiet[:n], quiet[n:n + x]
    adopted = [t for t in pool if t["kind"] == "adopted" and t["answer"]]
    cases: list[tuple[str, dict, str]] = []
    cases += [("N", t, t["injected"]) for t in picked_n]
    cases += [("P", t, t["injected"]) for t in adopted]
    for t in picked_x:
        others = [o for o in pool if o["source"] != t["source"]] or [o for o in pool if o is not t]
        cases.append(("X", t, rng.choice(others)["injected"]))
    rng.shuffle(cases)
    out, key = [], {}
    for number, (kind, turn, injected) in enumerate(cases, start=1):
        case_id = f"c{number:03d}"
        out.append(_case(case_id, turn, injected))
        key[case_id] = {"kind": kind, "source": turn["source"], "position": turn["position"]}
    return out, key


def _normal(text: str) -> str:
    return " ".join((text or "").split()).lower()


def session_text_before(records: list[dict], position: int) -> str:
    """Everything said or shown in the session before the turn at ``position``."""
    return _normal(json.dumps(records[:position], ensure_ascii=False))


def settle(verdict: dict, key_row: dict, records_for) -> str:
    """Final label for one verdict: yes / no / unclear / already_in_session."""
    used = (verdict.get("used") or "").strip().lower()
    if used != "yes":
        return used if used in ("no", "unclear") else "unclear"
    evidence = _normal(verdict.get("evidence") or "")
    if len(evidence) < _MIN_EVIDENCE:
        return "yes"
    before = session_text_before(records_for(key_row["source"]), key_row["position"])
    return "already_in_session" if evidence in before else "yes"


def tally(labels: dict[str, str], key: dict, raw: dict[str, str] | None = None) -> dict:
    """Counts by kind, the N rate, and whether the judge can be trusted.

    ``labels`` are the settled labels; ``raw`` is what the judge said before
    the session check. Validity is about the judge, so it is read off
    ``raw``: a positive case the session already knew is still a case the
    judge got right. (The first run counted it against the judge — P fell
    from 30/34 to 18/34 and the run called itself invalid, 2026-10-04.)
    """
    raw = raw if raw is not None else labels
    by_kind: dict[str, Counter] = {"N": Counter(), "P": Counter(), "X": Counter()}
    said_yes: Counter = Counter()
    for case_id, label in labels.items():
        kind = key[case_id]["kind"]
        by_kind[kind][label] += 1
        said_yes[kind] += raw.get(case_id) == "yes"

    def rate(count: int, kind: str) -> float | None:
        total = sum(by_kind[kind].values())
        return round(count / total, 4) if total else None

    p_raw, x_raw = rate(said_yes["P"], "P"), rate(said_yes["X"], "X")
    return {
        "counts": {k: dict(v) for k, v in by_kind.items()},
        "n_yes_rate": rate(by_kind["N"]["yes"], "N"),
        "n_yes_rate_raw": rate(said_yes["N"], "N"),
        "p_yes_rate_raw": p_raw,
        "x_yes_rate_raw": x_raw,
        "judge_valid": x_raw is not None and x_raw <= 0.10 and (p_raw is None or p_raw >= 0.70),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--project", help="project root (for --cases)")
    ap.add_argument("--out", required=True, help="case directory, outside every repo")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--x", type=int, default=20)
    args = ap.parse_args()
    out = Path(args.out).expanduser()

    if args.cases:
        pool: list[dict] = []
        for path in discover_claude_transcripts(Path(args.project).expanduser()):
            for turn in turns(_read_records(path)):
                pool.append({**turn, "kind": kind_of(turn), "source": str(path)})
        cases, key = build_cases(pool, args.n, args.x)
        out.mkdir(parents=True, exist_ok=True)
        for start in range(0, len(cases), _BATCH):
            name = f"batch-{start // _BATCH + 1:02d}.json"
            (out / name).write_text(
                json.dumps(cases[start:start + _BATCH], ensure_ascii=False, indent=1),
                encoding="utf-8")
        (out / "key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
        print(f"turns with a pre-fetch: {len(pool)} {dict(Counter(t['kind'] for t in pool))}")
        print(f"cases: {len(cases)} {dict(Counter(k['kind'] for k in key.values()))} "
              f"in {(len(cases) + _BATCH - 1) // _BATCH} batches → {out}")
        return 0

    if args.score:
        key = json.loads((out / "key.json").read_text(encoding="utf-8"))
        cache: dict[str, list[dict]] = {}

        def records_for(source: str) -> list[dict]:
            if source not in cache:
                cache[source] = _read_records(Path(source))
            return cache[source]

        labels: dict[str, str] = {}
        raw: dict[str, str] = {}
        for path in sorted(out.glob("verdict-*.json")):
            for verdict in json.loads(path.read_text(encoding="utf-8")):
                case_id = verdict.get("id")
                if case_id in key:
                    raw[case_id] = (verdict.get("used") or "").strip().lower()
                    labels[case_id] = settle(verdict, key[case_id], records_for)
        result = tally(labels, key, raw)
        result["judged"], result["cases"] = len(labels), len(key)
        (out / "report.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    ap.error("pass --cases or --score")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
