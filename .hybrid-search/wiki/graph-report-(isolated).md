# Graph Report (Isolated)
> synthesized: 2026-04-14

## Overview

Graph Report is a legacy artifact from MindVault integration — a markdown report containing community detection results, god nodes, surprising connections, and suggested questions generated from a knowledge graph analysis. It exists as a static output file (`mindvault-out/GRAPH_REPORT.md`) and is not functionally integrated with the hybrid-search system.

## Key Design Decisions

- **Isolated grouping**: No call graph edges connect this file to the hybrid-search codebase, placing it in directory-based fallback grouping

## Data Flow

```
MindVault analysis (external)
  → GRAPH_REPORT.md written to mindvault-out/
    → indexed as document chunks by hybrid-search scanner
      → searchable via hybrid_search but not functionally integrated
```

## Caveats

- This is a static output file, not executable code — it will never have call edges or meaningful staleness tracking
- The file may no longer exist on disk; wiki references to this module may be stale

## Related Modules

(none -- this module is isolated with no code-level relationships)

<details>
<summary>Structure (auto-generated)</summary>

## Files

- `mindvault-out/GRAPH_REPORT.md`

## Symbols

### `mindvault-out/GRAPH_REPORT.md`

- **MindVault Graph Report** (section, L1)
- **Overview** (section, L5)
- **Communities** (section, L11)
- **God Nodes** (section, L40)
- **Surprising Connections** (section, L49)
- **Suggested Questions** (section, L61)

</details>