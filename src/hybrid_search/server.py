"""MCP Server entry point — exposes hybrid search tools to Claude Code.

Slim server: only 3 interactive tools exposed via MCP.
All admin/wiki operations are available via CLI:
  python -m hybrid_search.cli <command>
"""

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
from hybrid_search.tools.trace import handle_trace_callers, handle_trace_callees

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
    """Create and configure the MCP server with 3 tools."""
    server = Server("hybrid-search-mcp")

    # Hot-reloadable config wrapper
    actual_path = config_path or (DEFAULT_DATA_DIR / "config.toml")
    hot_config = _HotReloadableConfig(config, actual_path)

    # Initialize shared resources
    registry = ProjectRegistry(config.global_dir)
    embedder = Embedder(config.embedding, config.models_dir)
    pipeline = IndexingPipeline(config, registry, embedder)
    orchestrator = SearchOrchestrator(config, registry, embedder)

    _state = {"config": config, "embedder": embedder, "pipeline": pipeline, "orchestrator": orchestrator}

    def _maybe_reload() -> tuple[Config, Embedder, IndexingPipeline, SearchOrchestrator]:
        if hot_config.check_reload():
            new_cfg = hot_config.config
            old_cfg = _state["config"]
            new_embedder = _state["embedder"]
            if new_cfg.embedding != old_cfg.embedding:
                logger.info("Embedding config changed, reinitializing embedder")
                new_embedder = Embedder(new_cfg.embedding, new_cfg.models_dir)
            _state["config"] = new_cfg
            _state["embedder"] = new_embedder
            _state["pipeline"] = IndexingPipeline(new_cfg, registry, new_embedder)
            _state["orchestrator"] = SearchOrchestrator(new_cfg, registry, new_embedder)
        return _state["config"], _state["embedder"], _state["pipeline"], _state["orchestrator"]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="hybrid_search",
                description=(
                    "Search code and docs using hybrid BM25 + semantic vector search "
                    "with cross-language support (Korean ↔ English). "
                    "Set bm25_weight=0 for pure semantic search."
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
                            "description": "BM25 weight (0=pure semantic, 1=pure keyword). Auto-classified if omitted.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Current working directory. Boosts results from the matching project.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="trace_callers",
                description="Find all functions that call the given function (reverse call graph).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Function/method name or qualified name"},
                        "chunk_id": {"type": "string", "description": "Chunk ID from a prior search result (more precise)"},
                        "project": {"type": "string"},
                        "depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 10},
                        "min_confidence": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                    },
                },
            ),
            Tool(
                name="trace_callees",
                description="Find all functions called by the given function (forward call graph).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Function/method name or qualified name"},
                        "chunk_id": {"type": "string", "description": "Chunk ID from a prior search result"},
                        "project": {"type": "string"},
                        "depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 10},
                        "min_confidence": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            cfg, emb, pipe, orch = _maybe_reload()
            result = _dispatch_tool(name, arguments, cfg, registry)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    def _dispatch_tool(name: str, args: dict, config: Config, registry: ProjectRegistry) -> dict:
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

    return server


async def _run_server(config: Config) -> None:
    """Run the MCP server over stdio."""
    server = create_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    import asyncio
    asyncio.run(_run_server(config))


if __name__ == "__main__":
    main()
