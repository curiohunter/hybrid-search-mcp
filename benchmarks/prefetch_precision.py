"""Measure pre-fetch precision on content-heavy projects.

The metric is intentionally simple: run a query set through production
``hybrid_search`` and count the percentage of returned ``file_path`` values
that look like project code rather than content/binary corpus noise.

Example:
    python benchmarks/prefetch_precision.py \
      --gold benchmarks/valuein_gold.json \
      --project valuein_homepage \
      --cwd /path/to/valuein_homepage
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hybrid_search.config import (  # noqa: E402
    DEFAULT_CONTENT_EXCLUDE_EXTENSIONS,
    DEFAULT_CONTENT_ROOTS,
    load_config,
)
from hybrid_search.index.embedder import Embedder  # noqa: E402
from hybrid_search.project import ProjectRegistry  # noqa: E402
from hybrid_search.search.orchestrator import SearchOrchestrator  # noqa: E402


DEFAULT_CODE_ROOTS = (
    "app/",
    "src/",
    "components/",
    "hooks/",
    "services/",
    "lib/",
    "database/",
    "harness/",
    "supabase/",
    "tests/",
    "server/",
    "tools/",
    "types/",
)

DEFAULT_DOC_ROOTS = (
    "docs/",
    ".hybrid-search/wiki/",
    ".hybrid-search/memory/cards/",
    ".hybrid-search/qa/",
)

DEFAULT_CONTENT_SEGMENTS = (
    *DEFAULT_CONTENT_ROOTS,
    "마케팅",
    "운영",
    "인사",
    "분석",
    "재무",
)


def _load_queries(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    queries = raw.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError(f"{path} does not contain a query list")
    return queries


def _is_precise_path(
    file_path: str,
    code_roots: tuple[str, ...],
    doc_roots: tuple[str, ...],
) -> bool:
    rel = file_path.replace("\\", "/").lstrip("/")
    if rel in {"CLAUDE.md", "AGENTS.md", "README.md", "HANDOFF.md"}:
        return True
    if _under_content_segment(rel):
        return False
    if rel.startswith(code_roots) or rel.startswith(doc_roots):
        return True
    suffix = Path(rel).suffix.lower()
    if suffix in set(DEFAULT_CONTENT_EXCLUDE_EXTENSIONS):
        return False
    return "/" not in rel and suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".sql"}


def _under_content_segment(file_path: str) -> bool:
    segments = file_path.strip("/").split("/")[:-1]
    content_segments = {
        item.strip("/")
        for item in DEFAULT_CONTENT_SEGMENTS
        if item and "/" not in item.strip("/")
    }
    return any(segment in content_segments for segment in segments)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("benchmarks/valuein_gold.json"))
    parser.add_argument("--project", help="Registered project name")
    parser.add_argument("--cwd", help="Project cwd for auto-detection")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--code-roots",
        default=",".join(DEFAULT_CODE_ROOTS),
        help="Comma-separated roots counted as precise code hits",
    )
    parser.add_argument(
        "--doc-roots",
        default=",".join(DEFAULT_DOC_ROOTS),
        help="Comma-separated documentation roots counted as precise hits",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    config = load_config()
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    orchestrator = SearchOrchestrator(config, registry, embedder)

    code_roots = tuple(r.strip().rstrip("/") + "/" for r in args.code_roots.split(",") if r.strip())
    doc_roots = tuple(r.strip().rstrip("/") + "/" for r in args.doc_roots.split(",") if r.strip())

    rows = []
    total_hits = 0
    precise_hits = 0
    for item in _load_queries(args.gold):
        query = item.get("query") or item.get("prompt")
        if not query:
            continue
        response = orchestrator.hybrid_search(
            query,
            project=args.project,
            cwd=args.cwd,
            limit=args.limit,
        )
        paths = [r.file_path for r in response.results]
        precise = [
            p for p in paths
            if _is_precise_path(p, code_roots=code_roots, doc_roots=doc_roots)
        ]
        total_hits += len(paths)
        precise_hits += len(precise)
        rows.append({
            "id": item.get("id"),
            "query": query,
            "hits": len(paths),
            "precise_hits": len(precise),
            "precision": round(len(precise) / len(paths), 4) if paths else 0.0,
        })

    overall = precise_hits / total_hits if total_hits else 0.0
    report = {
        "gold": str(args.gold),
        "project": args.project,
        "cwd": args.cwd,
        "limit": args.limit,
        "precise_hits": precise_hits,
        "total_hits": total_hits,
        "precision": round(overall, 4),
        "target": 0.90,
        "passed": overall >= 0.90,
        "queries": rows,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"pre-fetch precision: {report['precision']:.1%} "
            f"({precise_hits}/{total_hits}, target >= 90%)"
        )
        for row in rows:
            print(f"  {row['id']}: {row['precision']:.1%} ({row['precise_hits']}/{row['hits']})")

    if not report["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
