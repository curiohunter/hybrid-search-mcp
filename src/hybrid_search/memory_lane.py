"""What counts as the memory layer's own output rather than project source.

Everything the memory layer writes — qa logs, memory cards, synthesised
wiki pages, indexed conversations, commit history — lands inside the
project tree. It is retrieval *content*: worth searching, never worth
treating as source.

Passes that reason about a project's structure must exclude it, or the
system starts describing itself. Two failures came from exactly that:
wiki planning produced `-isolated-isolated` page chains by re-reading its
own pages, and module discovery grouped 1,869 qa logs into a single
2,442-file "module" whose 100KB summary then won unrelated queries.

The list lived in three places and was missing from a fourth. It lives
here now.
"""

from __future__ import annotations

MEMORY_LANE_PREFIXES: tuple[str, ...] = (
    ".hybrid-search/",
    ".conversations/",
    ".git-history/",
)


def is_memory_lane_path(relative_path: str) -> bool:
    """True when ``relative_path`` is memory-layer output, not project source."""
    return relative_path.startswith(MEMORY_LANE_PREFIXES)
