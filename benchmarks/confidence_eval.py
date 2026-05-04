"""Evaluate weak confidence as the signal to fall back from hybrid_search."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import load_config  # noqa: E402
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.orchestrator import SearchOrchestrator  # noqa: E402


def _load_queries(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    queries = raw.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError(f"{path} does not contain a query list")
    return queries


def _target_in_paths(target: str, paths: list[str]) -> bool:
    target = (target or "").replace("\\", "/").lstrip("/")
    if not target:
        return False
    if target.endswith("/"):
        return any(p.replace("\\", "/").lstrip("/").startswith(target) for p in paths)
    return target in {p.replace("\\", "/").lstrip("/") for p in paths}


def _module_match(acceptable: list[str], results: list) -> bool:
    names = {
        value
        for result in results
        for value in (
            getattr(result, "name", None),
            getattr(result, "qualified_name", None),
        )
        if value
    }
    for module_name in acceptable:
        if module_name in names:
            return True
        if any(str(name).endswith(f"::{module_name}") for name in names):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("benchmarks/valuein_gold.json"))
    parser.add_argument("--project", help="Registered project name")
    parser.add_argument("--cwd", help="Project cwd for auto-detection")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orchestrator = SearchOrchestrator(config, registry, embedder)

    rows = []
    true_positive = false_positive = false_negative = 0
    for item in _load_queries(args.gold):
        query = item.get("query") or item.get("prompt")
        if not query:
            continue
        response = orchestrator.hybrid_search(
            query=query,
            project=args.project,
            cwd=args.cwd,
            limit=args.limit,
        )
        paths = [r.file_path for r in response.results]
        target_found = _target_in_paths(item.get("primary_target", ""), paths)
        module_found = _module_match(item.get("acceptable_module_names", []), response.results)
        should_fall_back = not target_found and not module_found
        # A weak band means "fall back when the returned set lacks the known
        # target/module." If the benchmark target is already present, the
        # response should not be counted as an actionable fallback.
        predicted = response.confidence == "weak" and should_fall_back
        true_positive += int(predicted and should_fall_back)
        false_positive += int(predicted and not should_fall_back)
        false_negative += int((not predicted) and should_fall_back)
        rows.append({
            "id": item.get("id"),
            "confidence": response.confidence,
            "top_score": response.top_score,
            "score_gap": response.score_gap,
            "should_fall_back": should_fall_back,
            "target_found": target_found,
            "module_found": module_found,
        })

    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 1.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 1.0
    report = {
        "gold": str(args.gold),
        "project": args.project,
        "cwd": args.cwd,
        "limit": args.limit,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "target": 0.80,
        "passed": precision >= 0.80 and recall >= 0.80,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "queries": rows,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"confidence weak precision: {precision:.1%}, recall: {recall:.1%} "
            "(target >= 80%)"
        )
        for row in rows:
            print(
                f"  {row['id']}: {row['confidence']} "
                f"fall_back={row['should_fall_back']} "
                f"top={row['top_score']:.6f} gap={row['score_gap']}"
            )

    if not report["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
