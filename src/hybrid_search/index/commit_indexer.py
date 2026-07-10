"""Git commit-message indexing — the missing piece of feature genesis.

The "why" of a change lives in three places: conversations (indexed),
plan documents (indexed), and commit messages — which retrieval could
never see. A question like "confidence 판정 로직은 어떻게 바뀌었어" is
often answered best by the commit that changed it, so commits join the
memory lane as ``node_type="commit"`` chunks:

- one virtual file per project (``.git-history/commits``), one chunk per
  commit — commit hashes are stable ids, so delta indexing is a set
  difference, and rewritten history simply drops the orphaned hashes
- the commit date rides in the chunk's frontmatter, so the existing
  recency decay makes old commits fade exactly like old Q&A
- changed-file paths are embedded with the message, anchoring the commit
  to the code it touched (the same trick conversation turns use)
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from hybrid_search.config import Config
from hybrid_search.index.embedder import Embedder
from hybrid_search.project import ProjectRegistry, project_hash
from hybrid_search.search.bm25 import BM25Engine
from hybrid_search.search.vector import VectorEngine
from hybrid_search.storage.db import ChunkRecord, FileRecord, StoreDB
from hybrid_search.storage.indexes import IndexPaths, get_project_dir

logger = logging.getLogger(__name__)

COMMIT_NODE_TYPE = "commit"
COMMIT_REL_PATH = ".git-history/commits"
_COMMIT_ID_PREFIX = "commit:"

# Bound the corpus: enough history for "how did this evolve", cheap enough
# to embed once. Delta runs only embed commits not yet indexed.
_MAX_COMMITS = 500
_MAX_FILES_PER_COMMIT = 20
_MAX_BODY_CHARS = 2000

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_LOG_FORMAT = f"%H{_FIELD_SEP}%aI{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}"


@dataclass(frozen=True)
class CommitEntry:
    sha: str
    date: str  # ISO 8601 author date
    subject: str
    body: str
    files: tuple[str, ...] = field(default=())

    @property
    def text(self) -> str:
        """Embedding/BM25 input — message plus file anchors, no frontmatter."""
        parts = [self.subject]
        if self.body:
            parts.append(self.body[:_MAX_BODY_CHARS])
        if self.files:
            parts.append("files: " + " ".join(self.files[:_MAX_FILES_PER_COMMIT]))
        return "\n\n".join(parts)

    @property
    def content(self) -> str:
        """Stored/displayed content — frontmatter date feeds recency decay."""
        return f"---\ndate: {self.date}\n---\n\n{self.text}"


@dataclass
class CommitIndexingResult:
    project_id: str = ""
    project_name: str = ""
    commits_indexed: int = 0
    commits_removed: int = 0


def collect_commits(repo_root: Path, max_commits: int = _MAX_COMMITS) -> list[CommitEntry]:
    """Read recent commits via ``git log --name-only``. Empty on git failure.

    With ``--name-only`` git appends each commit's file list *after* the
    format record (and our record separator), so segment N's leading lines
    are commit N-1's files.
    """
    try:
        proc = subprocess.run(
            ["git", "log", f"-{max_commits}", f"--format={_LOG_FORMAT}", "--name-only"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    raw_entries: list[dict] = []
    for segment in proc.stdout.split(_RECORD_SEP):
        sep_at = segment.find(_FIELD_SEP)
        if sep_at < 0:
            # Trailing segment: only the previous commit's file list.
            if raw_entries and segment.strip():
                raw_entries[-1]["files"] = _parse_file_block(segment)
            continue
        record_start = segment.rfind("\n", 0, sep_at) + 1
        file_block, record = segment[:record_start], segment[record_start:]
        if raw_entries and file_block.strip():
            raw_entries[-1]["files"] = _parse_file_block(file_block)
        parts = record.split(_FIELD_SEP)
        if len(parts) < 4 or not parts[0].strip():
            continue
        raw_entries.append({
            "sha": parts[0].strip(),
            "date": parts[1].strip(),
            "subject": parts[2].strip(),
            "body": parts[3].strip(),
            "files": (),
        })

    return [CommitEntry(**e) for e in raw_entries]


def _parse_file_block(block: str) -> tuple[str, ...]:
    return tuple(
        line.strip() for line in block.splitlines() if line.strip()
    )[:_MAX_FILES_PER_COMMIT]


class CommitIndexer:
    """Delta-indexes commit messages into a project's unified store."""

    def __init__(self, config: Config, registry: ProjectRegistry, embedder: Embedder) -> None:
        self._config = config
        self._registry = registry
        self._embedder = embedder

    def index_commits(
        self, project_path: str, project_name: str | None = None
    ) -> CommitIndexingResult:
        abs_path = Path(project_path).resolve()
        pid = project_hash(str(abs_path))
        name = project_name or abs_path.name
        result = CommitIndexingResult(project_id=pid, project_name=name)

        entries = collect_commits(abs_path)
        if not entries:
            return result

        idx_paths = IndexPaths(get_project_dir(self._config.projects_dir, pid))
        idx_paths.ensure_dirs()
        db = StoreDB(idx_paths.store_db)
        bm25 = BM25Engine(idx_paths.tantivy_dir)
        vector = VectorEngine(idx_paths.vectors_dir, self._embedder.embedding_dim)

        try:
            file_id = f"{_COMMIT_ID_PREFIX}{pid}"
            existing = set(db.get_chunk_ids_by_file(file_id))
            desired = {f"{_COMMIT_ID_PREFIX}{e.sha}": e for e in entries}
            to_add = [(cid, e) for cid, e in desired.items() if cid not in existing]
            to_delete = [cid for cid in existing if cid not in desired]

            if not to_add and not to_delete:
                return result

            embeddings = (
                self._embedder.embed_texts([e.text for _, e in to_add])
                if to_add else None
            )

            with db.transaction() as conn:
                db.upsert_file(conn, FileRecord(
                    id=file_id, project_id=pid, relative_path=COMMIT_REL_PATH,
                    file_hash=entries[0].sha, language="git-history",
                    chunk_count=len(desired),
                ))
                if to_delete:
                    db.delete_chunks_by_ids(conn, to_delete)
                if to_add:
                    db.insert_chunks(conn, [
                        ChunkRecord(
                            id=cid, file_id=file_id, project_id=pid,
                            name=e.subject[:120],
                            qualified_name=f"commit:{e.sha[:10]}",
                            node_type=COMMIT_NODE_TYPE,
                            content=e.content, embedding_input=e.text,
                        )
                        for cid, e in to_add
                    ])

            if to_delete:
                bm25.delete_batch(to_delete)
                vector.remove_batch(to_delete)
            if to_add:
                for cid, e in to_add:
                    bm25.add(
                        chunk_id=cid, name=e.subject[:120],
                        qualified_name=f"commit:{e.sha[:10]}",
                        content=e.text, docstring=None,
                    )
                vector.add_batch([cid for cid, _ in to_add], embeddings)
            bm25.commit()
            vector.save()

            result.commits_indexed = len(to_add)
            result.commits_removed = len(to_delete)
        finally:
            db.close()

        logger.info(
            "Commit indexing for %s: %d added, %d removed",
            name, result.commits_indexed, result.commits_removed,
        )
        return result
