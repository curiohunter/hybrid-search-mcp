"""(c) 반환 폭 — 인덱스 그대로, 히트 턴의 이웃 턴까지 합쳐서 채점한다.

SGMem ablation은 turn-level 반환이 가장 약하고 session-level이 강하다고
말한다. 우리 반환 단위가 턴이다. 색인을 건드리지 않고 반환만 넓히면
얼마가 공짜로 들어오는지 잰다.

이웃 = 같은 세션(file_id)에서 turn_index 순으로 인접한, 살아남은 청크.
품질 게이트가 턴을 버려도 turn_index는 전진하므로 번호 산술이 아니라
정렬 순서로 잡는다.
"""
import json, os, pathlib, sqlite3, sys
from collections import defaultdict

sys.path.insert(0, "src")
os.environ["HYBRID_SEARCH_IN_FLIGHT"] = "0"
from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator

SNAP = pathlib.Path(sys.argv[1])
GOLD = pathlib.Path(sys.argv[2])
WINDOWS = [int(x) for x in sys.argv[3].split(",")]

cfg = load_config(SNAP / "config.toml")
reg = ProjectRegistry(cfg.global_dir)
emb = Embedder(cfg.embedding, cfg.models_dir)
orch = SearchOrchestrator(config=cfg, registry=reg, embedder=emb)
gold = json.loads(GOLD.read_text(encoding="utf-8"))
pinfo = reg.get_by_path(gold["project_path"])
conn = sqlite3.connect(f"file:{pathlib.Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)

# session -> ordered chunk ids + content
order: dict[str, list[str]] = defaultdict(list)
body: dict[str, str] = {}
for cid, fid, content in conn.execute(
    "select id, file_id, content from chunks where project_id=? and node_type='conv_turn'",
    (pinfo.id,)
):
    order[fid].append(cid)
    body[cid] = content or ""
pos: dict[str, tuple[str, int]] = {}
for fid, ids in order.items():
    ids.sort()  # ids embed a zero-padded turn index, so this is turn order
    for i, cid in enumerate(ids):
        pos[cid] = (fid, i)


def window_text(cid: str, k: int) -> str:
    if k <= 0 or cid not in pos:
        return ""
    fid, i = pos[cid]
    ids = order[fid]
    return "\n".join(body[c] for c in ids[max(0, i - k): i + k + 1] if c != cid)


print(f"{'k':>2s} {'answer_found':>13s} {'answer_in_top3':>15s} {'mrr':>7s} "
      f"{'ctx_chars':>10s}")
summary = []
for k in WINDOWS:
    ranks, extra = [], 0
    for q in gold["queries"]:
        resp = orch.hybrid_search(query=q["query"], cwd=gold["project_path"], limit=10)
        first = None
        for i, r in enumerate(resp.results, 1):
            text = (r.content or "") + (r.snippet or "")
            if (r.node_type or "") == "conv_turn":
                w = window_text(r.chunk_id, k)
                extra += len(w)
                text += "\n" + w
            if any(p in text for p in q["any_of"]):
                first = i
                break
        ranks.append(first)
    n = len(ranks)
    found = sum(1 for r in ranks if r) / n
    top3 = sum(1 for r in ranks if r and r <= 3) / n
    mrr = sum(1.0 / r for r in ranks if r) / n
    summary.append({"k": k, "found": found, "top3": top3, "mrr": mrr,
                    "ranks": ranks, "extra_chars": extra})
    print(f"{k:>2d} {found:>13.2f} {top3:>15.2f} {mrr:>7.3f} {extra // max(n,1):>10d}")

print("\nper-query rank (k = " + ", ".join(str(w) for w in WINDOWS) + ")")
for i, q in enumerate(gold["queries"]):
    line = "  ".join(f"{str(s['ranks'][i] or '-'):>3s}" for s in summary)
    print(f"  {q['id']:4s} {line}  {q['query'][:40]}")
json.dump(summary, open(sys.argv[4], "w"), ensure_ascii=False, indent=1)
