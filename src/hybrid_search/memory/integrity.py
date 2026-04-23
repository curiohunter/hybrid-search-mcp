"""Memory-Layer integrity pass — staleness, semantic dedup, archive tier.

Runs at the tail of every reindex (after auto-prune, after wiki cleanup).
Three deterministic sub-passes, each with its own archive record:

1. **Staleness** — qa_log whose ``## Top results`` paths are *all* gone
   from the store DB is archived. Mirrors the wiki-orphan detector.
2. **Semantic dedup** — for every pair of qa_log chunks with cosine
   similarity ≥ ``dedup_threshold`` (default 0.90), the older is archived.
   Embeddings come from the already-indexed vectors — no new LLM calls.
3. **Archive purge** — entries under ``.hybrid-search/qa-archive/``
   older than 30 days are unlinked permanently.

All prunes **move** files to ``<project>/.hybrid-search/qa-archive/
YYYY/MM/<original-stem>.md`` so a user who regrets a pass can run
``qa-restore``. Archive is not indexed by the scanner (the existing
ignore rules cover ``.hybrid-search/*`` except ``.hybrid-search/qa/``).
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


QA_DIRNAME = ".hybrid-search/qa"
QA_ARCHIVE_DIRNAME = ".hybrid-search/qa-archive"

DEFAULT_DEDUP_THRESHOLD = 0.90
DEFAULT_ARCHIVE_TTL_DAYS = 30


# ── shared helpers ────────────────────────────────────────────────────

_RESULT_HEADER_RE = re.compile(
    r"^###\s+\d+\.\s+`([^`:]+)(?::[^`]*)?`", re.MULTILINE
)


def _extract_result_paths(body: str) -> list[str]:
    """Pull relative paths from ``### N. \\`path\\``` lines in ``## Top results``."""
    return _RESULT_HEADER_RE.findall(body)


@dataclass
class DuplicatePair:
    kept: Path           # the newer qa file retained in place
    archived: Path       # the older qa file moved to qa-archive
    similarity: float


@dataclass
class IntegrityReport:
    stale_archived: list[Path] = field(default_factory=list)
    dedup_pairs: list[DuplicatePair] = field(default_factory=list)
    archive_purged: list[Path] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def total_archived(self) -> int:
        return len(self.stale_archived) + len(self.dedup_pairs)


# ── archive tier ──────────────────────────────────────────────────────


def archive_file(path: Path, project_root: Path) -> Path | None:
    """Move ``path`` from qa/ into qa-archive/ preserving the YYYY/MM tree.

    Returns the new archive path, or None on failure. Safe if the source
    is missing. Overwrites an existing archive target (rare — would only
    happen if the same stem was already archived on an earlier pass).
    """
    try:
        rel = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        logger.debug("archive_file: %s is outside %s", path, project_root)
        return None

    rel_parts = rel.parts
    if len(rel_parts) < 2 or rel_parts[0] != ".hybrid-search" or rel_parts[1] != "qa":
        logger.debug("archive_file: %s not under %s", path, QA_DIRNAME)
        return None

    # Rebuild qa-archive/<rest-after-qa>/
    tail = Path(*rel_parts[2:])
    target = project_root / QA_ARCHIVE_DIRNAME / tail
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Collision-safe: if a file already exists at target (previous
        # archive), append a counter so history isn't silently overwritten.
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for i in range(1, 100):
                candidate = target.with_name(f"{stem}.{i}{suffix}")
                if not candidate.exists():
                    target = candidate
                    break
        shutil.move(str(path), str(target))
        return target
    except (OSError, shutil.Error) as exc:
        logger.debug("archive_file: failed %s → %s: %s", path, target, exc)
        return None


def purge_old_archive(
    project_root: Path,
    *,
    max_age_days: int = DEFAULT_ARCHIVE_TTL_DAYS,
    now: datetime | None = None,
) -> list[Path]:
    """Unlink archived qa files older than ``max_age_days``. Returns removed paths."""
    archive_root = project_root / QA_ARCHIVE_DIRNAME
    if not archive_root.is_dir():
        return []

    now = now or datetime.now(timezone.utc)
    cutoff_ts = (now.timestamp()) - max_age_days * 86400.0
    removed: list[Path] = []
    for path in archive_root.rglob("*.md"):
        try:
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    # Remove empty YYYY/MM dirs so the tree doesn't accumulate skeletons.
    for month_dir in sorted(archive_root.rglob("*"), reverse=True):
        if month_dir.is_dir():
            try:
                month_dir.rmdir()
            except OSError:
                continue
    return removed


def restore_archived(project_root: Path, identifier: str) -> Path | None:
    """Move an archived entry back into qa/ given a stem / hash prefix / id.

    Accepts any of:
    - full stem: ``22-104510-2da65337``
    - hash prefix (≥ 4 chars): ``2da52337`` (matches any archived file ending in that hash)
    - friendly id: ``2026-04-22-104510-2da65337`` (from ``qa-list``)
    """
    archive_root = project_root / QA_ARCHIVE_DIRNAME
    if not archive_root.is_dir():
        return None
    token = identifier.strip()
    if not token:
        return None

    candidates: list[Path] = []
    token_hash_only = len(token) >= 4 and re.fullmatch(r"[0-9a-f]+", token)
    for path in archive_root.rglob("*.md"):
        stem = path.stem
        if stem == token:
            candidates.append(path)
            break
        if token_hash_only and stem.endswith("-" + token):
            candidates.append(path)
            break
        # Friendly id form
        parts = path.parts
        if len(parts) >= 3 and parts[-3].isdigit() and parts[-2].isdigit():
            friendly = f"{parts[-3]}-{parts[-2]}-{stem}"
            if friendly == token:
                candidates.append(path)
                break

    if not candidates:
        return None

    src = candidates[0]
    try:
        rel = src.relative_to(archive_root)
    except ValueError:
        return None
    dst = project_root / QA_DIRNAME / rel
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return dst
    except (OSError, shutil.Error):
        return None


# ── M1: qa staleness ──────────────────────────────────────────────────


def detect_stale_qa(project_root: Path, indexed_paths: set[str]) -> list[Path]:
    """Return qa files whose every ``## Top results`` path is absent from the DB.

    Follows the same "all refs dead → archive" rule as wiki orphan detection.
    Missing ``## Top results`` (e.g. qa saved with ``trigger=stop_hook`` that
    never ran MCP) is treated as no-refs → the qa is preserved.
    """
    qa_root = project_root / QA_DIRNAME
    if not qa_root.is_dir():
        return []

    stale: list[Path] = []
    for path in qa_root.rglob("*.md"):
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs = _extract_result_paths(body)
        if not refs:
            continue
        if all(ref not in indexed_paths for ref in refs):
            stale.append(path)
    return stale


# ── M2: semantic dedup via existing vector index ──────────────────────


def _cosine(a, b) -> float:
    import numpy as np

    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na = float((a * a).sum()) ** 0.5
    nb = float((b * b).sum()) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return float((a * b).sum()) / (na * nb)


def detect_semantic_duplicates(
    qa_log_chunks: list[tuple[str, str, float]],
    get_vector,
    *,
    threshold: float = DEFAULT_DEDUP_THRESHOLD,
) -> list[tuple[str, str, float]]:
    """Pairwise-cluster qa_log chunks on cosine similarity.

    Parameters
    ----------
    qa_log_chunks
        List of ``(chunk_id, file_path, mtime)`` tuples for every qa_log
        chunk in the index. ``mtime`` is used to pick which member of a
        duplicate pair to retain (newer wins).
    get_vector
        Callable ``chunk_id -> np.ndarray | None``. Typically the bound
        method ``VectorEngine.get_vector``. Chunks whose vectors aren't
        retrievable are skipped silently (they'll be checked on the next
        reindex).
    threshold
        Cosine similarity at or above which two chunks are considered
        duplicates. Pair members below this threshold are untouched.

    Returns a list of ``(archive_path, kept_path, similarity)`` tuples —
    the older file of each surviving duplicate pair. Transitive
    closure: if A~B~C all cluster, only the newest of the three is kept.
    """
    # (chunk_id → path, mtime, vec)
    vectors: dict[str, tuple[str, float, object]] = {}
    for cid, path, mtime in qa_log_chunks:
        vec = get_vector(cid)
        if vec is None:
            continue
        vectors[cid] = (path, mtime, vec)

    ids = list(vectors.keys())
    if len(ids) < 2:
        return []

    # Union-find to cluster transitively.
    parent: dict[str, str] = {cid: cid for cid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Record similarity of the edge that joined each pair for reporting.
    edge_sim: dict[tuple[str, str], float] = {}

    for i in range(len(ids)):
        id_a = ids[i]
        _, _, vec_a = vectors[id_a]
        for j in range(i + 1, len(ids)):
            id_b = ids[j]
            _, _, vec_b = vectors[id_b]
            sim = _cosine(vec_a, vec_b)
            if sim >= threshold:
                union(id_a, id_b)
                edge_sim[(id_a, id_b)] = sim

    # Group by cluster root.
    clusters: dict[str, list[str]] = {}
    for cid in ids:
        clusters.setdefault(find(cid), []).append(cid)

    victims: list[tuple[str, str, float]] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        # Sort by mtime ascending so the last one is the newest.
        members.sort(key=lambda c: vectors[c][1])
        keeper = members[-1]
        keeper_path = vectors[keeper][0]
        # For each older member, report against the keeper with the
        # highest similarity edge we've seen.
        for older in members[:-1]:
            older_path = vectors[older][0]
            sim = max(
                edge_sim.get((older, keeper), 0.0),
                edge_sim.get((keeper, older), 0.0),
            )
            if sim == 0.0:
                # Not directly connected — recompute.
                sim = _cosine(vectors[older][2], vectors[keeper][2])
            victims.append((older_path, keeper_path, sim))
    return victims


# ── orchestration ─────────────────────────────────────────────────────


@dataclass
class IntegrityConfig:
    enabled: bool = True
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD
    archive_ttl_days: int = DEFAULT_ARCHIVE_TTL_DAYS


def run_integrity_pass(
    project_root: Path,
    *,
    indexed_paths: set[str] | None = None,
    qa_log_chunks: Iterable[tuple[str, str, float]] | None = None,
    get_vector=None,
    config: IntegrityConfig | None = None,
) -> IntegrityReport:
    """Single entry point — called by the reindex tail and the CLI.

    Any of the inputs can be ``None`` to skip that sub-pass:
    - ``indexed_paths = None`` → skip staleness detection
    - ``qa_log_chunks = None`` or ``get_vector = None`` → skip dedup

    Archive purge always runs if enabled (cheap, independent of the above).
    """
    cfg = config or IntegrityConfig()
    report = IntegrityReport()
    if not cfg.enabled:
        return report

    # M1 staleness
    if indexed_paths is not None:
        for stale in detect_stale_qa(project_root, indexed_paths):
            archived = archive_file(stale, project_root)
            if archived is not None:
                report.stale_archived.append(archived)
            else:
                report.errors.append((stale, "archive_move_failed"))

    # M2 dedup — skipped when we have fewer than 2 qa_log chunks.
    if qa_log_chunks is not None and get_vector is not None:
        qa_list = list(qa_log_chunks)
        pairs = detect_semantic_duplicates(
            qa_list,
            get_vector,
            threshold=cfg.dedup_threshold,
        )
        for older_path_str, keeper_path_str, sim in pairs:
            older_path = Path(older_path_str)
            if not older_path.is_absolute():
                older_path = project_root / older_path
            archived = archive_file(older_path, project_root)
            if archived is not None:
                report.dedup_pairs.append(
                    DuplicatePair(
                        kept=Path(keeper_path_str),
                        archived=archived,
                        similarity=sim,
                    )
                )
            else:
                report.errors.append((older_path, "archive_move_failed"))

    # Archive TTL purge — always fires.
    report.archive_purged = purge_old_archive(
        project_root,
        max_age_days=cfg.archive_ttl_days,
    )
    return report


# ── stats (M5) ────────────────────────────────────────────────────────


def count_active(project_root: Path) -> int:
    root = project_root / QA_DIRNAME
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.md"))


def count_archived(project_root: Path) -> int:
    root = project_root / QA_ARCHIVE_DIRNAME
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.md"))


def count_recent_archive_additions(
    project_root: Path,
    *,
    window_days: int = 7,
    now: datetime | None = None,
) -> int:
    """Approximate count of files moved into archive in the last N days.

    Uses filesystem mtime as a proxy — ``shutil.move`` preserves mtime
    from the source, so this reflects when the qa file was first written
    rather than when it was archived. Good enough to show "churn" in
    qa-stats; callers wanting perfect fidelity should supplement with a
    jsonl audit log (deferred to v0.5 if needed).
    """
    root = project_root / QA_ARCHIVE_DIRNAME
    if not root.is_dir():
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_days * 86400.0
    count = 0
    for path in root.rglob("*.md"):
        try:
            if path.stat().st_mtime >= cutoff:
                count += 1
        except OSError:
            continue
    return count
