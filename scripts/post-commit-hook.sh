#!/bin/bash
# Hybrid Search — post-commit hook
# Auto delta-reindex after every commit (background, non-blocking)
# Install: cp scripts/post-commit-hook.sh <project>/.git/hooks/post-commit && chmod +x

HYBRID_SEARCH_VENV="/Users/ian/project/claude_project/hybrid-search-mcp/.venv/bin/python"
PROJECT_DIR="$(git rev-parse --show-toplevel)"

# Run reindex in background (non-blocking, no terminal output)
nohup "$HYBRID_SEARCH_VENV" -m hybrid_search.cli reindex --cwd "$PROJECT_DIR" \
    > /dev/null 2>&1 &
