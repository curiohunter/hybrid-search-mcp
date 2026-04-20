#!/bin/bash
# Hybrid Search — post-commit hook (reference implementation)
# Auto delta-reindex after every commit (background, non-blocking).
# Install: cp scripts/post-commit-hook.sh <project>/.git/hooks/post-commit && chmod +x

PROJECT_DIR="$(git rev-parse --show-toplevel)"

# M3: capture diff synchronously so the deferred reindex sees THIS commit,
# not whatever HEAD~1..HEAD resolves to later (racy when rapid re-commits).
if git rev-parse HEAD~1 >/dev/null 2>&1; then
    HOOK_DIFF="$(git diff --name-status HEAD~1 HEAD 2>/dev/null)"
    if [ -z "$HOOK_DIFF" ]; then
        exit 0
    fi
    export HYBRID_SEARCH_CHANGED_STATUS="$HOOK_DIFF"
fi

# Prefer pip-installed CLI, fall back to venv python
if command -v hybrid-search-mcp >/dev/null 2>&1; then
    nohup hybrid-search-mcp reindex --git-delta --wiki-scope affected --cwd "$PROJECT_DIR" \
        > /dev/null 2>&1 &
else
    echo "hybrid-search-mcp not found on PATH. Install with: pip install hybrid-search-mcp"
fi
