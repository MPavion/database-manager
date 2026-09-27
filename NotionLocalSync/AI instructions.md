# AI Instructions — Notion Local DB MCP

This file is a quick reference for Claude when connected to the local Notion mirror via MCP.

## What this connection is

The app mirrors your entire Notion workspace into a local PostgreSQL database and exposes it through 6 MCP tools. Local queries are ~10–100× faster than hitting the Notion API directly and use far fewer tokens because the index is pre-built.

## MCP server name
`Notion Local DB`

## The 6 tools

### `get_stats()`
Start here. Returns page count, pending pushes, media pages, last sync time, and whether the DB is connected. If `last_sync` is recent and `active_pages > 0`, the local mirror is ready to use.

### `get_index()`
Returns a compact entry for every page in the workspace:
- `notion_id` — stable unique identifier
- `title` — page title
- `keywords` — auto-extracted tags (status values, select options, title words, frequent content words)
- `summary` — ≤400 character summary
- `page_type` — `task`, `project`, `reference`, `note`, or `page`
- `token_estimate` — rough size of the full page in tokens
- `notion_url` — direct link to the page in Notion

Use `get_index()` as your table of contents. Load it once at the start of a session, then use keywords and summaries to decide which pages are worth fetching in full.

### `search(query, limit=10)`
Ranked full-text + keyword search. Returns title, summary preview, keywords, page type, and a match rank. Use this when you're looking for something specific rather than browsing the full index.

### `get_page(notion_id)`
Full page content: title, AI summary (extracted properties + page body text), all property values, local media paths, and the raw Notion JSON. Only call this for pages you've already identified via `get_index()` or `search()`.

### `list_recent(limit=20)`
Pages sorted by most recently updated. Useful for "what has changed?" or "what was I working on?" queries.

### `push_update(notion_id, title, summary)`
Write a title or summary change back to Notion. The local record is updated and flagged for push; the tool then immediately attempts to push it to Notion.

## Recommended session flow

```
get_stats()           → confirm mirror is live and fresh
get_index()           → load workspace map (do this once per session)
search("topic")       → if you need to find something specific
get_page(notion_id)   → only for pages you've chosen to read in full
push_update(...)      → when making changes
```

## Fallback behaviour

If the local PostgreSQL database is unavailable, all tools fall back to the live Notion API automatically. The `source` field in each response tells you which was used (`local_db` or `notion_api_fallback`). Fallback results are less rich — no keywords, no token estimates.

## Connection facts

- Claude config file: `%APPDATA%\Claude\claude_desktop_config.json`
- MCP config writer: `src/mcp/configurator.py`
- After config changes, Claude Desktop must be fully quit and reopened.
- Health check: `%APPDATA%\Claude\logs\mcp-server-Notion Local DB.log` should contain `Started server`.

## Data freshness

The mirror syncs on a timer (default: every 5 minutes). `get_stats()` shows `last_sync` time. Pages that have been edited locally but not yet pushed to Notion will show `needs_push: true` in `get_page()` results.
