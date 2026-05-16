# AI Instructions for Claude / MCP Connection

This file is a quick technical guide for Claude to use the local Notion mirror efficiently and with minimal token waste.

## What this connection is
- The app mirrors Notion into a local PostgreSQL database.
- Claude Desktop connects to that mirror through MCP under the server name `local_notion_mirror`.
- The main live code is under `src/`.

## Connection facts
- Claude config file: `%APPDATA%\Claude\claude_desktop_config.json`
- MCP config writer: `src/mcp/configurator.py`
- MCP server name: `local_notion_mirror`
- Optional MCP-specific database env vars:
  - `PG_MCP_USER`
  - `PG_MCP_PASSWORD`
  - `PG_MCP_HOST`
  - `PG_MCP_PORT`
  - `PG_MCP_DBNAME`
- If the config is updated, Claude Desktop must be fully quit and reopened before the connection refreshes.
- Best health check: `%APPDATA%\Claude\logs\mcp-server-local_notion_mirror.log` should contain `Server started and connected successfully`.

## Best query order for speed
Use the small helper views/functions first. Only fetch heavy JSON when absolutely needed.

1. Quick overview
```sql
SELECT * FROM workspace_catalog_stats;
```

2. Narrow the search
```sql
SELECT * FROM search_workspace_catalog('your topic here', 8);
```

3. Read the plain-language page preview
```sql
SELECT * FROM get_workspace_page_context('your-notion-page-id');
```

4. Only as a last resort, fetch full raw page JSON
```sql
SELECT raw_json
FROM workspace_mirror
WHERE notion_id = 'your-notion-page-id';
```

## Lightweight helpers Claude should prefer
- `workspace_catalog`
  - One current row per active page
  - Includes a short preview, property names, media count, and rough token estimate
- `workspace_catalog_stats`
  - Small summary of mirror size and freshness
- `search_workspace_catalog(search_text, result_limit)`
  - Cheap discovery before opening any full record
- `get_workspace_page_context(notion_id)`
  - Plain-language context preview for one page

## Bulk edit helpers
For large text cleanup jobs, prefer the local bulk replace functions instead of trying to rewrite many pages one by one.

Preview first:
```sql
SELECT * FROM preview_workspace_bulk_replace('old text', 'title,ai_summary', FALSE, 12);
```

Apply when ready:
```sql
SELECT * FROM apply_workspace_bulk_replace('old text', 'new text', 'title,ai_summary', FALSE);
```

## Full Notion write tools (MCP)
The Business Brain MCP server now includes direct Notion write helpers for full page content work, not just title/summary metadata.

- `notion_api_request(path, method, body, query)`
  - Raw Notion REST access through MCP (v1 paths like `/pages/{id}` or `/blocks/{id}/children`).
- `notion_list_block_children(block_id, page_size, max_pages)`
  - Reads page/block content with pagination.
- `notion_append_blocks(block_id, children)`
  - Appends new block content to a page or block.
- `notion_append_section(page_id, heading, paragraphs, bulleted_items, numbered_items, todo_items, checked_items, callout, quote, code, code_language, divider_before, divider_after)`
  - Preferred high-level helper for adding a new section without hand-authoring raw block JSON.
- `notion_replace_page_content(page_id, children, max_delete)`
  - Replaces top-level page content blocks.
- `notion_update_block(block_id, block_payload)`
  - Updates a specific block.
- `notion_delete_block(block_id)`
  - Archives/deletes a specific block.
- `notion_update_page(page_id, properties, archived)`
  - Updates page properties or archive state.
- `notion_create_page(parent, properties, children)`
  - Creates pages in databases or under parent pages.

Use these tools for section appends, block edits, and full page rewrites instead of bypassing MCP.

## Preferred write order for Claude
When modifying a Notion page through MCP:

1. If you are adding a normal section, use `notion_append_section` first.
2. If you need precise block JSON control, use `notion_append_blocks` or `notion_update_block`.
3. If you need a full rewrite, use `notion_replace_page_content`.
4. Only use `notion_api_request` when the higher-level tools do not cover the Notion endpoint or block shape you need.

## Reliability notes for Claude
- The MCP Notion request path now uses the same configured Notion secret source as the main app.
- `notion_list_block_children` supports broader pagination control.
- `notion_replace_page_content` supports unlimited delete-before-replace when `max_delete` is `0`.
- Prefer the high-level helpers because they automatically produce valid Notion block payloads and chunk long text safely.

## Efficiency rules
- Search first; do not scan the full `workspace_mirror` table unless the user truly needs raw detail.
- Prefer `workspace_catalog_stats`, `search_workspace_catalog`, and `get_workspace_page_context` over `raw_json`.
- Pull one page at a time for deeper inspection.
- Use bulk SQL helpers for large repetitive changes.
- Keep answers grounded in the lightweight catalog/context layer whenever possible.

## Data model notes
- The mirror is temporal.
- Records are not hard-deleted during normal history tracking.
- Older versions are expired with `valid_to`, and new active versions are inserted with a fresh `valid_from`.
- Point-in-time recovery depends on preserving that history.

## Useful repo locations
- Main application code: `src/`
- MCP connection setup: `src/mcp/configurator.py`
- SQL schema and helper functions: `src/db/sql/schema.sql`
- Connection status UI: `src/ui/tray.py` and `src/ui/wizard.py`
- First-time setup: `setup_and_run.bat`
- Normal launch: `run_app.bat`

## Verified local command
From `NotionLocalSync/`, this test command has been verified:
```powershell
.\venv\Scripts\python.exe -m unittest tests/test_sync_improvements.py
```

## Short operating summary for Claude
When answering questions about the mirrored Notion workspace:
1. Start with `workspace_catalog_stats`.
2. Use `search_workspace_catalog` to find likely matches.
3. Use `get_workspace_page_context` for the chosen page.
4. Only read `workspace_mirror.raw_json` if the lighter layers do not answer the question.

This keeps the MCP session fast, cheap, and reliable.