"""Label the displacements the cycle found, without a person.

The displacement audit reports which records the supersession map removed.
Turning that into "how many were damage" has always needed a human, and
this project has exactly one — who runs a math academy and cannot spend
his week judging retrieval cases. That ceiling is accepted, not a to-do.

What made it tractable is that the criterion became decidable. On
2026-09-12 the tool's owner ruled that a later merge of a DIFFERENT branch
is not an update of an earlier one, and with that written into the rule an
independent model and the hand labels went from Cohen's κ 0.50 to 0.80
over the same 31 cases. A judge that agrees with a person four times out
of five on a rule that person wrote is worth reading; before the ruling it
was not.

So: this sends the unjudged cases to that judge, and writes verdicts into
the label file marked `judge: llm`. Hand labels are never overwritten —
a person's verdict outranks the model's, always, and the file keeps both
so the agreement rate stays computable.

    export DEEPSEEK_API_KEY=…
    python benchmarks/judge_displacements.py --report <cycle raw>/disp_<project>.json

Cases leave this machine. They are the measured corpus's own turns, so
scrub what identifies people first — `--dry-run` prints exactly what would
be sent and stops.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.memory import supersession as ss  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.storage.db import StoreDB  # noqa: E402
from hybrid_search.storage.indexes import IndexPaths, get_project_dir  # noqa: E402

HOME_BENCH = Path("~/.hybrid-search/benchmarks").expanduser()
BODY_CHARS = 900
BATCH = 10

RULE = """당신은 검색 시스템의 '기록 대체' 판정자다. 아래 규칙만으로 판정하라.

한 검색 시스템이 "이 기록은 나중에 다시 답해졌다"고 판단해, 밀려난 기록 대신
후계자 기록을 결과에 내보냈다. 각 사례에서 **후계자가 밀려난 기록과 같은 일에 대한
더 나중의 답인가**를 판정하라.

- legitimate =
  · 같은 질문의 더 나중 답
  · 밀려난 쪽이 자기 답이 없고 후계자가 그 질문에 답함
  · 밀려난 기록의 충실한 종합
  · 반복되는 운영 요청(머지·푸시·배포·정리 등)이되 **같은 실물**(같은 브랜치·같은
    커밋·같은 파일)에 대한 것

- damage =
  · 후계자가 다른 질문에 답한다
  · 프로브가 찾던 내용을 후계자가 버렸다
  · **말은 같지만 다른 실물을 다룬 별개의 실행이다**

**핵심 판정 기준**: 두 기록이 언급하는 브랜치·커밋·파일이 하나도 안 겹치는데
문구만 비슷하다면, 그것은 같은 일이 아니다.

`probe`는 이 사례를 띄운 검색어인데 없는 사례도 있다 — 없으면 두 기록의 쌍만 보고
판정하라. 프로브 유무는 판정에 아무 의미가 없다.

출력은 JSON 배열만. 다른 말 금지.
[{"case":"<id>","verdict":"legitimate|damage","why":"<25자 이내>"}, ...]"""


def owned(text: str) -> tuple[str, str]:
    """(question, body) — the record's own words, without the results dump."""
    head = text.split("## Top results", 1)[0]
    question = ss._frontmatter_value(text, "query") or ""
    body = head.split("---", 2)[-1] if head.startswith("---") else head
    body = re.sub(r"^- \*\*\w+\*\*:.*$", "", body, flags=re.M)
    body = re.sub(r"^#+ Q:.*$", "", body, flags=re.M)
    return question, re.sub(r"\n{3,}", "\n\n", body).strip()[:BODY_CHARS]


def ask(cases: list[dict], model: str) -> list[dict]:
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    body = {
        "model": model,
        "messages": [{"role": "user", "content":
                      RULE + "\n\n" + json.dumps(cases, ensure_ascii=False, indent=1)}],
        "stream": False,
    }
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"],
                 "Content-Type": "application/json"})
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=1800))
            txt = r["choices"][0]["message"]["content"]
            return json.loads(txt[txt.find("["):txt.rfind("]") + 1])
        except Exception as exc:  # transient TLS resets are common here
            last = exc
            print(f"    retry {attempt + 1}: {type(exc).__name__}", flush=True)
            time.sleep(8 * (attempt + 1))
    raise last  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True,
                    help="displacement_audit.py 가 쓴 JSON")
    ap.add_argument("--config", default=str(Path("~/.hybrid-search/config.toml")
                                            .expanduser()))
    ap.add_argument("--labels", help="라벨 파일 (기본: 프로젝트 이름으로 찾는다)")
    ap.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL",
                                                      "deepseek-reasoner"))
    ap.add_argument("--dry-run", action="store_true",
                    help="보낼 내용을 출력만 하고 멈춘다")
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    project = report["project"]

    labels_path = Path(args.labels).expanduser() if args.labels else None
    if labels_path is None:
        for f in HOME_BENCH.glob("*_displacement_labels.json"):
            if json.loads(f.read_text(encoding="utf-8")).get("project") == project:
                labels_path = f
                break
    if labels_path is None or not labels_path.is_file():
        print(f"{project}: 라벨 파일을 못 찾았다")
        return 1
    labels = json.loads(labels_path.read_text(encoding="utf-8"))

    pending: dict[str, str] = {}
    for row in report["rows"]:
        for c in row.get("displaced") or []:
            if c.get("by_map") and c["chunk"] not in labels["labels"]:
                pending.setdefault(c["chunk"], row.get("query") or "")
    if not pending:
        print(f"{project}: 판정 대기 없음")
        return 0

    cfg = load_config(Path(args.config).expanduser())
    reg = ProjectRegistry(cfg.global_dir)
    pinfo = reg.get_by_name(project)
    db = StoreDB(IndexPaths(get_project_dir(cfg.projects_dir, pinfo.id)).store_db)
    content = {c.id: (c.content or "")
               for c in db.get_chunks_by_node_type(pinfo.id, "qa_log")}
    smap = db.get_qa_superseding(list(content))
    db.close()

    cases = []
    for chunk, probe in pending.items():
        succ = smap.get(chunk)
        if not succ or chunk not in content or succ not in content:
            continue
        dq, dbody = owned(content[chunk])
        sq, sbody = owned(content[succ])
        case = {"case": chunk, "displaced_question": dq, "displaced_body": dbody,
                "successor_question": sq, "successor_body": sbody}
        if probe:
            case["probe"] = probe[:200]
        cases.append(case)

    chars = sum(len(c["displaced_body"]) + len(c["successor_body"]) for c in cases)
    print(f"{project}: 판정 대기 {len(cases)}건 · 본문 {chars:,}자가 외부로 나간다")
    if args.dry_run:
        print(json.dumps(cases, ensure_ascii=False, indent=1))
        return 0
    if "DEEPSEEK_API_KEY" not in os.environ:
        print("DEEPSEEK_API_KEY 가 없다")
        return 1

    verdicts: list[dict] = []
    for i in range(0, len(cases), BATCH):
        got = ask(cases[i:i + BATCH], args.model)
        verdicts.extend(got)
        print(f"  batch {i // BATCH + 1}: {len(got)}건", flush=True)

    added = 0
    for v in verdicts:
        cid = v.get("case")
        if not cid or cid in labels["labels"]:
            continue  # a person's verdict always outranks the model's
        if v.get("verdict") not in ("legitimate", "damage"):
            continue
        labels["labels"][cid] = {
            "verdict": v["verdict"],
            "judge": "llm",
            "model": args.model,
            "why": (v.get("why") or "")[:120],
        }
        added += 1
    labels["version"] = labels.get("version", 1) + 1
    labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    hand = sum(1 for x in labels["labels"].values() if x.get("judge") != "llm")
    print(f"{added}건 추가 · 총 {len(labels['labels'])}건 "
          f"(사람 {hand} · 모델 {len(labels['labels']) - hand})")
    print(f"→ {labels_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
