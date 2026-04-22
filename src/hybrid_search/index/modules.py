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

# File-module weights. Kept as constants so discover_modules + tests
# share the same scale.
_WEIGHT_CODE = 1.0
_WEIGHT_DOC_PRIMARY = 0.5
# Step F1: docs that mention files across >1 module stay with their own
# docs-directory module (the strict-merge rule still holds) but are *also*
# attached as low-weight cross-ref members to each mentioned module. This is
# how a feature doc like docs/features/portal-parent-student.md ends up
# visible to the portal-v3 module card during synthesis — so "학부모" lands
# on the card text alongside "shell rendering". Weight stays well below
# primary doc weight (0.5) so chunk-search ranking isn't swayed.
_WEIGHT_DOC_CROSSREF = 0.2
# Per-module cap for cross-ref docs so a cross-cutting HANDOFF that mentions
# 20 paths doesn't bloat every affected module.
_MAX_CROSSREFS_PER_MODULE = 3


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

# Tokens for the F3 "named subsystem" check: words that could plausibly be
# a module name (alnum + hyphen + underscore, length ≥ 3). Conservative
# lower bound avoids matching 2-letter English stopwords.
_NAME_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")


def _extract_path_mentions(text: str) -> set[str]:
    """Find plausible file-path references in a doc body."""
    return set(m.group(1) for m in _PATH_MENTION_RE.finditer(text))


def _extract_name_tokens(text: str) -> set[str]:
    """Lowercase alnum/hyphen tokens — used to detect subsystem names that
    appear in prose (not just as file paths). Used by F3 sub-threshold
    promotion to answer "does any doc actually talk about this dir?"."""
    return {m.group(0).lower() for m in _NAME_TOKEN_RE.finditer(text)}


def _count_name_tokens(text: str) -> dict[str, int]:
    """Per-token occurrence count — F5 cross-ref needs to distinguish
    docs that *topically* describe a module ("portal-v3" mentioned 10
    times) from docs that merely name-drop ("admissions" once in a route
    listing). Thresholded occurrence keeps generic DESIGN.md / CLAUDE.md
    style manifests from hitching to every module whose name appears
    anywhere in them."""
    counts: dict[str, int] = {}
    for m in _NAME_TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        counts[tok] = counts.get(tok, 0) + 1
    return counts


# Docs that legitimately list every subsystem by name — they'd otherwise
# light up F5 cross-refs for every module. We skip them entirely.
_GENERIC_META_DOCS = frozenset({
    "claude.md", "design.md", "handoff.md", "readme.md",
    "contributing.md", "changelog.md",
})
# Minimum per-doc occurrence of a module's leaf name before we treat it as
# a topical mention (F5). Below this it's usually name-drop noise.
_MIN_NAME_MENTIONS = 2

# Step G: "bucket" directories — generic containers that group
# heterogeneous files by kind, not by subsystem. Files here should be
# cross-attached to whichever feature module their filename tokens name,
# so e.g. ``create_academy_monthly_stats.sql`` reaches the ``stats``
# module and not just the catch-all ``migrations`` one.
_BUCKET_DIR_LEAVES = frozenset({"migrations", "seed", "seeds", "schema"})
# SQL/ops verbs that carry zero subsystem signal — they'd otherwise
# match every module named "update", "drop" etc. if such existed.
_CROSSTREE_STOPWORDS = frozenset({
    "create", "update", "delete", "alter", "drop", "add", "remove",
    "insert", "select", "rename", "enable", "disable", "grant", "revoke",
    "init", "setup", "cleanup", "backfill", "migration", "migrations",
    "fix", "temp", "tmp", "test", "tests", "old", "new", "main",
})
# Minimum token length for cross-tree matching — keeps "ai", "id", etc.
# from producing spurious attachments.
_CROSSTREE_MIN_TOKEN_LEN = 3
# Cross-tree member weight. Below primary doc (0.5) and prose-crossref
# (0.2), so ranking still favors direct ownership but the file is
# discoverable via the feature module.
_WEIGHT_CROSSTREE = 0.3
# Per-module cap for cross-tree attachments.
_MAX_CROSSTREE_PER_MODULE = 4

_DATE_PREFIX_RE = re.compile(r"^\d{6,14}_?")


def _crosstree_filename_tokens(rel_path: str) -> set[str]:
    """Tokenize a filename for cross-tree matching.

    Strips the directory prefix, extension, leading date prefix, then
    splits on hyphen/underscore. Drops ``_CROSSTREE_STOPWORDS`` (verbs
    like "create") and tokens below ``_CROSSTREE_MIN_TOKEN_LEN``.
    """
    stem = Path(rel_path).stem
    stem = _DATE_PREFIX_RE.sub("", stem)
    tokens = {
        part.lower()
        for part in re.split(r"[-_]+", stem)
        if part
    }
    return {
        t for t in tokens
        if len(t) >= _CROSSTREE_MIN_TOKEN_LEN
        and t not in _CROSSTREE_STOPWORDS
    }


def _module_name_tokens(name: str) -> set[str]:
    """Leaf-name tokens for cross-tree matching. Produces both the
    original form and a naive singular ("stats" → {"stats", "stat"};
    "admissions" → {"admissions", "admission"}) so ``admission_results.sql``
    reaches the ``admissions`` module."""
    parts = {p.lower() for p in re.split(r"[-_]+", name) if p}
    singulars: set[str] = set()
    for p in parts:
        if len(p) > 4 and p.endswith("s"):
            singulars.add(p[:-1])
    return (parts | singulars) - _CROSSTREE_STOPWORDS


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
    #
    # Step F1 addendum: when a doc's mentions resolve to >1 module keys, we
    # don't merge — but we *do* record cross-refs so the doc is still
    # attached (as a low-weight member) to each target module later, giving
    # synthesis access to the doc's content across subsystems.
    docs_with_mentions: list[tuple[str, set[str]]] = []
    # target_key → ordered list of doc paths pointing at it (cross-ref only).
    cross_refs: dict[str, list[str]] = defaultdict(list)
    # Per-doc token counts. F3 uses the token set ("is this name spoken
    # anywhere?"); F5 uses per-doc counts ("does this doc *topically*
    # talk about that module, or just name-drop it once in a route
    # listing?"). Stored once to avoid re-reading files.
    doc_token_counts: dict[str, dict[str, int]] = {}
    for rel, f in path_to_file.items():
        if Path(rel).suffix.lower() not in _DOC_EXTS:
            continue
        abs_path = project_root / rel
        try:
            text = abs_path.read_text(errors="ignore")
        except OSError:
            continue
        doc_token_counts[rel] = _count_name_tokens(text)
        mentions = _extract_path_mentions(text)
        resolved = {m for m in mentions if m in path_to_file}
        if not resolved:
            continue
        docs_with_mentions.append((rel, resolved))

        target_keys = {key_by_path[target] for target in resolved}
        if len(target_keys) == 1:
            (only_key,) = target_keys
            # Merge with code key as the root so the subsystem name is
            # preserved (e.g., "portal-v3" / "analytics") rather than
            # inheriting the doc's directory ("features"). Without this,
            # a feature doc path-mentioning a code file steals the module
            # name and search-by-subsystem-name breaks.
            uf.union(only_key, key_by_path[rel])
            continue
        # Multi-target doc: record a cross-ref for each distinct target that
        # isn't the doc's own module.
        doc_key = key_by_path[rel]
        for tk in target_keys:
            if tk == doc_key:
                continue
            if rel not in cross_refs[tk]:
                cross_refs[tk].append(rel)

    # Files mentioned anywhere (any doc). Used by F3 promotion alongside
    # the doc-token set.
    mentioned_files: set[str] = set()
    for _doc, resolved in docs_with_mentions:
        mentioned_files |= resolved

    # Union of all doc tokens — cheap "name spoken somewhere?" lookup for F3
    # sub-threshold promotion.
    doc_token_set: set[str] = set()
    for counts in doc_token_counts.values():
        doc_token_set |= counts.keys()

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

    # Resolve union-find roots for cross-ref target keys once up-front so
    # cross-refs follow any merges that happened in step 2's doc pass.
    resolved_crossrefs: dict[str, list[str]] = defaultdict(list)
    for tk, docs in cross_refs.items():
        root = uf.find(tk)
        for d in docs:
            if d not in resolved_crossrefs[root]:
                resolved_crossrefs[root].append(d)

    # F5 — "named subsystem" cross-ref: when a doc's body names a module by
    # its leaf segment (e.g. prose mentions "portal-v3") but uses parenthesized
    # paths like ``app/(portal)/layout.tsx`` that the path-mention regex
    # doesn't capture, we still want that doc to enrich the module's card.
    # This is the S2 fix: the parent-portal feature doc talks about
    # "portal-v3" in prose alongside non-regex-friendly paths, and without
    # this rule it never reaches the portal-v3 module's summary.
    root_leafs: dict[str, str] = {}
    for root in members_by_root:
        if root.startswith("root:"):
            continue
        leaf = root.rsplit("/", 1)[-1].lower()
        if len(leaf) >= 3:
            root_leafs[root] = leaf
    for doc_rel, counts in doc_token_counts.items():
        # Skip generic meta docs that describe the whole project; they'd
        # otherwise light up cross-refs for every module they list.
        if Path(doc_rel).name.lower() in _GENERIC_META_DOCS:
            continue
        doc_root = uf.find(key_by_path.get(doc_rel, ""))
        for root, leaf in root_leafs.items():
            if root == doc_root:
                continue
            if counts.get(leaf, 0) >= _MIN_NAME_MENTIONS:
                if doc_rel not in resolved_crossrefs[root]:
                    resolved_crossrefs[root].append(doc_rel)

    # Cap per module (applies after path-mention + name-prose collection).
    for root in list(resolved_crossrefs):
        resolved_crossrefs[root] = resolved_crossrefs[root][:_MAX_CROSSREFS_PER_MODULE]

    for root, members in members_by_root.items():
        members = sorted(members)
        promoted_via_doc = False
        if len(members) < _MIN_MODULE_SIZE:
            single = members[0]
            is_doc = Path(single).suffix.lower() in _DOC_EXTS
            # F3 sub-threshold promotion: a size-1 code dir is kept as a
            # module if *some* doc talks about it — either (a) the file is
            # mentioned directly by path, or (b) the dir's leaf name
            # appears in any doc body as a distinct token. That's enough
            # signal to promote components/analytics/ into a subsystem
            # card when a feature doc says "analytics" in prose, which
            # otherwise gets dropped by the ≥ 2 files rule.
            if not is_doc:
                leaf = root.rsplit("/", 1)[-1].lower()
                file_mentioned = any(
                    m in mentioned_files for m in members
                )
                name_in_docs = (
                    leaf
                    and not leaf.startswith("root:")
                    and leaf in doc_token_set
                )
                if not (file_mentioned or name_in_docs):
                    continue
                promoted_via_doc = True
        module_id = _module_id(project_id, members)
        name = _derive_name(root, members)
        signals_base = {"directory"} | _signal_flags(root, members, docs_with_mentions)
        crossrefs_for_mod = [
            d for d in resolved_crossrefs.get(root, []) if d not in members
        ]
        if crossrefs_for_mod:
            signals_base.add("crossref_doc")
        if promoted_via_doc:
            signals_base.add("doc_promoted")
        signals = sorted(signals_base)
        # related_docs advertises both primary members and cross-ref docs so
        # downstream readers (wiki links, search result footers) see the full
        # set of documentation that speaks about this module.
        related_docs = [
            m for m in members if Path(m).suffix.lower() in _DOC_EXTS
        ] + crossrefs_for_mod
        # member_hash folds cross-refs in so synth re-runs when the set
        # changes — otherwise Step F2's doc-excerpt pass would skip a module
        # whose primary members didn't change.
        hash_input = "\n".join(members) + "\n##crossref##\n" + "\n".join(crossrefs_for_mod)
        member_hash = hashlib.sha256(
            hash_input.encode("utf-8")
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
            weight = _WEIGHT_DOC_PRIMARY if Path(path).suffix.lower() in _DOC_EXTS else _WEIGHT_CODE
            file_module_rows.append((fid, module_id, weight))
        for doc_path in crossrefs_for_mod:
            fid = file_path_to_id.get(doc_path)
            if fid is None:
                continue
            file_module_rows.append((fid, module_id, _WEIGHT_DOC_CROSSREF))

    # -- Step G: cross-tree attachment --
    #
    # Bucket dirs (database/migrations, supabase/migrations, …) group
    # heterogeneous files by kind, not by feature. Re-project each file's
    # filename tokens onto the module catalog so a query like
    # ``월별 학원 통계`` can reach ``create_academy_monthly_stats.sql`` via
    # the ``stats`` module, not just via the catch-all ``migrations``
    # module whose summary mixes admission/RLS/workspace/etc. content.
    module_tokens_by_id: dict[str, set[str]] = {
        m.id: _module_name_tokens(m.name) for m in modules_out
    }
    # Attach map: module_id → list[file_rel]
    crosstree_attach: dict[str, list[str]] = defaultdict(list)
    for rel, f in path_to_file.items():
        parent_leaf = Path(rel).parent.name.lower()
        if parent_leaf not in _BUCKET_DIR_LEAVES:
            continue
        file_toks = _crosstree_filename_tokens(rel)
        if not file_toks:
            continue
        scored: list[tuple[str, int, int]] = []  # (module_id, overlap, name_len)
        for mid, mtoks in module_tokens_by_id.items():
            overlap = len(file_toks & mtoks)
            if overlap == 0:
                continue
            # Prefer more-specific matches (longer module name wins ties).
            name_len = len(next((m for m in modules_out if m.id == mid)).name)
            scored.append((mid, overlap, name_len))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[1], -x[2]))
        # Attach to top-1 only to avoid a file advertising in every module
        # that happens to share a generic token.
        target_mid = scored[0][0]
        if rel not in crosstree_attach[target_mid]:
            crosstree_attach[target_mid].append(rel)

    # Fold attachments into file_module_rows + signal flag + member_hash.
    if crosstree_attach:
        # Rebuild hash_input per touched module so synth re-runs pick the
        # new member set up.
        touched = set(crosstree_attach.keys())
        # Rewrite the corresponding module records with attachment signals
        # and an updated member_hash.
        updated_modules: list[ModuleRecord] = []
        for m in modules_out:
            if m.id in touched:
                attachments = crosstree_attach[m.id][:_MAX_CROSSTREE_PER_MODULE]
                sig = set(json.loads(m.signals or "[]"))
                sig.add("crosstree_attached")
                new_hash = hashlib.sha256(
                    (m.member_hash + "\n##crosstree##\n" + "\n".join(attachments))
                    .encode("utf-8")
                ).hexdigest()[:16]
                updated_modules.append(
                    ModuleRecord(
                        id=m.id, project_id=m.project_id, name=m.name,
                        summary=m.summary, entry_points=m.entry_points,
                        depends_on=m.depends_on, related_docs=m.related_docs,
                        rationale=m.rationale,
                        signals=json.dumps(sorted(sig)),
                        member_hash=new_hash, updated_at=m.updated_at,
                    )
                )
                for rel in attachments:
                    fid = file_path_to_id.get(rel)
                    if fid is None:
                        continue
                    file_module_rows.append((fid, m.id, _WEIGHT_CROSSTREE))
            else:
                updated_modules.append(m)
        modules_out = updated_modules

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
