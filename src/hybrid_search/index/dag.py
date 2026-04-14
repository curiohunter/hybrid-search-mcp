"""DAG construction and module tree generation for CodeWiki (Phase 8a).

Builds a directed dependency graph from resolved call_edges, finds connected
components, performs topological sort, and produces a module tree for wiki
page generation.

Based on CodeWiki (ACL 2026):
  1. call_edges + imports → directed graph G=(V,E)
  2. zero-in-degree nodes = entry points
  3. connected components = feature modules
  4. topological sort → bottom-up processing order
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from hybrid_search.storage.db import StoreDB, ChunkRecord, FileRecord

logger = logging.getLogger(__name__)

# Modules with more chunks than this get split into sub-modules
MAX_MODULE_CHUNKS = 40

# Minimum chunks for a module to be considered non-trivial
MIN_MODULE_CHUNKS = 2


@dataclass
class ModuleNode:
    """A feature module discovered from the dependency graph."""

    name: str
    files: list[str] = field(default_factory=list)         # relative_paths
    chunks: list[str] = field(default_factory=list)         # chunk_ids
    entry_points: list[str] = field(default_factory=list)   # chunk qualified_names (zero in-degree)
    representative_paths: list[str] = field(default_factory=list)  # top-3 representative paths

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


@dataclass
class WikiPlan:
    """The complete module tree for a project."""

    modules: list[ModuleNode] = field(default_factory=list)
    isolated_modules: list[ModuleNode] = field(default_factory=list)  # dir-based fallback
    total_chunks: int = 0
    covered_chunks: int = 0

    @property
    def coverage(self) -> float:
        return self.covered_chunks / max(self.total_chunks, 1)


def build_dependency_graph(
    edges: list[dict],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build adjacency lists from resolved call_edges (High+Medium only).

    Returns (forward_graph, reverse_graph) where:
      forward[caller] = {callee1, callee2, ...}
      reverse[callee] = {caller1, caller2, ...}
    """
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)

    for edge in edges:
        caller = edge["caller_chunk_id"]
        callee = edge.get("callee_chunk_id")
        confidence = edge.get("confidence", "low")

        if not callee or confidence not in ("high", "medium"):
            continue
        if caller == callee:
            continue  # skip self-loops

        forward[caller].add(callee)
        reverse[callee].add(caller)

    return dict(forward), dict(reverse)


def find_connected_components(
    forward: dict[str, set[str]],
    reverse: dict[str, set[str]],
    all_chunk_ids: set[str],
) -> list[set[str]]:
    """Find connected components using undirected BFS on the dependency graph.

    Only chunks that appear in at least one edge are included.
    Isolated chunks (no edges) are excluded — handled separately.
    """
    # Build undirected adjacency from forward + reverse
    adj: dict[str, set[str]] = defaultdict(set)
    for src, dsts in forward.items():
        for dst in dsts:
            adj[src].add(dst)
            adj[dst].add(src)
    for src, dsts in reverse.items():
        for dst in dsts:
            adj[src].add(dst)
            adj[dst].add(src)

    graph_nodes = set(adj.keys()) & all_chunk_ids
    visited: set[str] = set()
    components: list[set[str]] = []

    for node in graph_nodes:
        if node in visited:
            continue
        component: set[str] = set()
        queue = deque([node])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in adj.get(current, set()):
                if neighbor not in visited and neighbor in all_chunk_ids:
                    queue.append(neighbor)
        if component:
            components.append(component)

    return sorted(components, key=len, reverse=True)


def topological_sort(
    forward: dict[str, set[str]], component: set[str],
) -> list[str]:
    """Kahn's algorithm for topological sort within a component.

    Returns chunk_ids in bottom-up order (leaves first, entry points last).
    Falls back to arbitrary order if the subgraph has cycles.
    """
    # Build in-degree counts within the component
    in_degree: dict[str, int] = {node: 0 for node in component}
    sub_forward: dict[str, set[str]] = defaultdict(set)

    for src in component:
        for dst in forward.get(src, set()):
            if dst in component:
                sub_forward[src].add(dst)
                in_degree[dst] = in_degree.get(dst, 0) + 1

    # Kahn's: start from zero in-degree (entry points call others but nobody calls them)
    queue = deque(node for node, deg in in_degree.items() if deg == 0)
    result: list[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for neighbor in sub_forward.get(node, set()):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # If cycle exists, append remaining nodes
    if len(result) < len(component):
        remaining = component - set(result)
        result.extend(remaining)

    # Reverse: leaves first, entry points last → bottom-up processing
    return list(reversed(result))


def _derive_module_name(
    files: list[str], chunks: list[ChunkRecord],
) -> str:
    """Derive a human-readable module name from file paths.

    Strategy:
    1. Find the longest common directory prefix
    2. If all files share a meaningful directory, use it
    3. Otherwise use the most common directory segment
    """
    if not files:
        return "unknown"

    paths = [PurePosixPath(f) for f in files]

    # Single file → use filename stem
    if len(paths) == 1:
        return paths[0].stem

    # Find common prefix directory
    parts_list = [list(p.parts) for p in paths]
    common_parts: list[str] = []
    for parts in zip(*parts_list):
        if len(set(parts)) == 1:
            common_parts.append(parts[0])
        else:
            break

    if common_parts:
        # Use the deepest common directory (skip generic ones like "src")
        meaningful = [p for p in common_parts if p not in ("src", "lib", "app", ".", "source")]
        if meaningful:
            return meaningful[-1]

    # Fallback: most common parent directory
    parent_counts: dict[str, int] = defaultdict(int)
    for p in paths:
        if len(p.parts) >= 2:
            parent_counts[p.parts[-2]] += 1
        else:
            parent_counts[p.stem] += 1

    if parent_counts:
        return max(parent_counts, key=lambda k: parent_counts[k])

    return paths[0].stem


_DOC_EXTENSIONS = frozenset({".md", ".txt", ".rst", ".adoc"})


def _group_isolated_by_directory(
    isolated_ids: set[str],
    chunk_map: dict[str, ChunkRecord],
    file_map: dict[str, FileRecord],
) -> list[set[str]]:
    """Group isolated chunks by their parent directory.

    Document files (.md, .txt, .rst) are split per-file so each document
    becomes its own wiki page with independent wikilinks and staleness tracking.
    Code files are grouped by directory as before.
    """
    dir_groups: dict[str, set[str]] = defaultdict(set)
    doc_file_groups: dict[str, set[str]] = defaultdict(set)

    for chunk_id in isolated_ids:
        chunk = chunk_map.get(chunk_id)
        if not chunk:
            continue
        file_rec = file_map.get(chunk.file_id)
        if not file_rec:
            continue
        ext = PurePosixPath(file_rec.relative_path).suffix.lower()
        if ext in _DOC_EXTENSIONS:
            doc_file_groups[file_rec.relative_path].add(chunk_id)
        else:
            parent = str(PurePosixPath(file_rec.relative_path).parent)
            dir_groups[parent].add(chunk_id)

    groups = [g for g in dir_groups.values() if len(g) >= MIN_MODULE_CHUNKS]
    groups.extend(g for g in doc_file_groups.values() if g)
    return groups


def _representative_paths(files: list[str], max_paths: int = 3) -> list[str]:
    """Pick representative paths — shortest and most descriptive."""
    if len(files) <= max_paths:
        return sorted(files)
    return sorted(files, key=len)[:max_paths]


def generate_wiki_plan(db: StoreDB, project_id: str) -> WikiPlan:
    """Generate the complete module tree for CodeWiki.

    Pipeline:
    1. Load all call_edges + chunks + files
    2. Build dependency DAG (High+Medium confidence)
    3. Find connected components
    4. For each component: topological sort, identify entry points, derive name
    5. Isolated nodes: group by directory as fallback
    6. Return WikiPlan with all modules
    """
    edges = db.get_all_call_edges(project_id)
    all_chunks = db.get_chunks_by_project(project_id)
    all_files = db.get_all_files(project_id)

    # Build lookup maps
    chunk_map: dict[str, ChunkRecord] = {c.id: c for c in all_chunks}
    file_map: dict[str, FileRecord] = {f.id: f for f in all_files}
    all_chunk_ids = set(chunk_map.keys())

    # Step 1: Build dependency graph
    forward, reverse = build_dependency_graph(edges)

    # Step 2: Find connected components
    components = find_connected_components(forward, reverse, all_chunk_ids)

    # Track which chunks are covered by graph components
    covered: set[str] = set()
    modules: list[ModuleNode] = []

    for comp in components:
        covered.update(comp)

        # Topological sort for bottom-up processing order
        topo_order = topological_sort(forward, comp)

        # Entry points: zero in-degree within this component
        entry_points: list[str] = []
        for chunk_id in comp:
            if chunk_id not in reverse or not (reverse[chunk_id] & comp):
                chunk = chunk_map.get(chunk_id)
                if chunk and chunk.qualified_name:
                    entry_points.append(chunk.qualified_name)

        # Collect files for this component
        file_ids: set[str] = set()
        for chunk_id in comp:
            chunk = chunk_map.get(chunk_id)
            if chunk:
                file_ids.add(chunk.file_id)

        files = [
            file_map[fid].relative_path
            for fid in file_ids
            if fid in file_map
        ]
        chunks_in_comp = [chunk_map[cid] for cid in comp if cid in chunk_map]

        name = _derive_module_name(files, chunks_in_comp)
        rep_paths = _representative_paths(files)

        module = ModuleNode(
            name=name,
            files=sorted(files),
            chunks=list(comp),
            entry_points=sorted(entry_points)[:5],  # top 5
            representative_paths=rep_paths,
        )

        # Split large modules
        if module.chunk_count > MAX_MODULE_CHUNKS:
            sub_modules = _split_large_module(module, chunk_map, file_map, forward, comp)
            modules.extend(sub_modules)
        else:
            modules.append(module)

    # Step 3: Isolated nodes — directory-based fallback
    isolated = all_chunk_ids - covered
    isolated_groups = _group_isolated_by_directory(isolated, chunk_map, file_map)
    isolated_modules: list[ModuleNode] = []

    for group in isolated_groups:
        file_ids_set: set[str] = set()
        for cid in group:
            chunk = chunk_map.get(cid)
            if chunk:
                file_ids_set.add(chunk.file_id)

        files = [
            file_map[fid].relative_path
            for fid in file_ids_set
            if fid in file_map
        ]
        chunks_in_group = [chunk_map[cid] for cid in group if cid in chunk_map]
        name = _derive_module_name(files, chunks_in_group)

        isolated_modules.append(ModuleNode(
            name=f"{name} (isolated)",
            files=sorted(files),
            chunks=list(group),
            entry_points=[],
            representative_paths=_representative_paths(files),
        ))

    # Deduplicate module names
    _deduplicate_names(modules)
    _deduplicate_names(isolated_modules)

    # Sort: largest modules first
    modules.sort(key=lambda m: m.chunk_count, reverse=True)
    isolated_modules.sort(key=lambda m: m.chunk_count, reverse=True)

    covered_with_isolated = covered | {cid for g in isolated_groups for cid in g}

    plan = WikiPlan(
        modules=modules,
        isolated_modules=isolated_modules,
        total_chunks=len(all_chunk_ids),
        covered_chunks=len(covered_with_isolated),
    )

    logger.info(
        "Wiki plan: %d modules + %d isolated, coverage %.1f%% (%d/%d chunks)",
        len(modules), len(isolated_modules),
        plan.coverage * 100, plan.covered_chunks, plan.total_chunks,
    )
    return plan


def _split_large_module(
    module: ModuleNode,
    chunk_map: dict[str, ChunkRecord],
    file_map: dict[str, FileRecord],
    forward: dict[str, set[str]],
    component: set[str],
) -> list[ModuleNode]:
    """Split a large module by sub-directory grouping."""
    dir_groups: dict[str, list[str]] = defaultdict(list)  # dir → chunk_ids

    for chunk_id in component:
        chunk = chunk_map.get(chunk_id)
        if not chunk:
            continue
        file_rec = file_map.get(chunk.file_id)
        if not file_rec:
            continue
        # Use parent directory relative to the module's common prefix
        parent = str(PurePosixPath(file_rec.relative_path).parent)
        dir_groups[parent].append(chunk_id)

    # If splitting doesn't help (all in one dir), return as-is
    if len(dir_groups) <= 1:
        return [module]

    sub_modules: list[ModuleNode] = []
    for dir_path, chunk_ids in dir_groups.items():
        file_ids_set: set[str] = set()
        for cid in chunk_ids:
            chunk = chunk_map.get(cid)
            if chunk:
                file_ids_set.add(chunk.file_id)

        files = [
            file_map[fid].relative_path
            for fid in file_ids_set
            if fid in file_map
        ]
        chunks_in_sub = [chunk_map[cid] for cid in chunk_ids if cid in chunk_map]
        sub_name = _derive_module_name(files, chunks_in_sub)

        entry_points: list[str] = []
        chunk_set = set(chunk_ids)
        for cid in chunk_ids:
            # Entry point if no callers from within this sub-group
            callers_in_group = set()
            for src, dsts in forward.items():
                if cid in dsts and src in chunk_set:
                    callers_in_group.add(src)
            if not callers_in_group:
                chunk = chunk_map.get(cid)
                if chunk and chunk.qualified_name:
                    entry_points.append(chunk.qualified_name)

        sub_modules.append(ModuleNode(
            name=sub_name,
            files=sorted(files),
            chunks=chunk_ids,
            entry_points=sorted(entry_points)[:5],
            representative_paths=_representative_paths(files),
        ))

    return sub_modules


@dataclass
class WikiPageContent:
    """Generated wiki page content for a module."""

    name: str
    filename: str       # slug.md
    title: str
    content: str
    tags: list[str]
    file_ids: list[str]  # for dependency tracking
    chunk_ids: list[str]


def generate_module_wiki(
    module: ModuleNode,
    chunk_map: dict[str, ChunkRecord],
    file_map: dict[str, FileRecord],
    forward: dict[str, set[str]],
    reverse: dict[str, set[str]],
    chunk_to_module: dict[str, str] | None = None,
    filepath_to_module: dict[str, str] | None = None,
) -> WikiPageContent:
    """Generate structured wiki markdown for a single module.

    Produces a deterministic code-structure summary (no LLM needed):
    - Module overview with file list
    - Entry points (zero in-degree)
    - Functions/classes with call relationships
    - Internal dependency map
    """
    lines: list[str] = []
    title = module.name.replace("-", " ").replace("_", " ").title()
    lines.append(f"# {title}")
    lines.append("")

    # Collect chunk records for this module
    chunks = [chunk_map[cid] for cid in module.chunks if cid in chunk_map]

    # Group chunks by file
    file_chunks: dict[str, list[ChunkRecord]] = defaultdict(list)
    for chunk in chunks:
        file_chunks[chunk.file_id].append(chunk)

    # --- Overview ---
    lines.append(f"**Files**: {module.file_count} | **Symbols**: {module.chunk_count}")
    lines.append("")

    # --- Files ---
    lines.append("## Files")
    lines.append("")
    for fpath in module.files:
        lines.append(f"- `{fpath}`")
    lines.append("")

    # --- Entry Points ---
    if module.entry_points:
        lines.append("## Entry Points")
        lines.append("")
        for ep in module.entry_points:
            lines.append(f"- `{ep}`")
        lines.append("")

    # --- Symbols per file ---
    lines.append("## Symbols")
    lines.append("")

    file_ids_used: list[str] = []
    for file_id, file_chunks_list in sorted(
        file_chunks.items(),
        key=lambda kv: file_map.get(kv[0], FileRecord(id="", project_id="", relative_path="zzz", file_hash="")).relative_path,
    ):
        file_rec = file_map.get(file_id)
        if not file_rec:
            continue
        file_ids_used.append(file_id)

        lines.append(f"### `{file_rec.relative_path}`")
        lines.append("")

        for chunk in sorted(file_chunks_list, key=lambda c: c.start_line or 0):
            node_type = chunk.node_type or "symbol"
            name = chunk.name or "(anonymous)"
            line_info = f"L{chunk.start_line}" if chunk.start_line else ""

            # Callees from this chunk
            callees = forward.get(chunk.id, set())
            callee_names = []
            for cid in callees:
                callee = chunk_map.get(cid)
                if callee:
                    callee_names.append(callee.name or callee.qualified_name or cid)

            # Callers into this chunk
            callers = reverse.get(chunk.id, set())
            caller_names = []
            for cid in callers:
                caller = chunk_map.get(cid)
                if caller:
                    caller_names.append(caller.name or caller.qualified_name or cid)

            prefix = "→" if chunk.id in {ep_id for ep_id in module.chunks if chunk.qualified_name in module.entry_points} else "-"
            lines.append(f"- **{name}** ({node_type}, {line_info})")

            if callee_names:
                lines.append(f"  - calls: {', '.join(sorted(callee_names)[:8])}")
            if caller_names:
                lines.append(f"  - called by: {', '.join(sorted(caller_names)[:8])}")

        lines.append("")

    # --- Dependency summary ---
    all_callees_outside: list[str] = []
    all_callers_outside: list[str] = []
    module_chunk_set = set(module.chunks)

    for cid in module.chunks:
        for callee_id in forward.get(cid, set()):
            if callee_id not in module_chunk_set:
                callee = chunk_map.get(callee_id)
                if callee:
                    all_callees_outside.append(callee.qualified_name or callee.name or callee_id)
        for caller_id in reverse.get(cid, set()):
            if caller_id not in module_chunk_set:
                caller = chunk_map.get(caller_id)
                if caller:
                    all_callers_outside.append(caller.qualified_name or caller.name or caller_id)

    # --- Related Modules (wikilinks) ---
    related_modules: set[str] = set()

    # 1) Call-edge based: functions calling/called by other modules
    if chunk_to_module:
        module_chunk_set = set(module.chunks)
        for cid in module.chunks:
            for callee_id in forward.get(cid, set()):
                if callee_id not in module_chunk_set and callee_id in chunk_to_module:
                    related_modules.add(chunk_to_module[callee_id])
            for caller_id in reverse.get(cid, set()):
                if caller_id not in module_chunk_set and caller_id in chunk_to_module:
                    related_modules.add(chunk_to_module[caller_id])

    # 2) Content-reference based: backtick file paths in chunk content
    #    e.g. `storage/wiki.py` in a .md doc → matches src/hybrid_search/storage/wiki.py
    if filepath_to_module:
        for cid in module.chunks:
            chunk = chunk_map.get(cid)
            if not chunk or not chunk.content:
                continue
            for ref_path, ref_module in filepath_to_module.items():
                if ref_module == module.name:
                    continue
                # Exact match or suffix match (e.g. "cli.py" matches "src/.../cli.py")
                if ref_path in chunk.content:
                    related_modules.add(ref_module)
                else:
                    # Suffix match: "storage/wiki.py" should match "src/.../storage/wiki.py"
                    basename = ref_path.rsplit("/", 1)[-1] if "/" in ref_path else ref_path
                    if basename.endswith((".py", ".ts", ".tsx", ".js", ".rs", ".go", ".rb",
                                         ".java", ".c", ".cpp", ".swift", ".kt", ".md")):
                        if basename in chunk.content:
                            related_modules.add(ref_module)

    if related_modules:
        lines.append("## Related Modules")
        lines.append("")
        for mod_name in sorted(related_modules):
            lines.append(f"- [[{mod_name}]]")
        lines.append("")

    if all_callees_outside or all_callers_outside:
        lines.append("## External Dependencies")
        lines.append("")
        if all_callees_outside:
            lines.append("**Calls out to:**")
            for dep in sorted(set(all_callees_outside))[:10]:
                lines.append(f"- `{dep}`")
            lines.append("")
        if all_callers_outside:
            lines.append("**Called by:**")
            for dep in sorted(set(all_callers_outside))[:10]:
                lines.append(f"- `{dep}`")
            lines.append("")

    content = "\n".join(lines)
    slug = module.name.lower().replace(" ", "-").replace("(", "").replace(")", "")
    tags = [module.name]
    for fpath in module.files[:5]:
        tag = PurePosixPath(fpath).stem
        if tag not in tags:
            tags.append(tag)

    return WikiPageContent(
        name=module.name,
        filename=f"{slug}.md",
        title=title,
        content=content,
        tags=tags,
        file_ids=file_ids_used,
        chunk_ids=list(module.chunks),
    )


import re as _re

_WIKILINK_PATTERN = _re.compile(r"\[\[([^\]]+)\]\]")


def _inject_coreference_wikilinks(pages: list[WikiPageContent]) -> None:
    """Add wikilinks between modules that share references to the same third module.

    If module A links to [[X]] and module B also links to [[X]], then A and B
    are related via co-reference. This is especially useful for connecting
    documents that discuss the same code modules without referencing each other.

    Only adds links where at least 2 shared references exist (noise threshold).
    """
    # Extract existing wikilinks per page
    page_links: dict[str, set[str]] = {}
    for page in pages:
        links = set(_WIKILINK_PATTERN.findall(page.content))
        if links:
            page_links[page.name] = links

    if len(page_links) < 2:
        return

    # Find co-reference pairs: modules sharing 2+ common link targets
    names = list(page_links.keys())
    coreference_links: dict[str, set[str]] = defaultdict(set)

    for i, name_a in enumerate(names):
        for name_b in names[i + 1:]:
            shared = page_links[name_a] & page_links[name_b]
            if len(shared) >= 2:
                # Only add if not already linked
                if name_b not in page_links.get(name_a, set()):
                    coreference_links[name_a].add(name_b)
                if name_a not in page_links.get(name_b, set()):
                    coreference_links[name_b].add(name_a)

    if not coreference_links:
        return

    # Inject wikilinks into page content
    for page in pages:
        new_links = coreference_links.get(page.name)
        if not new_links:
            continue

        # Merge with existing Related Modules or create new section
        if "## Related Modules" in page.content:
            # Add to existing section
            existing = set(_WIKILINK_PATTERN.findall(page.content))
            additions = sorted(new_links - existing)
            if additions:
                insert_lines = "\n".join(f"- [[{name}]]" for name in additions)
                page.content = page.content.replace(
                    "## Related Modules\n",
                    f"## Related Modules\n{insert_lines}\n",
                )
        else:
            # Create new section before External Dependencies or at end
            section = "\n## Related Modules\n\n" + "\n".join(
                f"- [[{name}]]" for name in sorted(new_links)
            ) + "\n"
            if "## External Dependencies" in page.content:
                page.content = page.content.replace(
                    "## External Dependencies", section + "\n## External Dependencies"
                )
            else:
                page.content = page.content.rstrip() + "\n" + section


def generate_all_wiki_pages(
    db: StoreDB, project_id: str,
) -> tuple[WikiPlan, list[WikiPageContent]]:
    """Generate wiki pages for all modules in a project.

    Returns (plan, pages) where pages are in topological order (leaves first).
    """
    edges = db.get_all_call_edges(project_id)
    all_chunks = db.get_chunks_by_project(project_id)
    all_files = db.get_all_files(project_id)

    chunk_map: dict[str, ChunkRecord] = {c.id: c for c in all_chunks}
    file_map: dict[str, FileRecord] = {f.id: f for f in all_files}

    forward, reverse = build_dependency_graph(edges)
    plan = generate_wiki_plan(db, project_id)

    pages: list[WikiPageContent] = []
    all_modules = plan.modules + plan.isolated_modules

    # Build chunk → module name mapping for wikilinks (call-edge based)
    chunk_to_module: dict[str, str] = {}
    for module in all_modules:
        for cid in module.chunks:
            chunk_to_module[cid] = module.name

    # Build filepath → module name mapping for wikilinks (content-reference based)
    filepath_to_module: dict[str, str] = {}
    for module in all_modules:
        for fpath in module.files:
            filepath_to_module[fpath] = module.name

    for module in all_modules:
        page = generate_module_wiki(
            module, chunk_map, file_map, forward, reverse,
            chunk_to_module, filepath_to_module,
        )
        pages.append(page)

    # Post-process: co-reference wikilinks between modules
    # If two modules both reference the same third module, they're related.
    # This connects documents that discuss the same code without referencing each other directly.
    _inject_coreference_wikilinks(pages)

    # Generate index page
    index_lines = ["# Wiki Index", ""]
    if plan.modules:
        index_lines.append(f"## Modules ({len(plan.modules)})")
        index_lines.append("")
        for m in plan.modules:
            index_lines.append(f"- [{m.name}]({m.name.lower().replace(' ', '-')}.md) — {m.file_count} files, {m.chunk_count} symbols")
        index_lines.append("")

    if plan.isolated_modules:
        index_lines.append(f"## Isolated ({len(plan.isolated_modules)})")
        index_lines.append("")
        for m in plan.isolated_modules:
            slug = m.name.lower().replace(" ", "-").replace("(", "").replace(")", "")
            index_lines.append(f"- [{m.name}]({slug}.md) — {m.file_count} files, {m.chunk_count} symbols")
        index_lines.append("")

    index_lines.append(f"**Coverage**: {plan.covered_chunks}/{plan.total_chunks} chunks ({plan.coverage*100:.1f}%)")

    pages.insert(0, WikiPageContent(
        name="index",
        filename="index.md",
        title="Wiki Index",
        content="\n".join(index_lines),
        tags=["index"],
        file_ids=[],
        chunk_ids=[],
    ))

    return plan, pages


def _deduplicate_names(modules: list[ModuleNode]) -> None:
    """Append numeric suffix to duplicate module names in-place."""
    name_count: dict[str, int] = defaultdict(int)
    for m in modules:
        name_count[m.name] += 1

    duplicates: dict[str, int] = {}
    for m in modules:
        if name_count[m.name] > 1:
            idx = duplicates.get(m.name, 0) + 1
            duplicates[m.name] = idx
            m.name = f"{m.name}-{idx}"
