"""(a) Set B를 부정/수치/기타로 가르고, 각 문항의 '리랭커 천장'을 잰다.

리랭커는 회수된 후보만 재정렬한다. 따라서 천장 = 목표 청크가 후보 풀에
들어와 있는가. 풀 밖이면 어떤 리랭커도 못 고친다.

부정 가설(NevIR)이 우리 코퍼스에 실제로 작동하는지도 같이 잰다:
질의-목표 코사인이 부정 문항에서 체계적으로 낮은가.
"""
import json, os, pathlib, sqlite3, sys
import numpy as np

sys.path.insert(0, "src")
os.environ["HYBRID_SEARCH_IN_FLIGHT"] = "0"
from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry
from hybrid_search.search import orchestrator as O
from hybrid_search.search.fusion import reciprocal_rank_fusion

SNAP = pathlib.Path(sys.argv[1])
CATEGORY = {
    "R1": "other",    "R2": "numeric",  "R3": "numeric",  "R4": "negation",
    "R5": "other",    "R6": "numeric",  "R7": "numeric",  "R8": "negation",
    "R9": "negation", "R10": "negation",
}

cfg = load_config(SNAP / "config.toml")
reg = ProjectRegistry(cfg.global_dir)
emb = Embedder(cfg.embedding, cfg.models_dir)
orch = O.SearchOrchestrator(config=cfg, registry=reg, embedder=emb)
VP = "/Users/ian/project/claude_project/valuein_homepage"
pinfo = reg.get_by_path(VP)
conn = sqlite3.connect(f"file:{pathlib.Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)
gold = json.loads((pathlib.Path.home() / ".hybrid-search/benchmarks/valuein_conv_gold_rawonly.json").read_text(encoding="utf-8"))


def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


rows = []
for q in gold["queries"]:
    phrase = q["any_of"][0]
    tgt = conn.execute(
        "select id, content from chunks where project_id=? and node_type='conv_turn' "
        "and instr(content, ?)>0", (pinfo.id, phrase)).fetchall()
    if not tgt:
        rows.append({"id": q["id"], "cat": CATEGORY[q["id"]], "note": "target chunk absent"})
        continue
    tgt_ids = {t[0] for t in tgt}
    chunk_text = tgt[0][1]
    # the answering sentence itself
    sent = next((s.strip() for s in chunk_text.replace("。", ".").split("\n")
                 if phrase in s), phrase)

    qv = emb.embed_texts([q["query"]])[0]
    cv, sv = emb.embed_texts([chunk_text[:4000], sent])

    depth = cfg.search.retrieval_depth_floor
    b, v, *_ = orch._search_single(pinfo, q["query"], qv, depth, None, [O._CONV_NODE_TYPE], None)
    fused = reciprocal_rank_fusion(b, v, k=cfg.search.rrf_k, bm25_weight=0.15)
    br = next((i for i, c in enumerate(b, 1) if c in tgt_ids), None)
    vr = next((i for i, c in enumerate(v, 1) if c in tgt_ids), None)
    fr = next((i for i, f in enumerate(fused, 1) if f.chunk_id in tgt_ids), None)
    rows.append({
        "id": q["id"], "cat": CATEGORY[q["id"]], "query": q["query"],
        "bm25": br, "vec": vr, "fused": fr,
        "in_pool_50": bool(fr and fr <= 50),
        "cos_chunk": round(cos(qv, cv), 4),
        "cos_sentence": round(cos(qv, sv), 4),
        "sentence": sent[:70],
    })

print(f"{'id':4s} {'cat':9s} {'bm25':>5s} {'vec':>5s} {'fused':>6s} {'pool50':>7s} "
      f"{'cos_chunk':>10s} {'cos_sent':>9s}")
for r in rows:
    if "note" in r:
        print(f"{r['id']:4s} {r['cat']:9s} {r['note']}")
        continue
    print(f"{r['id']:4s} {r['cat']:9s} {str(r['bm25']):>5s} {str(r['vec']):>5s} "
          f"{str(r['fused']):>6s} {str(r['in_pool_50']):>7s} "
          f"{r['cos_chunk']:>10.4f} {r['cos_sentence']:>9.4f}")

print("\n=== 범주별 요약 ===")
for cat in ("negation", "numeric", "other"):
    grp = [r for r in rows if r["cat"] == cat and "note" not in r]
    if not grp:
        continue
    inpool = sum(1 for r in grp if r["in_pool_50"])
    print(f"{cat:9s} n={len(grp)}  pool50={inpool}/{len(grp)}  "
          f"cos_chunk={np.mean([r['cos_chunk'] for r in grp]):.4f}  "
          f"cos_sent={np.mean([r['cos_sentence'] for r in grp]):.4f}")
json.dump(rows, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
