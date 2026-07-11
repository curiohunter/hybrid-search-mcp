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
#   - Fast path (venv ready, revision unchanged) is one string compare.
#   - Slow path (first run / upgrade) backgrounds the pip install so the
#     session is never blocked; the user restarts once when it finishes.
#   - Upgrade detection is by git commit SHA (falling back to a content
#     hash of everything the plugin ships), NOT pyproject.toml alone: a
#     marketplace update that changes source without bumping the version
#     must still reinstall.
#   - Lock acquisition is ATOMIC (mkdir) — two concurrent SessionStarts
#     race the directory creation and exactly one wins; check-then-write
#     on a lock file would let both through.
#   - The lock always clears (trap on EXIT/HUP/INT/TERM in the installer)
#     and a stale lock — dead PID or older than 30 min — is reclaimed
#     instead of wedging every future session.

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA="${CLAUDE_PLUGIN_DATA:-$HOME/.hybrid-search/plugin-data}"
VENV="$DATA/venv"
PY="$VENV/bin/python"
LOCK="$DATA/revision.lock"
LOCKDIR="$DATA/.installing.lock"
LOG="$DATA/bootstrap.log"
STALE_SECONDS=1800

current_revision() {
    git -C "$ROOT" rev-parse HEAD 2>/dev/null && return 0
    # Not a git checkout (e.g. tarball source): hash everything the plugin
    # ships — a hooks/skills/scripts-only update must still reinstall.
    {
        cat "$ROOT/pyproject.toml" 2>/dev/null
        find "$ROOT/src" "$ROOT/skills" "$ROOT/hooks" "$ROOT/scripts" "$ROOT/.claude-plugin" \
            -type f -exec shasum -a 256 {} + 2>/dev/null | sort
    } | shasum -a 256 | cut -d' ' -f1
}

CUR_REV=$(current_revision)
[ -n "$CUR_REV" ] || exit 0

# Fast path: installed and at the current revision.
if [ -x "$PY" ] && [ "$(cat "$LOCK" 2>/dev/null)" = "$CUR_REV" ]; then
    exit 0
fi

mkdir -p "$DATA" 2>/dev/null || exit 0

acquire_lock() {
    # mkdir is atomic: of N concurrent bootstraps exactly one succeeds.
    if mkdir "$LOCKDIR" 2>/dev/null; then
        echo "$$ $(date +%s)" > "$LOCKDIR/owner"
        return 0
    fi
    lock_pid=$(cut -d' ' -f1 "$LOCKDIR/owner" 2>/dev/null)
    lock_ts=$(cut -d' ' -f2 "$LOCKDIR/owner" 2>/dev/null)
    now_ts=$(date +%s)
    age=$(( now_ts - ${lock_ts:-0} ))
    # Within a 120 s grace window the lock is trusted unconditionally (the
    # owner file may not be stamped yet); after that the installer PID must
    # be alive, and nothing survives past STALE_SECONDS.
    if [ "$age" -lt 120 ] || { [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null && [ "$age" -lt "$STALE_SECONDS" ]; }; then
        return 1
    fi
    rm -rf "$LOCKDIR"
    # Stale reclaim also races; only the mkdir winner proceeds.
    if mkdir "$LOCKDIR" 2>/dev/null; then
        echo "$$ $(date +%s)" > "$LOCKDIR/owner"
        return 0
    fi
    return 1
}

if ! acquire_lock; then
    echo "memory-layer: install still running in background (log: $LOG)"
    exit 0
fi

nohup sh -c '
    ROOT="$1"; DATA="$2"; VENV="$3"; PY="$4"; LOCK="$5"; REV="$6"; LOCKDIR="$7"
    echo "$$ $(date +%s)" > "$LOCKDIR/owner"
    trap "rm -rf \"$LOCKDIR\"" EXIT HUP INT TERM
    set -e
    if [ ! -x "$PY" ]; then
        python3.12 -m venv "$VENV" 2>/dev/null \
            || python3.11 -m venv "$VENV" 2>/dev/null \
            || python3 -m venv "$VENV"
    fi
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q "$ROOT"
    "$PY" -m hybrid_search.cli setup --global-only
    # Revision lock is written only after a fully successful install, so a
    # failed attempt retries on the next SessionStart.
    printf "%s" "$REV" > "$LOCK"
' _ "$ROOT" "$DATA" "$VENV" "$PY" "$LOCK" "$CUR_REV" "$LOCKDIR" >"$LOG" 2>&1 &

if [ -x "$PY" ]; then
    echo "memory-layer: updating to new revision in background (~1 min)."
else
    echo "memory-layer: first-time install started in background (~1-2 min)."
fi
echo "Restart Claude Code when it finishes to pick up the changes."
exit 0
