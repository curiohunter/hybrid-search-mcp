"""One command that measures everything, compares to last time, and says what moved.

Every number in this repo has, until now, been produced by a person running
five scripts in the right order against a snapshot they remembered to take.
That works exactly as long as the person is there, and it has already cost
this line two corrupted readings (a live index moving mid-measurement, and a
benchmark run against half-rebuilt vectors).

So this runs the whole battery, writes the result next to the previous one,
and prints the diff. Nothing here is new measurement — it is the existing
runners, in the order the measurement protocol already specifies.

**The part that is new is the holdout.** Each cycle records the date it ran
and the code it ran against. The next cycle probes the displacement audit
with ``--since <that date>``, so it is scored on qa records that did not
exist when the rules were frozen. One corpus and one user cannot become
many, but a test set that refills itself every week is worth more than a
bigger sample of the records the thresholds were fitted to. The dogfood
corpus produces ~380 records a week; four weeks is a clean thousand.

    python benchmarks/cycle.py                 # freeze, measure, diff, record
    python benchmarks/cycle.py --no-freeze     # reuse the last snapshot
    python benchmarks/cycle.py --quick         # skip the displacement audits

Exit code is 1 when a gate regressed, so it can sit in a scheduler.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

HOME_BENCH = Path("~/.hybrid-search/benchmarks").expanduser()
CYCLE_DIR = HOME_BENCH / "cycle"
SNAP = Path("~/.hybrid-search/.cycle-snapshot").expanduser()

# The objective function this line has kept since 2026-09-08, plus the two
# floors added since. A cycle FAILS when one of these moves the wrong way
# against the previous cycle — not against a hardcoded target, so the bar
# rises with the system instead of being re-argued every session.
GATES = (
    # (path in the cycle record, human name, "higher is better")
    ("set_a.answer_in_top3", "Set A top3", True),
    ("set_a.answer_found", "Set A found", True),
    ("set_b.answer_found", "Set B found", True),
    ("set_b.answer_in_top3", "Set B top3", True),
    ("code.primary_top5", "코드축 top5", True),
    ("code.recall_at_10", "코드축 recall@10", True),
    ("code.memory_at_3", "코드축 memory@3", True),
    ("damage_total", "밀어냄 damage(합계)", False),
)


def _sh(*args: str) -> str:
    out = subprocess.run(args, capture_output=True, text=True, cwd=REPO, check=False)
    return (out.stdout or "").strip()


# Figures are compared at the precision they are printed at, with a hair of
# slack — a float that differs in the 5th decimal is the same reading.
_EPS = 1e-9


def _r(v):
    """Round a metric for both display and comparison."""
    return round(v, 4) if isinstance(v, (int, float)) else v


def _dig(obj: dict, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def freeze() -> Path:
    """Copy the live index aside. The corpus moves while you measure it."""
    live = Path("~/.hybrid-search").expanduser()
    SNAP.mkdir(parents=True, exist_ok=True)
    for sub in ("projects", "global"):
        src, dst = live / sub, SNAP / sub
        if not src.is_dir():
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    cfg = (live / "config.toml").read_text(encoding="utf-8")
    (SNAP / "config.toml").write_text(
        cfg.replace('data_dir = "~/.hybrid-search"', f'data_dir = "{SNAP}"'),
        encoding="utf-8",
    )
    return SNAP / "config.toml"


def _run(cmd: list[str]) -> None:
    print("  $ " + " ".join(c.replace(str(REPO) + "/", "") for c in cmd), flush=True)
    subprocess.run(cmd, cwd=REPO, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def labelled_projects() -> dict[str, Path]:
    """Projects that have hand labels, discovered from the label files.

    The files name their own project, so adding a corpus is dropping a file
    in — no list here to fall out of date.
    """
    found: dict[str, Path] = {}
    for f in sorted(HOME_BENCH.glob("*_displacement_labels.json")):
        try:
            name = json.loads(f.read_text(encoding="utf-8")).get("project")
        except (OSError, ValueError):
            continue
        if name:
            found[name] = f
    return found


def measure(config: Path, out: Path, since: str | None, quick: bool,
            sample: int = 600, prev_raw: Path | None = None) -> dict:
    gold = HOME_BENCH
    rec: dict = {}
    py = sys.executable

    print("· supersession 재계산")
    _run([py, "benchmarks/recompute_supersession.py", "--config", str(config)])

    for key, goldfile in (("set_a", "valuein_conv_gold.json"),
                          ("set_b", "valuein_conv_gold_rawonly.json")):
        path = gold / goldfile
        if not path.is_file():
            continue
        print(f"· {key}")
        dst = out / f"{key}.json"
        _run([py, "benchmarks/run_conv_bench.py", "--config", str(config),
              "--gold", str(path), "--out", str(dst), "--repeat", "3"])
        if dst.is_file():
            doc = json.loads(dst.read_text(encoding="utf-8"))
            s = doc["summary"]
            # Spread across the repeated runs. A non-zero spread means the
            # reading is contaminated, not that the system moved — this
            # line once read 0.05 of noise as a regression and chased it.
            tops = [r["answer_in_top3"] for r in doc.get("summaries") or []]
            rec[key] = {
                "answer_found": round(s["answer_found"], 4),
                "answer_in_top3": round(s["answer_in_top3"], 4),
                "mrr": round(s["mrr"], 4),
                "spread": round(max(tops) - min(tops), 4) if len(tops) > 1 else 0.0,
            }

    print("· 코드축")
    dst = out / "code.json"
    _run([py, "benchmarks/run_valuein_bench.py", "--config", str(config),
          "--gold", "benchmarks/valuein_gold.json", "--out", str(dst), "--limit", "10"])
    if dst.is_file():
        hy = json.loads(dst.read_text(encoding="utf-8"))["summary"]["overall"]["hybrid"]
        rec["code"] = {
            "primary_top5": round(hy["primary_top5"], 4),
            "recall_at_10": round(hy["recall_at_10_mean"], 4),
            "memory_at_3": round(hy["memory_hit_rate_at_3"], 4),
        }

    if quick:
        return rec

    rec["displacement"] = {}
    total_damage = 0
    for project, labels in labelled_projects().items():
        print(f"· 밀어냄 감사 — {project}" + (f" (홀드아웃 {since} 이후)" if since else ""))
        dst = out / f"disp_{project}.json"
        cmd = [py, "benchmarks/displacement_audit.py", "--config", str(config),
               "--project", project, "--sample", str(sample),
               "--labels", str(labels), "--out", str(dst)]
        if since:
            cmd += ["--since", since]
        # Last cycle's report carries the probes that were in the window
        # then; the audit counts the ones pushed out of it since.
        carry = prev_raw / f"disp_{project}.json" if prev_raw else None
        if carry and carry.is_file():
            cmd += ["--carry", str(carry)]
        _run(cmd)
        if not dst.is_file():
            continue
        d = json.loads(dst.read_text(encoding="utf-8"))
        seen: dict[str, str | None] = {}
        for row in d["rows"]:
            for c in row.get("displaced") or []:
                if c.get("by_map"):
                    seen[c["chunk"]] = c.get("label")
        dmg = sum(1 for v in seen.values() if v == "damage")
        rec["displacement"][project] = {
            "probes": d["probes"],
            "displaced": len(seen),
            "labelled": sum(1 for v in seen.values() if v),
            "damage": dmg,
            "unlabelled": sorted(k for k, v in seen.items() if not v),
            "self_retrieval": d["self_found_with_map"],
            "carried": d.get("carried", 0),
            "window_exits": len(d.get("window_exits") or []),
        }
        total_damage += dmg
    rec["damage_total"] = total_damage
    return rec


def _recent_qa_minutes() -> int | None:
    """Minutes since the newest qa record anywhere. None when there are none.

    The measurement is a few thousand searches against the same embedding
    backend the user's own session uses, and this line has already had one
    bulk job and one live session collide. A qa file written in the last
    few minutes means someone is working; the scheduler will come back in
    an hour.
    """
    newest = 0.0
    root = Path("~/.hybrid-search/projects").expanduser()
    reg = Path("~/.hybrid-search/config.toml").expanduser()
    if not reg.is_file():
        return None
    import time
    for qa_dir in Path("~/project").expanduser().glob("*/*/.hybrid-search/qa"):
        for f in qa_dir.rglob("*.md"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    if not newest:
        # Fall back to index mtime — cheaper and still a liveness signal.
        for db in root.glob("*/store.db"):
            try:
                newest = max(newest, db.stat().st_mtime)
            except OSError:
                continue
    if not newest:
        return None
    return int((time.time() - newest) / 60)


def previous() -> dict | None:
    runs = sorted(CYCLE_DIR.glob("*.json"))
    if not runs:
        return None
    try:
        return json.loads(runs[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report(now: dict, prev: dict | None) -> tuple[str, bool]:
    lines: list[str] = []
    regressed = False
    lines.append(f"# 측정 주기 — {now['date']}")
    lines.append("")
    lines.append(f"코드 `{now['code_sha']}` · 이전 주기 "
                 + (f"`{prev['date']}` (`{prev['code_sha']}`)" if prev else "없음(첫 실행)"))
    if now.get("holdout_since"):
        lines.append(f"**홀드아웃**: `{now['holdout_since']}` 이후에 쓰인 레코드만 채점했다 — "
                     "그 레코드들은 지금 규칙이 얼려질 때 존재하지 않았다.")
    lines.append("")
    lines.append("| 지표 | 이전 | 이번 | |")
    lines.append("|---|---|---|---|")
    for path, name, higher in GATES:
        cur, old = _dig(now, path), (_dig(prev, path) if prev else None)
        if cur is None:
            continue
        mark = "—"
        # Compare at the precision the report prints. Without this a change
        # in how a figure is ROUNDED reads as a regression: the first cycle
        # stored 0.14634146341463414 and the next stored 0.1463, and the
        # gate called it a loss (2026-09-12, first run of this file).
        if old is not None and abs(_r(cur) - _r(old)) > _EPS:
            worse = (_r(cur) < _r(old)) if higher else (_r(cur) > _r(old))
            mark = "**회귀**" if worse else "개선"
            regressed = regressed or worse
        lines.append(f"| {name} | {_r(old) if old is not None else '—'} "
                     f"| {_r(cur)} | {mark} |")
    for key in ("set_a", "set_b"):
        sp = _dig(now, f"{key}.spread")
        if sp:
            lines.append("")
            lines.append(f"> ⚠ {key} 의 `--repeat` 스프레드가 {sp} 다. "
                         "**이 판독은 버려라** — 시스템이 움직인 게 아니라 측정이 오염된 것이다.")
            regressed = True
    disp = now.get("displacement") or {}
    if disp:
        lines.append("")
        lines.append("| 코퍼스 | 프로브 | 밀어냄 | 라벨됨 | damage | 자기회수 | 창 이탈 |")
        lines.append("|---|---|---|---|---|---|---|")
        for proj, d in disp.items():
            # damage = an answer deleted; 창 이탈 = an answer pushed past the
            # limit with nothing deleted. Different failures, reported side by
            # side — the second is what read as "damage 0" on 2026-09-23.
            exits = (f"{d['window_exits']}/{d['carried']}" if d.get("carried")
                     else "—")
            lines.append(f"| {proj} | {d['probes']} | {d['displaced']} | "
                         f"{d['labelled']} | {d['damage']} | {d['self_retrieval']} "
                         f"| {exits} |")
        pending = {p: d["unlabelled"] for p, d in disp.items() if d["unlabelled"]}
        if pending:
            lines.append("")
            lines.append("**손이 필요한 것 — 라벨 없는 밀어냄**")
            for proj, ids in pending.items():
                lines.append(f"- `{proj}`: {len(ids)}건 — {', '.join(ids[:8])}"
                             + (" …" if len(ids) > 8 else ""))
            lines.append("")
            lines.append("판정 기준은 라벨 파일의 `criterion` 을 그대로 쓸 것. "
                         "실물(브랜치·커밋·파일)이 겹치지 않으면 같은 일이 아니다.")
    return "\n".join(lines), regressed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-freeze", action="store_true",
                    help="스냅샷을 다시 뜨지 않고 지난 것을 재사용")
    ap.add_argument("--quick", action="store_true", help="밀어냄 감사를 건너뛴다")
    ap.add_argument("--no-holdout", action="store_true",
                    help="이전 주기 이후 레코드로 좁히지 않고 코퍼스 전체를 잰다")
    ap.add_argument("--if-stale", type=int, metavar="DAYS",
                    help="마지막 주기가 이보다 최근이면 아무것도 하지 않고 끝낸다")
    ap.add_argument("--defer-if-busy", type=int, metavar="MINUTES", nargs="?",
                    const=20,
                    help="최근 이 시간 안에 qa 활동이 있으면 물러난다 (기본 20분)")
    ap.add_argument("--sample", type=int, default=600,
                    help="밀어냄 감사 프로브 수")
    args = ap.parse_args()

    CYCLE_DIR.mkdir(parents=True, exist_ok=True)
    prev = previous()

    # Two ways to say "not now", so this can sit in a scheduler that fires
    # every hour of the window the laptop is reliably on.
    if args.if_stale is not None and prev:
        try:
            age = (date.today() - date.fromisoformat(prev["date"])).days
        except (KeyError, ValueError):
            age = 10**6
        if age < args.if_stale:
            print(f"마지막 주기가 {age}일 전 — {args.if_stale}일이 안 됐으므로 넘어간다")
            return 0
    if args.defer_if_busy:
        busy = _recent_qa_minutes()
        if busy is not None and busy < args.defer_if_busy:
            print(f"{busy}분 전에 qa 활동이 있었다 — 작업 중으로 보고 물러난다 "
                  "(검색 백엔드를 두고 다투지 않는다)")
            return 0
    os.environ.setdefault("HYBRID_SEARCH_IN_FLIGHT", "0")

    config = SNAP / "config.toml"
    if not args.no_freeze or not config.is_file():
        print("· 인덱스를 얼린다")
        config = freeze()

    today = date.today().isoformat()
    work = CYCLE_DIR / f"raw-{today}"
    work.mkdir(parents=True, exist_ok=True)

    since = None
    if prev and not args.no_holdout:
        since = prev.get("date")

    now: dict = {
        "date": today,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_sha": _sh("git", "rev-parse", "--short", "HEAD") or "unknown",
        "dirty": bool(_sh("git", "status", "--porcelain")),
        "holdout_since": since,
    }
    # A same-day rerun's "previous" raw dir is this run's own — no carry.
    prev_raw = (CYCLE_DIR / f"raw-{prev['date']}"
                if prev and prev.get("date") != today else None)
    now.update(measure(config, work, since, args.quick, args.sample, prev_raw))

    text, regressed = report(now, prev)
    (CYCLE_DIR / f"{today}.json").write_text(
        json.dumps(now, ensure_ascii=False, indent=2), encoding="utf-8")
    (CYCLE_DIR / f"{today}.md").write_text(text, encoding="utf-8")
    print()
    print(text)
    print()
    print(f"기록: {CYCLE_DIR / (today + '.json')}")
    if now["dirty"]:
        print("⚠ 작업 트리가 깨끗하지 않다 — 이 판독이 어느 코드의 것인지 불분명하다.")
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
