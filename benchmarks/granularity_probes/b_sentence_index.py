"""(b) 문장 단위 색인이 실제로 목표를 꺼내는가 — small-to-big 반증 실험.

프로덕션·인덱스 변경 0. valuein의 conv_turn 청크 전량을 문장으로 쪼개
임베딩하고, 두 가지를 잰다:

  1. 정답 문장이 전체 문장 풀에서 몇 위인가            (매칭 단위의 효과)
  2. 문장 히트를 부모 턴으로 되돌려 랭킹하면 몇 위인가  (small-to-big 그대로)

2번이 현재 청크 단위 순위(실험 a)보다 좋아지지 않으면 small-to-big은
우리 코퍼스에서 답이 아니다.
"""
import json, os, pathlib, re, sqlite3, sys, time
import numpy as np

sys.path.insert(0, "src")
os.environ["HYBRID_SEARCH_IN_FLIGHT"] = "0"
from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry

SNAP = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

cfg = load_config(SNAP / "config.toml")
reg = ProjectRegistry(cfg.global_dir)
emb = Embedder(cfg.embedding, cfg.models_dir)
pinfo = reg.get_by_path("/Users/ian/project/claude_project/valuein_homepage")
conn = sqlite3.connect(f"file:{pathlib.Path(cfg.projects_dir)/pinfo.id/'store.db'}?mode=ro", uri=True)

SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")
_CODEISH = re.compile(r"^[\s`|+\-=_#*>]{0,4}(\$|>|\||```|https?://|/[A-Za-z0-9_./-]+$)")


def sentences(text: str):
    for s in SPLIT.split(text or ""):
        s = s.strip(" \t`|-")
        if not (12 <= len(s) <= 400):
            continue
        if not re.search(r"[가-힣A-Za-z]", s):
            continue
        if _CODEISH.match(s):
            continue
        yield s


vec_path, meta_path = OUT / "sent_vecs.npy", OUT / "sent_meta.json"
if vec_path.exists() and meta_path.exists():
    print("reusing cached sentence index", flush=True)
    vecs = np.load(vec_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
else:
    rows = conn.execute(
        "select id, content from chunks where project_id=? and node_type='conv_turn'",
        (pinfo.id,)).fetchall()
    seen: dict[str, int] = {}
    texts: list[str] = []
    parents: list[list[str]] = []
    for cid, content in rows:
        for s in sentences(content or ""):
            i = seen.get(s)
            if i is None:
                seen[s] = len(texts)
                texts.append(s)
                parents.append([cid])
            elif parents[i][-1] != cid:
                parents[i].append(cid)
    print(f"unique sentences: {len(texts):,} (from {len(rows):,} chunks)", flush=True)

    out = np.zeros((len(texts), emb.embedding_dim), dtype=np.float32)
    t0 = time.time()
    B = 100
    for i in range(0, len(texts), B):
        out[i:i + B] = emb.embed_texts(texts[i:i + B])
        if (i // B) % 50 == 0:
            done = i + B
            rate = done / max(time.time() - t0, 1e-9)
            print(f"  {done:,}/{len(texts):,}  {rate:.0f}/s  "
                  f"eta {(len(texts)-done)/max(rate,1e-9)/60:.1f}min", flush=True)
    vecs = out / np.clip(np.linalg.norm(out, axis=1, keepdims=True), 1e-9, None)
    np.save(vec_path, vecs)
    meta = {"texts": texts, "parents": parents}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"embedded in {(time.time()-t0)/60:.1f} min", flush=True)

texts, parents = meta["texts"], meta["parents"]
gold = json.loads((pathlib.Path.home() / ".hybrid-search/benchmarks/valuein_conv_gold_rawonly.json").read_text(encoding="utf-8"))
CATEGORY = {"R1": "other", "R2": "numeric", "R3": "numeric", "R4": "negation",
            "R5": "other", "R6": "numeric", "R7": "numeric", "R8": "negation",
            "R9": "negation", "R10": "negation"}

print(f"\n{'id':4s} {'cat':9s} {'sent_rank':>9s} {'parent_rank':>11s}  top-1 sentence")
results = []
for q in gold["queries"]:
    phrase = q["any_of"][0]
    qv = emb.embed_texts([q["query"]])[0]
    qv = qv / max(np.linalg.norm(qv), 1e-9)
    sims = vecs @ qv
    order = np.argsort(-sims)

    hit = [i for i, t in enumerate(texts) if phrase in t]
    sent_rank = next((r + 1 for r, i in enumerate(order) if i in set(hit)), None)

    # small-to-big: rank parents by their best sentence score
    tgt_parents = {p for i in hit for p in parents[i]}
    best: dict[str, float] = {}
    for i in order[:3000]:
        for p in parents[i]:
            if p not in best:
                best[p] = float(sims[i])
    ranked = sorted(best, key=lambda p: -best[p])
    parent_rank = next((r + 1 for r, p in enumerate(ranked) if p in tgt_parents), None)

    results.append({"id": q["id"], "cat": CATEGORY[q["id"]],
                    "sent_rank": sent_rank, "parent_rank": parent_rank,
                    "top1": texts[order[0]][:60]})
    print(f"{q['id']:4s} {CATEGORY[q['id']]:9s} {str(sent_rank):>9s} "
          f"{str(parent_rank):>11s}  {texts[order[0]][:60]}")

print("\n=== 요약 ===")
for cat in ("negation", "numeric", "other", None):
    grp = [r for r in results if cat is None or r["cat"] == cat]
    label = cat or "ALL"
    top3 = sum(1 for r in grp if r["parent_rank"] and r["parent_rank"] <= 3)
    top10 = sum(1 for r in grp if r["parent_rank"] and r["parent_rank"] <= 10)
    print(f"{label:9s} n={len(grp)}  parent_top3={top3}/{len(grp)}  parent_top10={top10}/{len(grp)}")
json.dump(results, open(OUT / "b_result.json", "w"), ensure_ascii=False, indent=1)
