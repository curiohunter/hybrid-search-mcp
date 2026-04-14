# Hybrid Search MCP

Hybrid BM25 + Vector search for codebases.
Cross-language search (Korean ↔ English) across code and docs.

---

## Quick Start

### Requirements

- Python 3.11+
- OpenAI API key ([get one here](https://platform.openai.com/api-keys))

### Install

```bash
pip install hybrid-search-mcp
```

Or from source:
```bash
git clone https://github.com/curiohunter/hybrid-search-mcp.git
cd hybrid-search-mcp
pip install -e .
```

### Set API key

```bash
export OPENAI_API_KEY=sk-...
```

Or create `~/.env.local`:
```
OPENAI_API_KEY=sk-...
```

### First search in 30 seconds

```bash
cd your-project/
hybrid-search-mcp index .
hybrid-search-mcp search "authentication flow"
```

That's it. Your project is indexed and searchable.

---

## CLI Usage

```bash
# Index a project
hybrid-search-mcp index .                    # current directory
hybrid-search-mcp index /path/to/project     # specific path
hybrid-search-mcp index . --force            # full re-index

# Search
hybrid-search-mcp search "login handler"
hybrid-search-mcp search "인증 로직"                     # Korean works
hybrid-search-mcp search "handleSubmit" --node-types function
hybrid-search-mcp search "migration" --file-pattern "*.sql"
hybrid-search-mcp search "auth" --json                   # JSON output
hybrid-search-mcp search "query" --limit 20

# Status & maintenance
hybrid-search-mcp status                     # show indexed projects
hybrid-search-mcp reindex --git-delta --cwd . # delta reindex (changed files only)
hybrid-search-mcp stale --cwd .              # check stale wiki pages
```

### Query auto-classification

The search engine automatically adjusts BM25/vector weights based on query type:

| Query | Type | BM25 weight |
|-------|------|-------------|
| `handleLogin` | Exact symbol | 0.8 (keyword-heavy) |
| `로그인 처리` | Korean NL | 0.15 (semantic-heavy) |
| `auth middleware` | English NL | 0.4 (balanced) |

---

## Claude Code Integration (Optional)

If you use Claude Code, hybrid-search-mcp becomes an MCP tool with auto-indexing.

### Setup

```bash
hybrid-search-mcp setup
```

This registers:
- MCP server in `~/.claude.json`
- Auto-index hook (indexes new projects on first file read)
- Stale wiki warning hook
- Wiki gap notification hook

Restart Claude Code after setup.

### Skills

Copy skills from `skills/` directory to `~/.claude/skills/`:

| Skill | When | Frequency |
|-------|------|-----------|
| `/setup-hybrid-search` | First install | Once |
| `/bootstrap-wiki` | Project onboarding | Per project |
| `/search` | Code/doc search | Every time |
| `/save-wiki` | Save analysis to wiki | Optional |
| `/maintain` | Index/wiki maintenance | Occasionally |

### Automation

| Trigger | Action | User action |
|---------|--------|-------------|
| Commit | Git delta reindex + affected wiki refresh | None |
| Before Edit/Write | STALE.md warning | Update wiki |
| After Edit/Write | Undocumented module alert | Add wiki |

---

## How It Works

### Search strategy — intent-based routing

| Query type | Primary | Fallback | Example |
|-----------|---------|----------|---------|
| Structure/relations | Wiki | hybrid_search | "Who calls this function?" |
| Feature exploration | hybrid_search | Wiki | "Explain the billing feature" |
| Exact lookup | Grep | Read | "Where is handleSubmit?" |
| Design/context | hybrid_search | Wiki | "Why is it designed this way?" |
| Schema/DB | hybrid_search | Grep | "problems table history" |

### Benchmark (1,776 files)

| Metric | hybrid+Wiki | Grep+Read |
|--------|-------------|-----------|
| Tool calls | 2-3 | 10-15 |
| Time | ~3s | 20-30s |
| Accuracy | 90%+ | Noisy |
| Token usage | Low | High |

---

## Tech Stack

| Component | Stack |
|-----------|-------|
| Embedding | OpenAI `text-embedding-3-small` |
| BM25 | tantivy-py (Rust) |
| Vector DB | USearch HNSW (C++) |
| AST parsing | tree-sitter (C), 14 languages |
| Storage | SQLite WAL |

Supported languages: TypeScript, JavaScript, Python, Rust, Go, Ruby, Java, C, C++, Swift, Kotlin, CSS, HTML, SQL

---

## Performance

| Operation | Time | Cost |
|-----------|------|------|
| First index (1,776 files) | ~165s | ~$0.04 |
| Git delta (post-commit) | ~2s | Minimal |
| Search | <2s | Free |

---

## Data locations

```
~/.hybrid-search/                        # Global
├── config.toml
└── projects/{hash}/store.db

<project>/.hybrid-search/                # Per project
├── wiki/
│   ├── index.md
│   ├── STALE.md
│   └── {module}.md
└── wiki-gaps.txt
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `index <path>` | Index a project |
| `search <query>` | Hybrid search |
| `serve` | Start MCP server (for Claude Code) |
| `setup` | Register MCP server + hooks in Claude Code |
| `status` | Show indexed projects |
| `reindex --cwd .` | Delta reindex |
| `reindex --force --cwd .` | Full reindex |
| `stale --cwd .` | Check stale wiki pages |
| `install-hook --cwd .` | Install post-commit hook |
| `remove-project <name>` | Unregister a project |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `OPENAI_API_KEY not found` | Set env var or create `~/.env.local` |
| Results from wrong project | Use `--cwd` or `--project` to scope |
| Too few results | `hybrid-search-mcp index . --force` |
| Rate limit errors | Auto-retry with 0.2s batch interval |
| Hooks not working | `hybrid-search-mcp setup` (re-run) |

---

## License

MIT
