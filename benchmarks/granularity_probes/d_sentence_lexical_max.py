"""(d) 문장 단위 어휘 최대값 — 인덱스 변경 0, 임베딩 0.

(b)가 산 것은 "청크 전체가 아니라 그 안의 가장 잘 맞는 한 조각으로 점수를
매기는 것"이다. ColBERT MaxSim의 성질이고, 임베딩이 아니라 어휘로도 같은
성질을 흉내낼 수 있다: 청크 점수 = max(문장별 질의어 커버리지).

라운드 4에서 되돌린 chunk-level lexical rerank와 정반대다. 그건 청크 전체를
haystack으로 삼아 커버리지를 쟀고, 긴 청크일수록 유리했다 — 희석을 고치는
게 아니라 보상했다. 여기서는 max를 취하므로 길이가 이득이 되지 않는다.
"""
import json, os, pathlib, re, sys
sys.path.insert(0, "src")
os.environ["HYBRID_SEARCH_IN_FLIGHT"] = "0"
from dataclasses import replace as dc_replace

from hybrid_search.config import load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.memory.quality import query_tokens
from hybrid_search.project import ProjectRegistry
from hybrid_search.search import orchestrator as O

SNAP, GOLD, MODE = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


def sentence_max_coverage(tokens: set[str], text: str) -> float:
    if not tokens:
        return 0.0
    best = 0.0
    for s in SPLIT.split(text or ""):
        s = s.strip().casefold()
        if len(s) < 8:
            continue
        cov = sum(1 for t in tokens if t in s) / len(tokens)
        if cov > best:
            best = cov
    return best


def patch(mode: str, weight: float = 0.6):
    orig = O._merge_conv_results

    def wrapped(chunk_results, conv_results, limit, **kw):
        toks = query_tokens(wrapped.query)
        scored = []
        for i, r in enumerate(conv_results):
            cov = sentence_max_coverage(toks, (r.content or "") + " " + (r.snippet or ""))
            key = -cov if mode == "pure" else -(r.rrf_score * (1.0 + weight * cov))
            scored.append((key, i, r))
        scored.sort()
        slots = sorted((r.rrf_score for r in conv_results), reverse=True)
        reordered = [dc_replace(r, rrf_score=s) for s, (_, _, r) in zip(slots, scored)]
        return orig(chunk_results, reordered, limit, **kw)

    wrapped.query = ""
    O._merge_conv_results = wrapped
    return wrapped


cfg = load_config(SNAP / "config.toml")
orch = O.SearchOrchestrator(config=cfg, registry=ProjectRegistry(cfg.global_dir),
                            embedder=Embedder(cfg.embedding, cfg.models_dir))
gold = json.loads(GOLD.read_text(encoding="utf-8"))
hook = patch(MODE) if MODE != "off" else None

ranks = []
for q in gold["queries"]:
    if hook:
        hook.query = q["query"]
    resp = orch.hybrid_search(query=q["query"], cwd=gold["project_path"], limit=10)
    first = None
    for i, r in enumerate(resp.results, 1):
        if any(p in ((r.content or "") + (r.snippet or "")) for p in q["any_of"]):
            first = i
            break
    ranks.append((q["id"], first))
n = len(ranks)
found = sum(1 for _, r in ranks if r) / n
top3 = sum(1 for _, r in ranks if r and r <= 3) / n
mrr = sum(1.0 / r for _, r in ranks if r) / n
print(f"mode={MODE:6s} answer_found={found:.2f} answer_in_top3={top3:.2f} mrr={mrr:.3f}   "
      + " ".join(f"{i}:{r or '-'}" for i, r in ranks))
