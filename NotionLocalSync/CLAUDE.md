# CLAUDE.md

Use this project’s local Notion mirror efficiently. Prefer the light catalog/context helpers first, and only pull heavier data when necessary.

## Project overview
- This is a Windows-native Python app that mirrors Notion into local PostgreSQL and exposes that data to Claude Desktop via MCP.
- Main code lives in `src/`.
- MCP server name: `local_notion_mirror`.

## Most efficient query flow
When answering questions about the mirrored workspace, use this order:

1. **Quick overview**
```sql
SELECT * FROM workspace_catalog_stats;
```

2. **Find likely matches**
```sql
SELECT * FROM search_workspace_catalog('your topic here', 8);
```

3. **Read one page in plain language**
```sql
SELECT * FROM get_workspace_page_context('your-notion-page-id');
```

4. **Only if needed, fetch full JSON**
```sql
SELECT raw_json
FROM workspace_mirror
WHERE notion_id = 'your-notion-page-id';
```

## Prefer these helpers
- `workspace_catalog`: current active-page catalog with preview text, property names, media count, and rough token estimate
- `workspace_catalog_stats`: tiny summary of mirror size and freshness
- `search_workspace_catalog(search_text, result_limit)`: cheap discovery helper
- `get_workspace_page_context(notion_id)`: lighter page preview before any `raw_json`

## Efficiency rules
- Search first; avoid broad scans of `workspace_mirror`.
- Prefer catalog/context helpers over `raw_json`.
- Pull one page at a time for deep inspection.
- Keep responses grounded in lightweight SQL results whenever possible.

## Bulk edit shortcuts
For larger local text cleanup jobs, prefer the built-in SQL helpers.

Preview:
```sql
SELECT * FROM preview_workspace_bulk_replace('old text', 'title,ai_summary', FALSE, 12);
```

Apply:
```sql
SELECT * FROM apply_workspace_bulk_replace('old text', 'new text', 'title,ai_summary', FALSE);
```

## Important data behavior
- The mirror is **temporal**.
- History is preserved using `valid_from` / `valid_to`.
- Normal updates expire old rows and insert new active rows instead of destroying history.

## MCP connection notes
- Claude config file: `%APPDATA%\Claude\claude_desktop_config.json`
- Config writer: `src/mcp/configurator.py`
- Optional MCP DB env vars:
  - `PG_MCP_USER`
  - `PG_MCP_PASSWORD`
  - `PG_MCP_HOST`
  - `PG_MCP_PORT`
  - `PG_MCP_DBNAME`
- After config changes, Claude Desktop must be fully quit and reopened.
- Good health signal: `%APPDATA%\Claude\logs\mcp-server-local_notion_mirror.log` contains `Server started and connected successfully`.

## Useful repo locations
- `src/mcp/configurator.py` — writes Claude MCP config
- `src/db/sql/schema.sql` — catalog/context views and functions
- `src/ui/tray.py` and `src/ui/wizard.py` — connection status and setup UX
- `setup_and_run.bat` — first-time setup
- `run_app.bat` — normal launch

## Verified local test command
From `NotionLocalSync/`:
```powershell
.\venv\Scripts\python.exe -m unittest tests/test_sync_improvements.py
```

## Operating summary
Default behavior should be:
1. `workspace_catalog_stats`
2. `search_workspace_catalog(...)`
3. `get_workspace_page_context(...)`
4. `workspace_mirror.raw_json` only when lighter layers are insufficient
