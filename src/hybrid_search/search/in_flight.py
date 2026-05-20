"""Query-time overlay for dirty worktree files.

The overlay is intentionally ephemeral: it reads the current worktree at
search time, scores locally, and never writes dirty content to persistent
indexes or embedding backends.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hybrid_search.config import IndexingConfig
from hybrid_search.index.scanner import (
    _build_ignore_spec,
    _is_indexable_path,
    parse_git_diff_name_status,
)


MAX_BYTES_PER_FILE = 200_000
SNIPPET_CHARS = 500
CONTENT_CHARS = 4_000
_TOKEN_RE = re.compile(r"[\w\uac00-\ud7a3]+", re.UNICODE)


@dataclass(frozen=True)
class InFlightFile:
    relative_path: str
    status: Literal["added", "modified", "renamed"]
    content: str
    content_hash: str
    truncated: bool = False


@dataclass(frozen=True)
class InFlightOverlay:
    files: list[InFlightFile]
    deleted_paths: set[str]


def collect_in_flight_overlay(
    project_root: Path,
    *,
    max_files: int = 50,
    max_bytes_per_file: int = MAX_BYTES_PER_FILE,
    indexing_config: IndexingConfig | None = None,
) -> InFlightOverlay:
    """Collect tracked dirty files from ``git diff --name-status HEAD``."""
    project_root = project_root.resolve()
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-status", "HEAD"],
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return InFlightOverlay(files=[], deleted_paths=set())

    if proc.returncode != 0:
        return InFlightOverlay(files=[], deleted_paths=set())

    diff = parse_git_diff_name_status(proc.stdout)
    deleted_paths = {_norm_rel(path) for path in diff.deleted if _norm_rel(path)}

    statuses: dict[str, Literal["added", "modified", "renamed"]] = {}
    for path in diff.added:
        rel = _norm_rel(path)
        if rel:
            statuses[rel] = "added"
    for path in diff.modified:
        rel = _norm_rel(path)
        if rel:
            statuses[rel] = "modified"
    for _old, new in diff.renamed:
        rel = _norm_rel(new)
        if rel:
            statuses[rel] = "renamed"

    config = indexing_config or IndexingConfig()
    ignore_spec = _build_ignore_spec(project_root, config)
    files: list[InFlightFile] = []
    for rel_path, status in list(statuses.items())[:max_files]:
        abs_path = project_root / rel_path
        if not _is_indexable_path(project_root, abs_path, rel_path, ignore_spec, config):
            continue

        loaded = _read_text_window(abs_path, max_bytes=max_bytes_per_file)
        if loaded is None:
            continue
        content, raw_hash, truncated = loaded
        files.append(
            InFlightFile(
                relative_path=rel_path,
                status=status,
                content=content,
                content_hash=raw_hash,
                truncated=truncated,
            )
        )

    return InFlightOverlay(files=files, deleted_paths=deleted_paths)


def score_in_flight_files(
    overlay: InFlightOverlay,
    *,
    query: str,
    project_name: str,
    project_id: str,
    limit: int = 5,
) -> list:
    """Score dirty files locally and return ``HybridResult`` instances."""
    from hybrid_search.search.orchestrator import HybridResult

    query_tokens = _tokens(query)
    if not query_tokens:
        return []

    scored: list[tuple[float, InFlightFile]] = []
    for item in overlay.files:
        score = _score_file(query_tokens, item)
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], pair[1].relative_path))
    results = []
    for rank, (score, item) in enumerate(scored[:limit], start=1):
        digest = hashlib.sha256(
            f"{item.relative_path}:{item.content_hash}".encode("utf-8")
        ).hexdigest()[:16]
        snippet = _snippet_for(item)
        results.append(
            HybridResult(
                chunk_id=f"ephemeral:{project_id}:{digest}",
                rrf_score=round(score, 6),
                bm25_rank=rank,
                vector_rank=None,
                file_path=item.relative_path,
                project=project_name,
                name=Path(item.relative_path).name,
                qualified_name=item.relative_path,
                node_type="in_flight_file",
                start_line=1,
                end_line=None,
                content=item.content[:CONTENT_CHARS],
                snippet=snippet,
                trust_meta="[in-flight dirty worktree; not indexed]",
            )
        )
    return results


def merge_in_flight_results(
    indexed_results: list,
    dirty_results: list,
    *,
    deleted_paths: set[str],
    limit: int,
) -> list:
    """Overlay dirty results and suppress stale/deleted same-file chunks."""
    dirty_paths = {r.file_path for r in dirty_results}
    protected_types = {"module", "module_card", "module_member", "memory_card"}
    merged = [(idx, result) for idx, result in enumerate(dirty_results)]

    for result in indexed_results:
        node_type = result.node_type or ""
        if result.file_path in deleted_paths:
            continue
        if result.file_path in dirty_paths and node_type not in protected_types:
            continue
        merged.append((len(merged), result))

    merged.sort(key=lambda pair: (-pair[1].rrf_score, pair[0]))
    return [result for _, result in merged[:limit]]


def _norm_rel(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def _read_text_window(
    file_path: Path,
    *,
    max_bytes: int,
) -> tuple[str, str, bool] | None:
    try:
        raw = file_path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None

    raw_hash = hashlib.sha256(raw).hexdigest()
    truncated = len(raw) > max_bytes
    window = raw[:max_bytes]
    try:
        text = window.decode("utf-8")
    except UnicodeDecodeError:
        text = window.decode("utf-8", errors="ignore")
    if not text.strip():
        return None
    return text, raw_hash, truncated


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text) if len(token) >= 2}


def _score_file(query_tokens: set[str], item: InFlightFile) -> float:
    path = item.relative_path.lower()
    path_tokens = _tokens(item.relative_path)
    content_tokens = _tokens(item.content)

    score = 0.0
    for token in query_tokens:
        if token in path:
            score += 0.014
        if token in path_tokens:
            score += 0.010
        if token in content_tokens:
            score += 0.006 if _identifierish(token) else 0.003

    if item.status == "added":
        score += 0.001
    return min(score, 0.08)


def _identifierish(token: str) -> bool:
    return (
        "_" in token
        or any(ch.isdigit() for ch in token)
        or any(ch.isupper() for ch in token)
    )


def _snippet_for(item: InFlightFile) -> str:
    body = item.content.strip().replace("\r\n", "\n")
    body = body[:SNIPPET_CHARS]
    suffix = " [truncated]" if item.truncated else ""
    return f"[in-flight] {item.relative_path}{suffix}\n{body}"
