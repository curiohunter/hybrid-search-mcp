---
description: Remove the Memory Layer's global surface (MCP registration, hooks, skills), then guide plugin removal.
allowed-tools: Bash, Read
---

# Memory Layer teardown

Remove everything `setup` registered globally. The plugin installs the
CLI into its own venv (not on PATH), so run the teardown binary by its
full path:

```bash
"${CLAUDE_PLUGIN_DATA:-$HOME/.hybrid-search/plugin-data}/venv/bin/hybrid-search-mcp" teardown
```

If that binary does not exist (pip install instead of plugin), fall back to:

```bash
hybrid-search-mcp teardown 2>/dev/null || pipx run memory-layer-mcp teardown
```

Report what was removed and what was kept (user-owned skills/hooks are
preserved by ownership checks). Then remind the user to finish with:

```
/plugin uninstall memory-layer@curiohunter
```

and restart Claude Code. Per-project files (`.hybrid-search/`, the
CLAUDE.md routing block) are intentionally untouched — mention that they
can be removed per project if desired.
