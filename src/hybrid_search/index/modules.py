"""Module discovery — groups files into coherent subsystems for Phase 5 retrieval.

Signals fused (v1):
  1. **Directory prefix** — files in the same parent directory are initial cluster.
     Files in root get grouped by extension category.
  2. **Doc-code mentions** — markdown docs under docs/ that reference code file
     paths in their body merge into the referenced files' modules.
  3. **Callgraph bridges** (optional, currently weak): files whose chunks call
     into another file's chunks hint at module affinity. Not used to merge in v1
     to avoid over-clustering; exposed as a signal tag only.

Output: writes ``modules`` + ``file_modules`` tables via StoreDB.upsert_module /
set_file_modules. Idempotent — member_hash allows Step 3 synthesis to skip
unchanged modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from hybrid_search.storage.db import FileRecord, ModuleRecord, StoreDB

logger = logging.getLogger(__name__)

# Parent directories we treat as "root containers" — children get grouped by
# their own subdirectory, not the container itself.
_ROOT_CONTAINERS = frozenset({
    "", ".", "src", "app", "components", "lib", "services", "hooks", "utils",
    "packages", "pkg", "internal", "apps",
})

# Extensions we treat as "document" files for doc-overlay logic.
_DOC_EXTS = frozenset({".md", ".mdx", ".rst", ".txt"})

# Minimum files for a module to survive singleton filter. Docs can stay alone.
_MIN_MODULE_SIZE = 2


def _module_key_for(relative_path: str) -> str:
    """Map a file path → initial module grouping key.

    Heuristic:
      - Files under a leaf directory → use the full parent directory path.
      - Root files → use ``root:<extension>`` grouping.
    """
    p = Path(relative_path)
    parent = p.parent
    if str(parent) in ("", "."):
        return f"root:{p.suffix.lstrip('.') or 'noext'}"
    # When the parent is a 'container' directory, step one more up.
    parts = parent.parts
    # Trim container ancestors from the left while the current leading part is
    # still a container and there's a child level left.
    while len(parts) > 1 and parts[0] in _ROOT_CONTAINERS:
        parts = parts[1:]
    return "/".join(parts) if parts else "root:misc"


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        if a not in self.parent:
            self.parent[a] = a
        if b not in self.parent:
            self.parent[b] = b
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


_PATH_MENTION_RE = re.compile(
    r"(?:^|[\s`'\"(\[])"
    r"([a-zA-Z0-9_][\w./-]*?/[\w./-]+?\.(?:ts|tsx|js|jsx|py|sql|md|mdx|go|rs|rb|java|vue|svelte))"
    r"(?:[\s`'\")\].,;:]|$)"
)


def _extract_path_mentions(text: str) -> set[str]:
    """Find plausible file-path references in a doc body."""
    return set(m.group(1) for m in _PATH_MENTION_RE.finditer(text))


def discover_modules(
    db: StoreDB,
    project_id: str,
    project_root: Path,
) -> dict:
    """Build modules + file_modules for the project. Returns stats dict."""
    all_files: list[FileRecord] = db.get_all_files(project_id)
    if not all_files:
        return {"modules": 0, "files_assigned": 0}

    path_to_file: dict[str, FileRecord] = {
        f.relative_path: f for f in all_files
    }

    # -- Step 1: initial grouping by directory key --
    key_by_path: dict[str, str] = {
        f.relative_path: _module_key_for(f.relative_path) for f in all_files
    }
    uf = UnionFind(list(key_by_path.values()))

    # -- Step 2: doc-code mentions — pull a doc *into* its target module.
    #
    # Strict rule: only merge if the doc's mentions all resolve to ONE module
    # key. Any plurality/N-way merge chains via UF so that sequential docs
    # (D1:{A,B}, D2:{B,C}) transitively fuse A+B+C into one giant module.
    # Cross-cutting docs (HANDOFFs/roadmaps) touch 5+ subsystems and would
    # collapse everything; we let them stay with their own directory group.
    docs_with_mentions: list[tuple[str, set[str]]] = []
    for rel, f in path_to_file.items():
        if Path(rel).suffix.lower() not in _DOC_EXTS:
            continue
        abs_path = project_root / rel
        try:
            text = abs_path.read_text(errors="ignore")
        except OSError:
            continue
        mentions = _extract_path_mentions(text)
        resolved = {m for m in mentions if m in path_to_file}
        if not resolved:
            continue
        docs_with_mentions.append((rel, resolved))

        target_keys = {key_by_path[target] for target in resolved}
        if len(target_keys) != 1:
            continue
        (only_key,) = target_keys
        uf.union(key_by_path[rel], only_key)

    # -- Step 3: materialize modules from union-find roots --
    members_by_root: dict[str, list[str]] = defaultdict(list)
    for path, key in key_by_path.items():
        root = uf.find(key)
        members_by_root[root].append(path)

    # Sort members deterministically, drop singletons unless they're a doc.
    now = datetime.now(timezone.utc).isoformat()
    modules_out: list[ModuleRecord] = []
    file_module_rows: list[tuple[str, str, float]] = []
    file_path_to_id = {f.relative_path: f.id for f in all_files}

    for root, members in members_by_root.items():
        members = sorted(members)
        if len(members) < _MIN_MODULE_SIZE:
            single = members[0]
            if Path(single).suffix.lower() not in _DOC_EXTS:
                continue
        module_id = _module_id(project_id, members)
        name = _derive_name(root, members)
        signals = sorted({"directory"} | _signal_flags(root, members, docs_with_mentions))
        related_docs = [m for m in members if Path(m).suffix.lower() in _DOC_EXTS]
        member_hash = hashlib.sha256(
            "\n".join(members).encode("utf-8")
        ).hexdigest()[:16]
        modules_out.append(
            ModuleRecord(
                id=module_id,
                project_id=project_id,
                name=name,
                summary=None,
                entry_points=None,
                depends_on=None,
                related_docs=json.dumps(related_docs, ensure_ascii=False),
                rationale=None,
                signals=json.dumps(signals),
                member_hash=member_hash,
                updated_at=now,
            )
        )
        for path in members:
            fid = file_path_to_id.get(path)
            if fid is None:
                continue
            # Docs get lower weight than code since they describe rather than implement.
            weight = 0.5 if Path(path).suffix.lower() in _DOC_EXTS else 1.0
            file_module_rows.append((fid, module_id, weight))

    # -- Step 4: write to DB (idempotent replacement) --
    with db.transaction() as conn:
        db.delete_project_modules(conn, project_id)
        for m in modules_out:
            db.upsert_module(conn, m)
        db.set_file_modules(conn, project_id, file_module_rows)

    return {
        "modules": len(modules_out),
        "files_assigned": len(file_module_rows),
        "total_files": len(all_files),
    }


def _module_id(project_id: str, members: list[str]) -> str:
    h = hashlib.sha256()
    h.update(project_id.encode("utf-8"))
    h.update(b"\n")
    for m in sorted(members):
        h.update(m.encode("utf-8"))
        h.update(b"\n")
    return f"mod_{h.hexdigest()[:16]}"


def _derive_name(root_key: str, members: list[str]) -> str:
    """Pick a human-readable name for the module.

    Priority:
      1. If root_key is "root:<ext>" → "(root <ext>)"
      2. Else → last non-empty segment of the directory, kebab-normalized.
    """
    if root_key.startswith("root:"):
        return f"(root {root_key.split(':', 1)[1]})"
    segments = [s for s in root_key.split("/") if s]
    if not segments:
        return root_key
    # Take the deepest segment as the canonical name.
    return segments[-1]


def _signal_flags(
    root_key: str,
    members: list[str],
    doc_mentions: list[tuple[str, set[str]]],
) -> set[str]:
    flags: set[str] = set()
    doc_set = {m for m in members if Path(m).suffix.lower() in _DOC_EXTS}
    if doc_set:
        flags.add("has_doc")
    for doc, targets in doc_mentions:
        if doc in members and any(t in members for t in targets):
            flags.add("doc_mention")
            break
    return flags
