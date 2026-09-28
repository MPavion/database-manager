# CLAUDE.md

This app mirrors Notion into local PostgreSQL and exposes it to Claude Desktop via MCP. The local DB is significantly faster and cheaper to query than Notion directly.

## MCP server name
`Notion Local DB` (configurable via Settings → Claude Desktop MCP)

## Recommended query flow

Start cheap; fetch detail only when needed.

```
1. get_stats()      → check page count and how fresh the mirror is
2. get_index()      → full workspace map (title, keywords, summary, type, token estimate)
3. search("topic")  → ranked search if you're looking for something specific
4. get_page(id)     → full page content once you know which page you need
```

## Architecture: read-only mirror

The local DB is a **read-only mirror** of Notion. Claude reads from it; writes go directly to Notion via Claude Desktop's native Notion integration. This preserves page layout, block structure, and inter-page links. The DB picks up changes on the next pull cycle.

## Available MCP tools

| Tool | When to use |
|------|-------------|
| `get_stats()` | First call — confirms the DB is live and mirror is fresh |
| `get_index()` | Load the complete workspace map in one call before doing anything else |
| `search(query, limit=10)` | Keyword/full-text search; returns ranked results with keywords and summaries |
| `get_page(notion_id)` | Full content for one page: title, AI summary, all properties, media paths |
| `list_recent(limit=20)` | Pages sorted by most recently updated — useful for "what changed?" |

All tools fall back to the live Notion API if the local DB is unreachable, so Claude is never left with nothing.

## Key tables (for direct SQL via PostgreSQL MCP)

- `page_index` — compact keyword index rebuilt after every sync. Primary table for discovery.
- `workspace_mirror` — full mirror with raw Notion JSON, AI summary, media paths, timestamps.

```sql
-- Quick size check
SELECT COUNT(*) FROM workspace_mirror WHERE is_active = TRUE;

-- Keyword search
SELECT notion_id, title, keywords, summary FROM page_index
WHERE 'client' = ANY(keywords) ORDER BY indexed_at DESC LIMIT 10;

-- Full-text search (uses the built-in function)
SELECT * FROM search_workspace('project management', 10);

-- Get a specific page
SELECT title, ai_summary FROM workspace_mirror WHERE notion_id = 'your-id' AND is_active = TRUE;
```

## Efficiency rules

- Call `get_index()` once at the start of a session to build your mental map; don't repeat it.
- Use `search()` before `get_page()` unless you already have the notion_id.
- `get_page()` returns the full raw JSON — only call it for pages you actually need.
- Never scan `workspace_mirror` broadly; use `page_index` or `search_workspace()` for discovery.

## Useful file locations

- MCP server tools: `src/mcp/server.py`
- Sync logic: `src/sync/engine.py`
- Self-healing agent: `src/sync/healer.py`
- DB schema: `src/db/sql/schema.sql`
- Keyword indexer: `src/db/indexer.py`
- Backup manager: `src/db/backup.py`
- Tray app: `src/ui/tray.py`
- Settings dialog: `src/ui/settings.py`
- MCP config writer: `src/mcp/configurator.py`
