# CLAUDE.md — Notion-Claude Optimizer

Notion-Claude Optimizer mirrors your entire Notion workspace into a local database (SQLite by default, PostgreSQL optional) and exposes it to Claude Desktop via MCP. It strips out Notion's UI scaffolding and restructures the data purely for AI consumption — making Claude up to 98% faster at searching your workspace and up to 97% cheaper per query compared to hitting the Notion API directly (search latency ~30 ms vs ~1,500 ms; ~200 tokens vs ~5,000 tokens per discovery query).

## MCP server name
`Notion Local DB` (configurable — the user may have renamed it in Settings)

## Recommended query flow

Start cheap; fetch detail only when needed.

```
1. get_stats()      → confirm mirror is live and how fresh it is
2. get_index()      → full workspace map (title, keywords, summary, type, token estimate)
3. search("topic")  → ranked search for something specific
4. get_page(id)     → full page content once you know which page you need
```

## Architecture: read-only mirror

The local DB is a **read-only mirror** of Notion. Always read from it using the MCP tools below. For any writes (new pages, property updates, status changes), use Claude Desktop's native Notion integration — this preserves page layout, block structure, and inter-page links. The local DB picks up changes on the next sync cycle (every few minutes by default).

## Available MCP tools

| Tool | When to use |
|------|-------------|
| `get_stats()` | First call — confirms the DB is live and how fresh the mirror is |
| `get_index()` | Load the complete workspace map in one call before doing anything else |
| `search(query, limit=10)` | Keyword/full-text search; returns ranked results with keywords and summaries |
| `get_page(notion_id)` | Full content for one page: title, AI summary, all properties, media paths |
| `list_recent(limit=20)` | Pages sorted by most recently updated — useful for "what changed?" |

All tools fall back to the live Notion API if the local DB is unreachable.

## Efficiency rules

- Call `get_index()` once per session to build your workspace map; don't repeat it.
- Use `search()` before `get_page()` unless you already have the `notion_id`.
- `get_page()` returns full raw JSON — only call it for pages you actually need.
- Never try to scan all pages at once; use `get_index()` or `search()` for discovery.

## Key tables

- `page_index` — compact keyword index rebuilt after every sync. Primary table for discovery.
- `workspace_mirror` — full mirror with raw Notion JSON, AI summary, media paths, timestamps.

The database backend is either SQLite (`data/notion_mirror.db`) or PostgreSQL, depending on user configuration. Use the MCP tools above rather than direct SQL — they work regardless of backend.

## Useful file locations

- MCP server tools: `src/mcp/server.py`
- Sync engine: `src/sync/engine.py`
- Self-healing agent: `src/sync/healer.py`
- DB manager (dual backend): `src/db/database.py`
- SQLite schema: `src/db/sql/schema_sqlite.sql`
- PostgreSQL schema: `src/db/sql/schema.sql`
- Keyword indexer: `src/db/indexer.py`
- Backup manager: `src/db/backup.py`
- Tray app: `src/ui/tray.py`
- Settings dialog: `src/ui/settings.py`
- MCP config writer: `src/mcp/configurator.py`
