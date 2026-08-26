"""Hybrid Search MCP Server — BM25 + Vector search with cross-language support."""

# Single version source = the installed distribution metadata (which
# comes from pyproject). A hardcoded constant here shipped "0.1.0" into
# every generated Codex plugin manifest regardless of release (2026-07-27
# Mac-mini E2E, F10 residual) — and because the manifest writer is
# content-idempotent, an installed 0.1.0 manifest then never upgraded.
# With the version read from metadata, a version bump changes the
# rendered manifest and the idempotent write replaces it naturally.
try:
    from importlib.metadata import version as _dist_version

    __version__ = _dist_version("memory-layer-mcp")
except Exception:  # source checkout without an installed dist
    __version__ = "0.0.0+source"
