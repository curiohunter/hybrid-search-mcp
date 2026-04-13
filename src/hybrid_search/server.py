"""MCP Server entry point — exposes hybrid search tools to Claude Code."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from hybrid_search.config import Config, load_config
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

logger = logging.getLogger("hybrid_search")


def create_server(config: Config) -> Server:
    """Create and configure the MCP server with all tools."""
    server = Server("hybrid-search-mcp")

    # Initialize shared resources
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    pipeline = IndexingPipeline(config, registry, embedder)
    orchestrator = SearchOrchestrator(config, registry, embedder)

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
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            result = _dispatch_tool(name, arguments, config, registry, embedder, pipeline, orchestrator)
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
