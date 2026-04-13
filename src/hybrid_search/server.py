"""MCP Server entry point — exposes hybrid search tools to Claude Code."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from hybrid_search.config import Config, DEFAULT_DATA_DIR, load_config
from hybrid_search.index.embedder import Embedder
from hybrid_search.index.pipeline import IndexingPipeline
from hybrid_search.project import ProjectRegistry
from hybrid_search.search.orchestrator import SearchOrchestrator
from hybrid_search.tools.hybrid_search import handle_hybrid_search
from hybrid_search.tools.index import handle_index_project, handle_index_status
from hybrid_search.tools.projects import handle_list_projects, handle_remove_project
from hybrid_search.tools.semantic_search import handle_semantic_search
from hybrid_search.tools.symbols import handle_search_symbols
from hybrid_search.tools.trace import handle_trace_callers, handle_trace_callees
from hybrid_search.tools.wiki import (
    handle_compile_to_wiki,
    handle_lookup_wiki,
    handle_check_wiki_staleness,
    handle_refresh_wiki_page,
)

logger = logging.getLogger("hybrid_search")


class _HotReloadableConfig:
    """Watches config.toml mtime and reloads when changed."""

    def __init__(self, config: Config, config_path: Path) -> None:
        self.config = config
        self._config_path = config_path
        self._last_mtime: float = self._get_mtime()

    def _get_mtime(self) -> float:
        try:
            return self._config_path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def check_reload(self) -> bool:
        """Check if config file changed and reload if so. Returns True if reloaded."""
        current_mtime = self._get_mtime()
        if current_mtime <= self._last_mtime:
            return False
        try:
            new_config = load_config(self._config_path)
            self.config = new_config
            self._last_mtime = current_mtime
            logger.info("Config reloaded from %s", self._config_path)
            return True
        except Exception as e:
            logger.warning("Config reload failed, keeping previous: %s", e)
            return False


def create_server(config: Config, config_path: Path | None = None) -> Server:
    """Create and configure the MCP server with all tools."""
    server = Server("hybrid-search-mcp")

    # Hot-reloadable config wrapper
    actual_path = config_path or (DEFAULT_DATA_DIR / "config.toml")
    hot_config = _HotReloadableConfig(config, actual_path)

    # Initialize shared resources
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    pipeline = IndexingPipeline(config, registry, embedder)
    orchestrator = SearchOrchestrator(config, registry, embedder)

    # Mutable state container for hot-reload closure
    _state = {"config": config, "embedder": embedder, "pipeline": pipeline, "orchestrator": orchestrator}

    def _maybe_reload() -> tuple[Config, Embedder, IndexingPipeline, SearchOrchestrator]:
        """Check for config changes and rebuild components if needed."""
        if hot_config.check_reload():
            new_cfg = hot_config.config
            old_cfg = _state["config"]
            # Rebuild embedder if model/backend changed
            new_embedder = _state["embedder"]
            if new_cfg.embedding != old_cfg.embedding:
                logger.info("Embedding config changed, reinitializing embedder")
                new_embedder = Embedder(new_cfg.embedding, new_cfg.models_dir)
            _state["config"] = new_cfg
            _state["embedder"] = new_embedder
            _state["pipeline"] = IndexingPipeline(new_cfg, registry, new_embedder)
            _state["orchestrator"] = SearchOrchestrator(new_cfg, registry, new_embedder)
        return _state["config"], _state["embedder"], _state["pipeline"], _state["orchestrator"]

    # -- Tool definitions --

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="hybrid_search",
                description=(
                    "Search code and documentation using hybrid BM25 + semantic vector search "
                    "with cross-language support (Korean ↔ English). "
                    "Use this tool when: (1) the automatic MindVault context is insufficient, "
                    "(2) you need cross-language search (Korean ↔ English), "
                    "(3) you need semantic/conceptual matching beyond exact keywords, "
                    "or (4) you need call graph tracing."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (Korean or English)"},
                        "project": {"type": "string", "description": "Project name. Omit for all projects."},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                        "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g., '*.ts')"},
                        "node_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by: function, class, interface, method, type, etc.",
                        },
                        "bm25_weight": {
                            "type": "number",
                            "default": 0.5,
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "Weight for BM25 (0-1). Auto-classified if omitted.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Current working directory. When set, boosts results from the matching project.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="semantic_search",
                description=(
                    "Pure semantic vector search. Best for cross-language queries "
                    "(e.g., Korean query → English code). "
                    "Use this tool when: (1) the automatic MindVault context is insufficient, "
                    "(2) you need cross-language search (Korean ↔ English), "
                    "(3) you need semantic/conceptual matching beyond exact keywords."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (Korean or English)"},
                        "project": {"type": "string", "description": "Project name. Omit for all projects."},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                        "file_pattern": {"type": "string", "description": "Glob pattern to filter files"},
                        "node_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by: function, class, interface, method, type, etc.",
                        },
                        "similarity_threshold": {
                            "type": "number",
                            "default": 0.5,
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="search_symbols",
                description="Search for symbols (functions, classes, types) by name with fuzzy matching",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Symbol name or pattern"},
                        "project": {"type": "string"},
                        "node_types": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="index_project",
                description="Index or re-index a project. Uses delta indexing if index already exists.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project_path": {"type": "string", "description": "Absolute path to project root"},
                        "project_name": {"type": "string", "description": "Human-readable project name"},
                        "force": {"type": "boolean", "default": False, "description": "Force full re-index"},
                    },
                    "required": ["project_path"],
                },
            ),
            Tool(
                name="index_status",
                description="Show indexing status for one or all projects",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name. Omit for all."},
                    },
                },
            ),
            Tool(
                name="list_projects",
                description="List all registered projects with their paths and index status",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="remove_project",
                description="Unregister a project and delete its index data. Does not delete source files.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name to remove"},
                        "keep_index": {"type": "boolean", "default": False},
                    },
                    "required": ["project"],
                },
            ),
            Tool(
                name="trace_callers",
                description=(
                    "Find all functions that call the given function (reverse call graph). "
                    "Provide chunk_id (precise) or symbol (name-based). "
                    "If both given, chunk_id takes precedence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Function/method name or qualified name (e.g., 'signIn' or 'AuthService.signIn')",
                        },
                        "chunk_id": {
                            "type": "string",
                            "description": "Chunk ID from a prior search result. More precise than symbol name.",
                        },
                        "project": {"type": "string"},
                        "depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 1,
                            "maximum": 10,
                            "description": "Max depth of call graph traversal",
                        },
                        "min_confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "default": "medium",
                            "description": "Minimum resolution confidence for call edges",
                        },
                    },
                },
            ),
            Tool(
                name="trace_callees",
                description=(
                    "Find all functions called by the given function (forward call graph). "
                    "Provide chunk_id (precise) or symbol (name-based). "
                    "If both given, chunk_id takes precedence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Function/method name or qualified name",
                        },
                        "chunk_id": {
                            "type": "string",
                            "description": "Chunk ID from a prior search result",
                        },
                        "project": {"type": "string"},
                        "depth": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 1,
                            "maximum": 10,
                        },
                        "min_confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "default": "medium",
                        },
                    },
                },
            ),
            Tool(
                name="compile_to_wiki",
                description=(
                    "Save a compiled wiki page from search results. "
                    "Claude writes the content; the server tracks which source files were used "
                    "so it can detect when the page becomes stale."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                        "query": {"type": "string", "description": "The question this wiki page answers"},
                        "title": {"type": "string", "description": "Page title"},
                        "content": {"type": "string", "description": "Markdown content (written by you)"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Topic tags for secondary lookup",
                        },
                        "source_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Chunk IDs from search results used to write this page",
                        },
                    },
                    "required": ["project", "query", "title", "content"],
                },
            ),
            Tool(
                name="lookup_wiki",
                description=(
                    "Look up a cached wiki page by query or tag. "
                    "Returns the page content with staleness status. "
                    "Use before re-searching to check if a compiled answer already exists."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                        "query": {"type": "string", "description": "Question to look up"},
                        "tag": {"type": "string", "description": "Tag to search by"},
                    },
                    "required": ["project"],
                },
            ),
            Tool(
                name="check_wiki_staleness",
                description=(
                    "Check if wiki pages are stale (source files changed since compilation). "
                    "Omit page_id to check all pages in the project."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                        "page_id": {"type": "string", "description": "Specific page ID, or omit for all"},
                    },
                    "required": ["project"],
                },
            ),
            Tool(
                name="refresh_wiki_page",
                description=(
                    "Update a stale wiki page with new content. "
                    "Re-snapshots file hashes so staleness resets."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                        "page_id": {"type": "string", "description": "Page ID to refresh"},
                        "content": {"type": "string", "description": "Updated markdown content"},
                        "source_chunk_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "New chunk IDs if sources changed",
                        },
                    },
                    "required": ["project", "page_id", "content"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            cfg, emb, pipe, orch = _maybe_reload()
            result = _dispatch_tool(name, arguments, cfg, registry, emb, pipe, orch)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    return server


def _dispatch_tool(
    name: str,
    args: dict,
    config: Config,
    registry: ProjectRegistry,
    embedder: Embedder,
    pipeline: IndexingPipeline,
    orchestrator: SearchOrchestrator,
) -> dict:
    """Route tool calls to handlers."""
    match name:
        case "hybrid_search":
            return handle_hybrid_search(
                orchestrator=orchestrator,
                query=args["query"],
                project=args.get("project"),
                limit=args.get("limit", 10),
                file_pattern=args.get("file_pattern"),
                node_types=args.get("node_types"),
                bm25_weight=args.get("bm25_weight"),
                cwd=args.get("cwd"),
            )
        case "semantic_search":
            return handle_semantic_search(
                config=config,
                registry=registry,
                embedder=embedder,
                query=args["query"],
                project=args.get("project"),
                limit=args.get("limit", 10),
                file_pattern=args.get("file_pattern"),
                node_types=args.get("node_types"),
                similarity_threshold=args.get("similarity_threshold", 0.5),
            )
        case "search_symbols":
            return handle_search_symbols(
                config=config,
                registry=registry,
                name=args["name"],
                project=args.get("project"),
                node_types=args.get("node_types"),
            )
        case "index_project":
            return handle_index_project(
                pipeline=pipeline,
                project_path=args["project_path"],
                project_name=args.get("project_name"),
                force=args.get("force", False),
            )
        case "index_status":
            return handle_index_status(
                pipeline=pipeline,
                project=args.get("project"),
            )
        case "list_projects":
            return handle_list_projects(registry)
        case "remove_project":
            return handle_remove_project(
                config=config,
                registry=registry,
                project=args["project"],
                keep_index=args.get("keep_index", False),
            )
        case "trace_callers":
            return handle_trace_callers(
                config=config,
                registry=registry,
                symbol=args.get("symbol"),
                chunk_id=args.get("chunk_id"),
                project=args.get("project"),
                depth=args.get("depth", 2),
                min_confidence=args.get("min_confidence", "medium"),
            )
        case "trace_callees":
            return handle_trace_callees(
                config=config,
                registry=registry,
                symbol=args.get("symbol"),
                chunk_id=args.get("chunk_id"),
                project=args.get("project"),
                depth=args.get("depth", 2),
                min_confidence=args.get("min_confidence", "medium"),
            )
        case "compile_to_wiki":
            return handle_compile_to_wiki(
                config=config,
                registry=registry,
                project=args["project"],
                query=args["query"],
                title=args["title"],
                content=args["content"],
                tags=args.get("tags"),
                source_chunk_ids=args.get("source_chunk_ids"),
            )
        case "lookup_wiki":
            return handle_lookup_wiki(
                config=config,
                registry=registry,
                project=args["project"],
                query=args.get("query"),
                tag=args.get("tag"),
            )
        case "check_wiki_staleness":
            return handle_check_wiki_staleness(
                config=config,
                registry=registry,
                project=args["project"],
                page_id=args.get("page_id"),
            )
        case "refresh_wiki_page":
            return handle_refresh_wiki_page(
                config=config,
                registry=registry,
                project=args["project"],
                page_id=args["page_id"],
                content=args["content"],
                source_chunk_ids=args.get("source_chunk_ids"),
            )
        case _:
            return {"error": f"Unknown tool: {name}"}


async def _run_server(config: Config) -> None:
    """Run the MCP server over stdio."""
    server = create_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    logger.info("Starting Hybrid Search MCP Server (data_dir=%s)", config.data_dir)

    import asyncio
    asyncio.run(_run_server(config))


if __name__ == "__main__":
    main()
