#!/bin/bash
# Hybrid Search — post-checkout hook (reference implementation)
# Auto delta-reindex on branch switch (background, non-blocking).
# The actual hook installed by `hybrid-search-mcp install-hook` bakes the
# venv python path into the script; this file is kept for reference / manual
# install.

# Args: $1=prev_head, $2=new_head, $3=flag (1=branch switch, 0=file checkout)
[ "$3" = "1" ] || exit 0

PROJECT_DIR="$(git rev-parse --show-toplevel)"

# Skip when hybrid-search isn't initialized here (no auto-bootstrap)
[ -d "$PROJECT_DIR/.hybrid-search" ] || exit 0

# Shared lock with post-commit
LOCK_FILE="$PROJECT_DIR/.hybrid-search/.reindex.lock"
if [ -f "$LOCK_FILE" ]; then
  LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null)
  if kill -0 "$LOCK_PID" 2>/dev/null; then
    exit 0
  fi
  rm -f "$LOCK_FILE"
fi

if command -v hybrid-search-mcp >/dev/null 2>&1; then
    nohup bash -c '
      echo $$ > "'"$LOCK_FILE"'"
      hybrid-search-mcp reindex --wiki-scope affected --cwd "'"$PROJECT_DIR"'" || true
      rm -f "'"$LOCK_FILE"'"
    ' > /dev/null 2>&1 &
else
    echo "hybrid-search-mcp not found on PATH. Install with: pip install hybrid-search-mcp"
fi
