"""File discovery with delta detection — (size, mtime) prefilter + SHA256."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pathspec

from hybrid_search.config import IndexingConfig
from hybrid_search.storage.db import FileRecord, StoreDB

logger = logging.getLogger(__name__)

# Max file size for .gitignore / .hybrid-search-ignore to prevent reading huge files
_GITIGNORE_MAX_SIZE = 64 * 1024

# Q10: Project-specific ignore file. Walks upward from project_root to the
# nearest ``.git`` boundary (or filesystem root), so monorepos and
# sub-checkouts inherit parent-level exclusions automatically.
_HYBRID_SEARCH_IGNORE_FILENAME = ".hybrid-search-ignore"

# Safety cap on ancestor walk depth (well past any realistic monorepo nesting)
# — also defends against pathological symlink cycles that escape the filesystem
# root check.
_IGNORE_WALK_MAX_DEPTH = 32


# Basename patterns — file likely contains secrets regardless of location.
# Tight on purpose: avoid matching source files like ``PasswordReset.tsx``.
_SENSITIVE_BASENAME_PATTERNS: list[re.Pattern[str]] = [
    # .env variants: .env, .env.local, .env.production, .envrc, ...
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r"^\.envrc$", re.IGNORECASE),
    # cert/key by extension (defensive — most already fail the extension filter)
    re.compile(r".+\.(pem|key|p12|pfx|crt|cer|der|p8|jks)$", re.IGNORECASE),
    # credential containers with indexable extensions
    re.compile(r"^(.*[-_.])?credentials?\.(json|ya?ml|toml|env|ini|conf)$", re.IGNORECASE),
    re.compile(r"^(.*[-_.])?secrets?\.(json|ya?ml|toml|env|ini|conf)$", re.IGNORECASE),
    re.compile(r"^service[-_.]?account.*\.json$", re.IGNORECASE),
    # SSH private keys
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$"),
    # Shell credential stores
    re.compile(r"^\.netrc$", re.IGNORECASE),
    re.compile(r"^\.pgpass$", re.IGNORECASE),
    re.compile(r"^\.htpasswd$", re.IGNORECASE),
]

# Full-path patterns — sensitive only when at a particular location.
_SENSITIVE_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.ssh/id_(rsa|dsa|ecdsa|ed25519)", re.IGNORECASE),
    re.compile(r"(^|/)\.aws/credentials$", re.IGNORECASE),
    re.compile(r"(^|/)\.gcloud/.*credentials", re.IGNORECASE),
]


def _is_sensitive_file(file_path: Path) -> bool:
    """Return True if ``file_path`` looks like a secrets/credentials file.

    Matches on basename first (most signals live there) then on full path
    (for ``.ssh/id_*``-style location-dependent names). Conservative by
    design — source files like ``PasswordReset.tsx`` or ``TokenManager.ts``
    must pass through.
    """
    if any(p.search(file_path.name) for p in _SENSITIVE_BASENAME_PATTERNS):
        return True
    full = str(file_path).replace(os.sep, "/")
    return any(p.search(full) for p in _SENSITIVE_PATH_PATTERNS)


@dataclass(frozen=True)
class ScanResult:
    added: list[Path]
    changed: list[Path]
    deleted: list[str]  # relative paths of deleted files


@dataclass(frozen=True)
class GitDiffResult:
    """Changed paths from git diff."""

    added: list[str]
    modified: list[str]
    deleted: list[str]
    renamed: list[tuple[str, str]]


def scan_project(
    project_root: Path,
    project_id: str,
    db: StoreDB,
    config: IndexingConfig,
) -> ScanResult:
    """Scan project directory, detect added/changed/deleted files vs DB state."""
    project_root = project_root.resolve()
    ignore_spec = _build_ignore_spec(project_root, config)

    # Discover all files on disk
    disk_files: dict[str, Path] = {}
    for file_path in _walk_files(project_root, ignore_spec, config):
        rel = str(file_path.relative_to(project_root))
        disk_files[rel] = file_path

    # Get DB state
    db_paths = db.get_all_file_paths(project_id)
    db_files = {
        rec.relative_path: rec
        for rec in db.get_all_files(project_id)
    }

    added: list[Path] = []
    changed: list[Path] = []
    deleted: list[str] = []

    # Find added and changed
    for rel_path, abs_path in disk_files.items():
        if rel_path not in db_paths:
            added.append(abs_path)
            continue

        db_rec = db_files[rel_path]
        if _is_changed(abs_path, db_rec):
            changed.append(abs_path)

    # Find deleted
    for rel_path in db_paths:
        if rel_path not in disk_files:
            deleted.append(rel_path)

    logger.info(
        "Scan complete: %d added, %d changed, %d deleted (total on disk: %d)",
        len(added), len(changed), len(deleted), len(disk_files),
    )
    return ScanResult(added=added, changed=changed, deleted=deleted)


def scan_project_subset(
    project_root: Path,
    project_id: str,
    db: StoreDB,
    config: IndexingConfig,
    changed_paths: list[str],
    deleted_paths: list[str] | None = None,
) -> ScanResult:
    """Scan only a subset of project files, using DB state for delta detection.

    Paths are project-relative. Unsupported/ignored paths are dropped.
    """
    project_root = project_root.resolve()
    ignore_spec = _build_ignore_spec(project_root, config)
    db_paths = db.get_all_file_paths(project_id)
    db_files = {rec.relative_path: rec for rec in db.get_all_files(project_id)}

    added: list[Path] = []
    changed: list[Path] = []
    deleted: list[str] = []

    seen: set[str] = set()
    for raw_rel_path in changed_paths:
        rel_path = raw_rel_path.strip().replace("\\", "/")
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)

        abs_path = project_root / rel_path
        if not _is_indexable_path(project_root, abs_path, rel_path, ignore_spec, config):
            continue

        if rel_path not in db_paths:
            added.append(abs_path)
            continue

        db_rec = db_files[rel_path]
        if _is_changed(abs_path, db_rec):
            changed.append(abs_path)

    for raw_rel_path in deleted_paths or []:
        rel_path = raw_rel_path.strip().replace("\\", "/")
        if rel_path and rel_path in db_paths and rel_path not in deleted:
            deleted.append(rel_path)

    logger.info(
        "Subset scan complete: %d added, %d changed, %d deleted (candidate paths: %d)",
        len(added), len(changed), len(deleted), len(changed_paths),
    )
    return ScanResult(added=added, changed=changed, deleted=deleted)


def get_changed_files_from_git(
    project_root: Path,
    revspec: str = "HEAD~1..HEAD",
) -> GitDiffResult | None:
    """Return git-changed project-relative paths, or None if unavailable."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-status", revspec],
            cwd=str(project_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        logger.info("git diff unavailable for %s: %s", project_root, exc)
        return None

    if proc.returncode != 0:
        logger.info("git diff failed for %s (%s): %s", project_root, revspec, proc.stderr.strip())
        return None

    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split("\t")
        status = parts[0]
        kind = status[0]

        if kind == "A" and len(parts) >= 2:
            added.append(parts[1])
        elif kind == "M" and len(parts) >= 2:
            modified.append(parts[1])
        elif kind == "D" and len(parts) >= 2:
            deleted.append(parts[1])
        elif kind == "R" and len(parts) >= 3:
            renamed.append((parts[1], parts[2]))
            deleted.append(parts[1])
            added.append(parts[2])

    return GitDiffResult(added=added, modified=modified, deleted=deleted, renamed=renamed)


# Matches a YAML frontmatter block at the very start of a Markdown file:
#   ---\n
#   key: value ...
#   ---\n
# Supports both LF and CRLF line endings. Non-greedy so only the first block
# is stripped (a body-level `---` horizontal rule is preserved).
_FRONTMATTER_RE = re.compile(rb"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def _strip_frontmatter(raw: bytes) -> bytes:
    return _FRONTMATTER_RE.sub(b"", raw, count=1)


def compute_file_hash(file_path: Path) -> str:
    """Compute SHA256 hash of file content.

    For Markdown files, the YAML frontmatter is stripped before hashing so
    metadata-only edits (reviewed/status/tags) do not invalidate caches or
    trigger re-embedding. Non-Markdown files are hashed in streaming mode.
    """
    if file_path.suffix.lower() == ".md":
        raw = file_path.read_bytes()
        return hashlib.sha256(_strip_frontmatter(raw)).hexdigest()

    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_language(file_path: Path) -> str | None:
    """Detect language from file extension."""
    ext_map = {
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".py": "python",
        ".rs": "rust", ".go": "go", ".rb": "ruby",
        ".java": "java", ".kt": "kotlin",
        ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
        ".swift": "swift",
        ".sql": "sql", ".css": "css", ".scss": "scss",
        ".html": "html",
        ".md": "markdown",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml",
    }
    return ext_map.get(file_path.suffix.lower())


def _is_changed(abs_path: Path, db_rec: FileRecord) -> bool:
    """Fast prefilter: check (size, mtime) first, then SHA256 if needed.

    Crash recovery: file_hash="" means a previous indexing run crashed mid-write
    (the file record was created with placeholder hash, but the final update never
    committed). Always re-process these files.
    """
    # Crash recovery: empty hash = partial write from crashed indexing
    if not db_rec.file_hash:
        logger.info("Partial write detected for %s, scheduling re-index", db_rec.relative_path)
        return True

    try:
        stat = abs_path.stat()
    except OSError:
        return True

    # Fast path: size or mtime differ → likely changed, compute hash to confirm
    if db_rec.file_size is not None and stat.st_size != db_rec.file_size:
        return compute_file_hash(abs_path) != db_rec.file_hash

    if db_rec.file_mtime is not None:
        disk_mtime = str(stat.st_mtime)
        if disk_mtime != db_rec.file_mtime:
            return compute_file_hash(abs_path) != db_rec.file_hash

    # Size and mtime match → skip
    return False


def _walk_files(
    project_root: Path,
    ignore_spec: pathspec.PathSpec,
    config: IndexingConfig,
) -> list[Path]:
    """Walk directory tree, respecting ignore patterns and extension filters."""
    max_size = config.max_file_size_kb * 1024
    extensions = set(config.supported_extensions)
    results: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(project_root, followlinks=False):
        dir_path = Path(dirpath)
        rel_dir = dir_path.relative_to(project_root)

        # Prune ignored directories in-place
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and not ignore_spec.match_file(str(rel_dir / d) + "/")
        ]

        for fname in filenames:
            file_path = dir_path / fname
            rel_path = str(file_path.relative_to(project_root))

            # Check ignore patterns
            if ignore_spec.match_file(rel_path):
                continue

            # Check extension
            if file_path.suffix.lower() not in extensions:
                continue

            # Skip files that look like secrets (silent — avoids BM25 leakage)
            if _is_sensitive_file(file_path):
                logger.debug("Skipping sensitive file: %s", rel_path)
                continue

            # Check symlink — resolve and verify it's within project root
            if file_path.is_symlink():
                try:
                    resolved = file_path.resolve()
                    if not str(resolved).startswith(str(project_root)):
                        continue
                except OSError:
                    continue

            # Check file size
            try:
                if file_path.stat().st_size > max_size:
                    continue
            except OSError:
                continue

            results.append(file_path)

    return results


def _is_indexable_path(
    project_root: Path,
    file_path: Path,
    rel_path: str,
    ignore_spec: pathspec.PathSpec,
    config: IndexingConfig,
) -> bool:
    """Check whether a single path would be included in indexing."""
    if ignore_spec.match_file(rel_path):
        return False

    if file_path.suffix.lower() not in set(config.supported_extensions):
        return False

    if _is_sensitive_file(file_path):
        return False

    try:
        if not file_path.exists() or not file_path.is_file():
            return False
    except OSError:
        return False

    if file_path.is_symlink():
        try:
            resolved = file_path.resolve()
            if not str(resolved).startswith(str(project_root)):
                return False
        except OSError:
            return False

    try:
        if file_path.stat().st_size > config.max_file_size_kb * 1024:
            return False
    except OSError:
        return False

    return True


def _collect_hybrid_search_ignore_patterns(project_root: Path) -> list[str]:
    """Walk upward from ``project_root`` collecting ``.hybrid-search-ignore`` lines.

    Stops after processing the directory that contains ``.git`` (git repo
    boundary) or at the filesystem root — whichever comes first. Patterns
    from ancestor files are included so a monorepo root can declare shared
    exclusions that apply to every sub-project scanned from within.

    Order: nearest-first (project_root, then parent, grandparent, …). Later
    patterns can override earlier ones via gitignore ``!`` negation.
    """
    collected: list[str] = []
    current = project_root.resolve()
    for _ in range(_IGNORE_WALK_MAX_DEPTH):
        candidate = current / _HYBRID_SEARCH_IGNORE_FILENAME
        if candidate.exists() and candidate.is_file():
            try:
                if candidate.stat().st_size <= _GITIGNORE_MAX_SIZE:
                    collected.extend(
                        candidate.read_text(
                            encoding="utf-8", errors="ignore"
                        ).splitlines()
                    )
            except OSError:
                pass

        # Stop AFTER processing the level that holds ``.git`` — repo boundary.
        if (current / ".git").exists():
            break

        parent = current.parent
        if parent == current:
            break  # filesystem root
        current = parent

    return collected


def _build_ignore_spec(
    project_root: Path,
    config: IndexingConfig,
) -> pathspec.PathSpec:
    """Build pathspec from config excludes + .gitignore + .hybrid-search-ignore."""
    patterns = list(config.exclude_patterns)

    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        try:
            if gitignore.stat().st_size <= _GITIGNORE_MAX_SIZE:
                patterns.extend(gitignore.read_text().splitlines())
        except OSError:
            pass

    patterns.extend(_collect_hybrid_search_ignore_patterns(project_root))

    return pathspec.PathSpec.from_lines("gitignore", patterns)
