#!/bin/sh
# Plugin bootstrap — runs on every SessionStart via hooks/hooks.json.
#
# The plugin is a thin installer: registering the MCP server through the
# plugin manifest would namespace the tool as
# mcp__plugin_memory-layer_..._hybrid_search and break every routing
# contract that names mcp__hybrid-search__hybrid_search (CLAUDE.md blocks,
# session hints, docs). So instead this script provisions a persistent
# venv and delegates to `cli setup --global-only`, which registers the
# exact same global surface a pip install would.
#
# Invariants:
#   - MUST always exit 0 — a non-zero SessionStart hook surfaces as a
#     phantom error on every session (the 0.5.1 lesson).
#   - Fast path (venv ready, pyproject unchanged) is one diff: ~5 ms.
#   - Slow path (first run / upgrade) backgrounds the pip install so the
#     session is never blocked; the user restarts once when it finishes.

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.hybrid-search/plugin-data}"
VENV="$DATA/venv"
PY="$VENV/bin/python"
LOCK="$DATA/pyproject.lock"
LOG="$DATA/bootstrap.log"

# Fast path: installed and up to date.
if [ -x "$PY" ] && diff -q "$ROOT/pyproject.toml" "$LOCK" >/dev/null 2>&1; then
    exit 0
fi

# Another bootstrap already running? Don't stack installs.
if [ -f "$DATA/.installing" ]; then
    echo "memory-layer: install still running in background (log: $LOG)"
    exit 0
fi

mkdir -p "$DATA" 2>/dev/null || exit 0
touch "$DATA/.installing"

nohup sh -c '
    ROOT="$1"; DATA="$2"; VENV="$3"; PY="$4"; LOCK="$5"
    set -e
    if [ ! -x "$PY" ]; then
        python3.12 -m venv "$VENV" 2>/dev/null \
            || python3.11 -m venv "$VENV" 2>/dev/null \
            || python3 -m venv "$VENV"
    fi
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q "$ROOT"
    cp "$ROOT/pyproject.toml" "$LOCK"
    "$PY" -m hybrid_search.cli setup --global-only
    rm -f "$DATA/.installing"
' _ "$ROOT" "$DATA" "$VENV" "$PY" "$LOCK" >"$LOG" 2>&1 &

echo "memory-layer: first-time install started in background (~1-2 min)."
echo "Restart Claude Code when it finishes to activate the hybrid_search MCP server."
exit 0
