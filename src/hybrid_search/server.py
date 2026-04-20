"""MCP Server entry point — exposes hybrid search tools to Claude Code.

Slim server: only 3 interactive tools exposed via MCP.
All admin/wiki operations are available via CLI:
  python -m hybrid_search.cli <command>
"""

from __future__ import annotations

import json
import logging
import sys
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
                            "description": "Current working directory. Auto-scopes search to the matching project.",
                        },
                    },
                    "required": ["query"],
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
            case _:
                return {"error": f"Unknown tool: {name}"}

    return server


def _filter_blank_stdin() -> None:
    """Drop blank lines from stdin before MCP's JSON-RPC parser sees them.

    Some MCP clients (Claude Desktop, etc.) send bare ``\\n`` between messages.
    The stdio transport tries to parse every line as a ``JSONRPCMessage``,
    so a blank line triggers a Pydantic ``ValidationError``. This installs an
    OS-level pipe that relays stdin while dropping blanks.

    Safe to call repeatedly from tests — the original fd is duped and the
    relay thread is a daemon. Bails out silently if stdin isn't a real fd
    (pytest captured stdin, closed stdin, etc.).
    """
    import os
    import threading

    try:
        stdin_fd = sys.stdin.fileno()
    except (OSError, ValueError):
        return  # pytest capture / no real stdin → nothing to relay

    try:
        r_fd, w_fd = os.pipe()
        saved_fd = os.dup(stdin_fd)
    except OSError:
        return

    def _relay() -> None:
        try:
            with os.fdopen(saved_fd, "rb") as src, os.fdopen(w_fd, "wb") as dst:
                for line in src:
                    if line.strip():
                        dst.write(line)
                        dst.flush()
        except Exception:
            pass

    threading.Thread(target=_relay, daemon=True).start()
    os.dup2(r_fd, stdin_fd)
    os.close(r_fd)
    sys.stdin = open(stdin_fd, "r", closefd=False)


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
    _filter_blank_stdin()
    asyncio.run(_run_server(config))


if __name__ == "__main__":
    main()
