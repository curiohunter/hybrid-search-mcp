"""Judge a retrieval change by the questions it was asked — not by pairs.

Until 2026-09-25 a supersession change was judged pair by pair: "was it
right to replace old record A with new record B?". A pair has no purpose.
Nobody — human or model — can say whether a replacement was right without
knowing what the search was for (docs/plans/2026-09-25-query-grounded-
judgment.md §1), and the pair judges we had disagreed with each other.

This runner asks the question that has a purpose: here is a question a
user really asked, here are the two top-10 lists two versions of the code
return for it — which list answers it better? Only the questions whose
lists differ are judged; a question both versions answer identically is a
tie by construction.

One process cannot load two versions of the code, so the work is split:

    --freeze   fix the question set once, from the M arm (probes + gold)
    --dump     each arm, from ITS OWN checkout, writes its top-10 lists
    --pair     read two dumps, keep the questions whose lists differ, write
               blind batches (X/Y assigned per question by seed) for the
               judge, twice — the second pass with X/Y swapped
    --score    read the judge's verdicts, un-blind, tally

    python benchmarks/query_judge.py --freeze --config $M/config.toml \\
        --project <name> --gold A=<gold.json> --gold B=<gold.json> --out Q.json
    python benchmarks/query_judge.py --dump --config $M/config.toml \\
        --questions Q.json --label M --out dump-M.json
    (G worktree) python benchmarks/query_judge.py --dump ... --label G ...
    python benchmarks/query_judge.py --pair --m dump-M.json --g dump-G.json \\
        [--noise dump-M2.json] --out <cases dir>
    (judge subagents write <cases dir>/<group>/verdict-NNN.json)
    python benchmarks/query_judge.py --score --cases <cases dir> --out report.json

Nothing but this runner is committed. The questions are the measured
project's own turns and the lists quote its records (CLAUDE.md "공개물에
코퍼스를 인용하지 말 것") — keep every output under ~/.hybrid-search/benchmarks/.
The judge runs locally only: the corpus is private and names real people.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from hybrid_search.memory import qa_shape  # noqa: E402
from hybrid_search.memory import supersession as ss  # noqa: E402

SEED = 20260925            # X/Y assignment and batch order (plan §2.4)
PROBE_SEED = 20260910      # the displacement audit's own seed — same probe set
PROBE_SINCE = "2026-09-12"
PROBE_SAMPLE = 600
LIMIT = 10
QUESTION_CHARS = 200
TEXT_CHARS = 400
BATCH_SIZE = 10
ARMS = ("M", "G")
VERDICTS = ("X", "Y", "same")
MIN_CALIBRATION = 5

# Final wording, registered before measuring (plan §3 · 2026-09-26).
JUDGE_PROMPT = """\
사용자가 아래 질문으로 과거 기록(대화 턴 · 질의응답 기록 · 노트 · 코드 · 커밋)을
검색했다. 같은 질문에 대해 두 검색 결과 목록 X, Y 가 있다. 각 목록은 위에서부터
순위 순이다.

이 질문에 답하는 데 필요한 내용을 **더 위에, 더 온전히** 보여주는 쪽을 골라라.
판단 근거 (앞의 것이 우선):
(1) 질문에 직접 답하는 기록이 목록에 있는가. 있다면 몇 위인가 — 위일수록 낫다.
(2) 질문과 다른 일을 다룬 기록이 답하는 기록보다 위에 있어 그것을 밀어냈는가.
(3) 같은 일에 대한 낡은 답과 최신 답이 함께 있으면 최신 답이 먼저인 쪽이 낫다.
    다른 일에 대한 기록은 최신이어도 가산하지 않는다.
두 목록의 차이가 답을 찾는 데 영향이 없거나 우열을 가릴 수 없으면 same.
X/Y 표시는 사례마다 무작위다. 목록 길이나 X/Y 이름으로 판단하지 말 것.

출력은 JSON 배열만. 원소 하나가 사례 하나:
{"q": "<사례 id>", "better": "X" | "Y" | "same", "why": "<25자 이내>"}
why 에 기록 원문을 옮겨 적지 말 것.
"""


# ── pure functions ──────────────────────────────────────────────────────

def lists_differ(a: list[str], b: list[str]) -> bool:
    """Members OR order differ. Two lists of chunk ids, top-``LIMIT``."""
    return list(a) != list(b)


def g_is_x(qid: str, seed: int = SEED) -> bool:
    """Pass-1 side of the G arm, fixed per question (swapped in pass 2).

    Seeded by the question id, not by position, so adding or dropping a
    question never reshuffles the others."""
    return random.Random(f"{seed}:{qid}").random() < 0.5


def to_arm(verdict: str, g_x: bool) -> str:
    """Un-blind one verdict: ``X``/``Y``/``same`` → ``G``/``M``/``same``."""
    if verdict == "same":
        return "same"
    return "G" if (verdict == "X") == g_x else "M"


def combine(arm1: str, arm2: str) -> str:
    """Both passes must pick the same arm; anything else is ``same`` (§2.5)."""
    return arm1 if arm1 == arm2 else "same"


def first_hit_rank(items: list[dict], phrases: list[str]) -> int | None:
    """Phrase scoring on dumped items — mirrors run_conv_bench.score_query."""
    for i, item in enumerate(items, start=1):
        text = f"{item.get('content') or ''}\n{item.get('snippet') or ''}"
        if any(p in text for p in phrases):
            return i
    return None


def reference_arm(rank_m: int | None, rank_g: int | None) -> str:
    """The phrase scorer's preference: the earlier first hit wins."""
    inf = LIMIT + 1
    m, g = rank_m or inf, rank_g or inf
    if m == g:
        return "same"
    return "M" if m < g else "G"


def gold_metrics(ranks: list[int | None]) -> dict:
    """found · top3 · mrr over a whole gold set (a miss counts 0)."""
    n = len(ranks) or 1
    hit = [r for r in ranks if r]
    return {
        "n": len(ranks),
        "found": round(len(hit) / n, 4),
        "top3": round(sum(1 for r in hit if r <= 3) / n, 4),
        "mrr": round(sum(1.0 / r for r in hit) / n, 4),
    }


def _qa_question(content: str) -> str:
    """The whole question — the frontmatter holds only its first line (§5)."""
    m = re.search(r"^# Q: (.*?)(?=^- \*\*query_type\*\*|^## |\Z)", content,
                  re.MULTILINE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return ss._frontmatter_value(content, "query") or ""


def _flat(text: str, cap: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= cap else text[:cap] + "…"


_META_LINE = re.compile(r"^\[[^\n]*\]$")


def snippet_body(snippet: str) -> str:
    """The search snippet without its bracketed head lines.

    The orchestrator prefixes ``[qa - stop_hook - decision - 3d ago]``-style
    trust lines (and ``[needs_revalidation …]``) — kind labels the judge must
    not see (§3.1). Only whole leading ``[…]`` lines are dropped."""
    lines = snippet.split("\n")
    while lines and _META_LINE.match(lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines)


def render_item(rank: int, item: dict) -> str:
    """One list entry as the judge sees it: kind, path, date, text."""
    content = item.get("content") or ""
    date = ""
    if item.get("node_type") == "qa_log":
        date = (ss._frontmatter_value(content, "timestamp") or "")[:10]
    date = date or (item.get("file_mtime") or "")[:10]
    head = f"{rank}. [{item.get('node_type') or '?'}] {item.get('file_path') or ''}"
    head += f" · {date}" if date else ""
    if item.get("node_type") == "qa_log":
        answer = qa_shape.answer_excerpt(content)
        snip = snippet_body(item.get("snippet") or "")
        body = f"질문: {_flat(_qa_question(content), QUESTION_CHARS)}"
        if snip.strip():
            body += f"\n   스니펫: {_flat(snip, TEXT_CHARS)}"
        body += "\n   답: " + (_flat(answer, TEXT_CHARS) if answer else "(답 없음)")
    else:
        body = _flat(content or item.get("snippet") or "", TEXT_CHARS)
    return f"{head}\n   {body}"


def seen_hit_rank(items: list[dict], phrases: list[str]) -> int | None:
    """First-hit rank on what the JUDGE sees (the rendered entry), not on the
    whole record. Calibration compares the judge against this: a phrase deep
    in a long pasted question is invisible to the judge, and scoring it
    there would charge the judge for text it was never shown (§3.1)."""
    flat = [" ".join(p.split()) for p in phrases]
    for i, item in enumerate(items, start=1):
        text = render_item(i, item)
        if any(p and p in text for p in flat):
            return i
    return None


def build_cases(questions: list[dict], dump_m: dict, dump_g: dict,
                noise: dict | None = None) -> dict:
    """Split the questions into judged cases, ties and noise.

    A question whose M list already differs between two M runs (``noise``)
    cannot be attributed to the code change and is set aside, not judged.
    """
    cases, ties, noisy, ranks, self_found = [], [], [], {}, {}
    for n, q in enumerate(questions, start=1):
        qid = q["id"]
        m, g = dump_m["results"][qid], dump_g["results"][qid]
        m_ids = [i["chunk_id"] for i in m]
        g_ids = [i["chunk_id"] for i in g]
        if q.get("any_of"):
            ranks[qid] = {"set": q["set"], "M": first_hit_rank(m, q["any_of"]),
                          "G": first_hit_rank(g, q["any_of"]),
                          "M_seen": seen_hit_rank(m, q["any_of"]),
                          "G_seen": seen_hit_rank(g, q["any_of"])}
        if q.get("probe_chunk"):
            self_found[qid] = {a: q["probe_chunk"] in ids
                               for a, ids in (("M", m_ids), ("G", g_ids))}
        if noise is not None and lists_differ(
                m_ids, [i["chunk_id"] for i in noise["results"][qid]]):
            noisy.append(qid)
        elif not lists_differ(m_ids, g_ids):
            ties.append(qid)
        else:
            cases.append({"cid": f"c{n:04d}", "qid": qid, "set": q["set"],
                          "query": q["query"], "g_is_x": g_is_x(qid)})
    return {"cases": cases, "ties": ties, "noisy": noisy,
            "gold_ranks": ranks, "self_found": self_found}


def render_case(case: dict, m: list[dict], g: list[dict], swapped: bool) -> str:
    g_x = case["g_is_x"] != swapped
    x, y = (g, m) if g_x else (m, g)
    lines = [f"## 사례 {case['cid']}", "",
             f"질문: {_flat(case['query'], 600)}", "", "### X"]
    lines += [render_item(i, it) for i, it in enumerate(x, start=1)] or ["(결과 없음)"]
    lines += ["", "### Y"]
    lines += [render_item(i, it) for i, it in enumerate(y, start=1)] or ["(결과 없음)"]
    return "\n".join(lines) + "\n"


def calibration_targets(cases: dict) -> list[str]:
    """Gold cases on which the judge-visible phrase ranks pick a side —
    the calibration denominator. Counted at --pair time: fewer than
    ``MIN_CALIBRATION`` and the judge is not run at all (§3.1)."""
    out = []
    for c in cases["cases"]:
        ref = cases["gold_ranks"].get(c["qid"])
        if ref and reference_arm(ref["M_seen"], ref["G_seen"]) != "same":
            out.append(c["qid"])
    return out


def hidden_answers(gold_ranks: dict) -> dict[str, int]:
    """Per arm: gold questions whose answer phrase is in a record's content
    but in no rendered entry — the judge could not have seen it."""
    return {arm: sum(1 for r in gold_ranks.values()
                     if r[arm] is not None and r[f"{arm}_seen"] is None)
            for arm in ARMS}


def score(cases: dict, verdicts: dict[str, dict[str, str]]) -> dict:
    """Tally. ``verdicts`` = {pass name: {cid: X|Y|same}}, passes "1" and "2"."""
    rows = []
    for c in cases["cases"]:
        v1 = verdicts.get("1", {}).get(c["cid"])
        v2 = verdicts.get("2", {}).get(c["cid"])
        if v1 not in VERDICTS or v2 not in VERDICTS:
            rows.append({**c, "final": None})
            continue
        a1, a2 = to_arm(v1, c["g_is_x"]), to_arm(v2, not c["g_is_x"])
        rows.append({**c, "arm1": a1, "arm2": a2, "final": combine(a1, a2)})

    def tally(group: list[dict]) -> dict:
        judged = [r for r in group if r["final"]]
        wins = {k: sum(1 for r in judged if r["final"] == k) for k in ("G", "M", "same")}
        decisive = wins["G"] + wins["M"]
        return {
            "cases": len(group), "judged": len(judged),
            "unjudged": len(group) - len(judged), **wins,
            "g_rate": round(wins["G"] / decisive, 4) if decisive else None,
            "same_rate": round(wins["same"] / len(judged), 4) if judged else None,
            "order_consistency": round(
                sum(1 for r in judged if r["arm1"] == r["arm2"]) / len(judged), 4)
            if judged else None,
        }

    probes = [r for r in rows if r["set"] == "probe"]
    gold = [r for r in rows if r["set"] != "probe"]
    calib = []
    for r in gold:
        ref = cases["gold_ranks"][r["qid"]]
        ref_arm = reference_arm(ref["M_seen"], ref["G_seen"])
        if ref_arm != "same" and r["final"]:
            calib.append({"qid": r["qid"], "reference": ref_arm, "judge": r["final"],
                          "agree": r["final"] == ref_arm})
    agree = sum(1 for c in calib if c["agree"])
    by_set: dict[str, dict] = {}
    for qid, ref in cases["gold_ranks"].items():
        by_set.setdefault(ref["set"], {"M": [], "G": []})
        for arm in ARMS:
            by_set[ref["set"]][arm].append(ref[arm])
    sf = cases["self_found"].values()
    return {
        "probes": {**tally(probes), "ties": sum(
            1 for q in cases["ties"] if q.startswith("p:")),
            "noisy": sum(1 for q in cases["noisy"] if q.startswith("p:"))},
        "calibration": {
            **tally(gold), "decisive_reference": len(calib), "agree": agree,
            "agreement": round(agree / len(calib), 4) if calib else None,
            "hidden_answers": hidden_answers(cases["gold_ranks"]),
            "rows": calib},
        "gold": {s: {arm: gold_metrics(v[arm]) for arm in ARMS}
                 for s, v in sorted(by_set.items())},
        "self_found": {arm: sum(1 for x in sf if x[arm]) for arm in ARMS},
        "rows": rows,
    }


def parse_verdicts(text: str) -> dict[str, str]:
    """A judge's JSON array → {cid: verdict}. Invalid entries are dropped
    (and later reported as unjudged), never guessed."""
    out = {}
    for v in json.loads(text):
        if isinstance(v, dict) and v.get("better") in VERDICTS:
            out[str(v.get("q"))] = v["better"]
    return out


# ── commands (these touch an index) ─────────────────────────────────────

def _load_bench(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "benchmarks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pin(config: str) -> None:
    from hybrid_search import clock
    os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")
    clock.pin_to_snapshot(config)


def cmd_freeze(args) -> int:
    from hybrid_search.config import load_config
    from hybrid_search.project import ProjectRegistry
    from hybrid_search.storage.db import StoreDB
    from hybrid_search.storage.indexes import IndexPaths, get_project_dir

    config = load_config(Path(args.config))
    pinfo = ProjectRegistry(config.global_dir).get_by_name(args.project)
    if pinfo is None:
        print(f"{args.project}: not registered")
        return 1
    db = StoreDB(IndexPaths(get_project_dir(config.projects_dir, pinfo.id)).store_db)
    try:
        probes = _load_bench("displacement_audit")._probe_set(
            db, pinfo.id, PROBE_SAMPLE, PROBE_SEED, PROBE_SINCE)
    finally:
        db.close()
    questions = [{"id": f"p:{cid}", "set": "probe", "query": q, "probe_chunk": cid}
                 for cid, q, _ in probes]
    for spec in args.gold or []:
        name, path = spec.split("=", 1)
        gold = json.loads(Path(path).read_text(encoding="utf-8"))
        if os.path.realpath(gold["project_path"]) != os.path.realpath(pinfo.path):
            print(f"gold {name}: project_path is not {args.project}")
            return 1
        questions += [{"id": f"{name}:{q['id']}", "set": name, "query": q["query"],
                       "any_of": q["any_of"]} for q in gold["queries"]]
    _write(args.out, {"project": args.project, "project_path": pinfo.path,
                      "questions": questions})
    print(f"questions: {len(probes)} probes + {len(questions) - len(probes)} gold → {args.out}")
    return 0


def cmd_dump(args) -> int:
    from hybrid_search.config import load_config
    from hybrid_search.index.embedder import Embedder
    from hybrid_search.project import ProjectRegistry
    from hybrid_search.search.orchestrator import SearchOrchestrator

    _pin(args.config)
    qs = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    config = load_config(Path(args.config))
    registry = ProjectRegistry(config.global_dir)
    orch = SearchOrchestrator(config=config, registry=registry,
                              embedder=Embedder(config.embedding, config.models_dir))
    results = {}
    for i, q in enumerate(qs["questions"], start=1):
        resp = orch.hybrid_search(query=q["query"], cwd=qs["project_path"], limit=LIMIT)
        results[q["id"]] = [{
            "chunk_id": r.chunk_id, "node_type": r.node_type, "file_path": r.file_path,
            "content": r.content or "", "snippet": r.snippet or "",
            "file_mtime": r.file_mtime,
        } for r in resp.results[:LIMIT]]
        if i % 50 == 0:
            print(f"  … {i}/{len(qs['questions'])}", flush=True)
    head = subprocess.run(["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    _write(args.out, {"label": args.label, "code": head, "config": args.config,
                      "results": results})
    print(f"dump {args.label} (code {head}): {len(results)} questions → {args.out}")
    return 0


def cmd_pair(args) -> int:
    qs_m, qs_g = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.m, args.g))
    noise = json.loads(Path(args.noise).read_text(encoding="utf-8")) if args.noise else None
    if set(qs_m["results"]) != set(qs_g["results"]):
        print("the two dumps were not made from the same question file")
        return 1
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    built = build_cases(questions, qs_m, qs_g, noise)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    groups = {"calib": [c for c in built["cases"] if c["set"] != "probe"],
              "probe": [c for c in built["cases"] if c["set"] == "probe"]}
    for group, cases in groups.items():
        for pas, swapped in (("1", False), ("2", True)):
            order = list(cases)
            random.Random(f"{SEED}:{group}:{pas}").shuffle(order)
            d = out / f"{group}-p{pas}"
            d.mkdir(exist_ok=True)
            for b in range(0, len(order), BATCH_SIZE):
                n = b // BATCH_SIZE + 1
                body = [f"# 판정 배치 {group}-p{pas} {n:03d}", "", JUDGE_PROMPT,
                        f"결과 파일: {d / f'verdict-{n:03d}.json'}", "", "---", ""]
                body += [render_case(c, qs_m["results"][c["qid"]],
                                     qs_g["results"][c["qid"]], swapped)
                         for c in order[b:b + BATCH_SIZE]]
                (d / f"batch-{n:03d}.md").write_text("\n".join(body), encoding="utf-8")
    _write(out / "cases.json", {**built, "m_code": qs_m["code"], "g_code": qs_g["code"]})
    targets = calibration_targets(built)
    print(f"judged: calib {len(groups['calib'])} · probe {len(groups['probe'])} · "
          f"ties {len(built['ties'])} · noise {len(built['noisy'])} → {out}")
    print(f"calibration targets: {len(targets)} · answer in content but not rendered: "
          f"{hidden_answers(built['gold_ranks'])}")
    if len(targets) < MIN_CALIBRATION:
        print(f"STOP: fewer than {MIN_CALIBRATION} calibration targets — "
              "the judge cannot be calibrated; do not run it (§3.1)")
        return 2
    return 0


def cmd_score(args) -> int:
    root = Path(args.cases)
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    verdicts: dict[str, dict[str, str]] = {"1": {}, "2": {}}
    for f in sorted(root.glob("*-p[12]/verdict-*.json")):
        verdicts[f.parent.name[-1]].update(parse_verdicts(f.read_text(encoding="utf-8")))
    report = {"judge_model": args.judge_model, **score(cases, verdicts)}
    _write(args.out, report)
    p, c = report["probes"], report["calibration"]
    print(f"calibration: agreement {c['agreement']} ({c['agree']}/{c['decisive_reference']}) "
          f"· order consistency {c['order_consistency']}")
    print(f"probes: G {p['G']} · M {p['M']} · same {p['same']} · ties {p['ties']} "
          f"· noise {p['noisy']} · unjudged {p['unjudged']} "
          f"· order consistency {p['order_consistency']}")
    for s, arms in report["gold"].items():
        print(f"gold {s}: M {arms['M']} · G {arms['G']}")
    print(f"self found: {report['self_found']}")
    return 0


def _write(path, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    for m in ("freeze", "dump", "pair", "score"):
        mode.add_argument(f"--{m}", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--project")
    ap.add_argument("--gold", action="append", help="NAME=path, repeatable (freeze)")
    ap.add_argument("--questions", help="the frozen question file")
    ap.add_argument("--label", choices=ARMS)
    ap.add_argument("--m", help="M dump (pair)")
    ap.add_argument("--g", help="G dump (pair)")
    ap.add_argument("--noise", help="a second M dump; questions it disagrees on are set aside")
    ap.add_argument("--cases", help="cases directory (score)")
    ap.add_argument("--judge-model", help="the judge subagents' model id, recorded (score)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.freeze:
        return cmd_freeze(args)
    if args.dump:
        return cmd_dump(args)
    if args.pair:
        return cmd_pair(args)
    if not args.judge_model:
        ap.error("--score needs --judge-model")
    return cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
