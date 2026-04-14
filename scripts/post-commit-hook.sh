#!/bin/bash
# Hybrid Search — post-commit hook
# Auto delta-reindex after every commit (background, non-blocking)
# Install: cp scripts/post-commit-hook.sh <project>/.git/hooks/post-commit && chmod +x

PROJECT_DIR="$(git rev-parse --show-toplevel)"

# Prefer pip-installed CLI, fall back to venv python
if command -v hybrid-search-mcp >/dev/null 2>&1; then
    nohup hybrid-search-mcp reindex --git-delta --wiki-scope affected --cwd "$PROJECT_DIR" \
        > /dev/null 2>&1 &
else
    echo "hybrid-search-mcp not found on PATH. Install with: pip install hybrid-search-mcp"
fi
