# Notion Local Sync

A lightweight Windows tray application that mirrors your Notion workspace into a local PostgreSQL database and exposes it to Claude Desktop via MCP — faster, cheaper, and offline-capable compared to querying Notion directly.

## What it does

- **2-way sync**: Pulls all Notion databases and pages into local PostgreSQL. Pushes local edits back.
- **Claude-optimised index**: After every sync, rebuilds a `page_index` table with keywords, page type, token estimates, and compact summaries so Claude can navigate your workspace in a single tool call.
- **MCP server**: Exposes 6 tools to Claude Desktop. Local DB is always tried first; falls back to the Notion API if the DB is unreachable.
- **Media proxy**: Downloads attached files and images locally and serves them on `http://localhost:8080` so Claude can reference them without hitting Notion's expiring signed URLs.
- **Encrypted secrets**: API keys and passwords are stored with Fernet symmetric encryption.
- **Windows auto-start**: Optional registry entry to launch on login.

## System tray states

| Colour | Meaning |
|--------|---------|
| 🟢 Green | Connected and idle — last sync succeeded |
| 🟡 Amber | Sync in progress |
| 🔴 Red | DB unreachable or sync error — tooltip and balloon notification show the reason |

Right-click the tray icon to force a sync, open Settings, or quit.

## Setup

1. **Install PostgreSQL** (any recent version). Note your host, port, username, and password.
2. **Clone or download** this repository.
3. **Create the virtual environment and install dependencies:**
   ```powershell
   cd NotionLocalSync
   python -m venv venv
   .\venv\Scripts\pip install -r requirements.txt
   ```
4. **Launch the app:**
   ```powershell
   .\venv\Scripts\pythonw.exe -m src.main
   ```
   On first run the tray icon appears. Right-click → **Settings** to enter your credentials.
5. **Configure** in the Settings dialog:
   - Notion integration token (`secret_…` from [notion.so/my-integrations](https://www.notion.so/my-integrations))
   - Database IDs to sync (comma-separated, or `ALL` to sync everything)
   - PostgreSQL connection details
   - Sync interval (default: 5 minutes)
   - Claude MCP server name
6. Click **Auto-configure Claude Desktop** — the app writes the MCP entry into `%APPDATA%\Claude\claude_desktop_config.json` and removes any stale entries from previous versions.
7. **Restart Claude Desktop** fully (quit and reopen) to pick up the MCP config.

## MCP tools for Claude Desktop

Once connected, Claude Desktop has access to 6 tools. All try the local DB first and fall back to the Notion API if the DB is unavailable.

| Tool | Description |
|------|-------------|
| `get_index()` | Full workspace map in one call — title, keywords, summary, page type, token estimate for every page |
| `search(query, limit)` | Ranked full-text + keyword search |
| `get_page(notion_id)` | Complete page content — title, AI summary, all properties, media paths, raw JSON |
| `list_recent(limit)` | Most recently updated pages |
| `push_update(notion_id, title, summary)` | Write title/summary back to Notion |
| `get_stats()` | Page count, pending pushes, media pages, last sync time |

### Recommended Claude query flow

```
1. get_stats()        → check how fresh the local mirror is
2. get_index()        → load the complete workspace map (keywords, summaries)
3. search("topic")    → narrow to specific pages if needed
4. get_page(id)       → fetch full content for one page
5. push_update(id, …) → write back any changes
```

`get_index()` returns a compact entry per page (not the full JSON) — use it as a table of contents before deciding which pages to fetch in full. This is significantly faster and cheaper than querying Notion directly for every lookup.

## Project layout

```
NotionLocalSync/
  src/
    core/
      config.py          # env/secret loading, Windows startup, tray icon path
      security.py        # Fernet encryption for API keys
      proxy.py           # local media HTTP proxy (port 8080)
      http.py            # retry HTTP session builder
    db/
      database.py        # PostgreSQL connection and all DB operations
      indexer.py         # builds page_index after each sync
      change_tracking.py # content hashing, timestamp comparison, JSON normalisation
      schema_loader.py   # loads schema.sql at import time
      sql/schema.sql     # table definitions, indexes, search function
    mcp/
      server.py          # FastMCP server (6 tools)
      configurator.py    # writes claude_desktop_config.json
    sync/
      engine.py          # Notion ↔ PostgreSQL 2-way sync
    ui/
      tray.py            # system tray application
      settings.py        # settings dialog
    main.py              # entry point (tray mode or --mcp-server mode)
```

## Database tables

### `workspace_mirror`
One active row per Notion page or database. Stores the raw Notion JSON, a plain-text AI summary extracted from the content, media file paths, content hash, and push/pull timestamps.

### `page_index`
Rebuilt after every successful sync by `indexer.py`. Contains keywords (auto-extracted from title, status/select properties, and content), a compact summary, page type classification, and a token estimate. This is the primary table Claude reads — it's small, fast, and avoids loading the full JSON.

## Configuration (.env)

The app reads from `NotionLocalSync/.env` (created automatically on first save in Settings). Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTION_TOKEN_ENCRYPTED` | — | Fernet-encrypted Notion integration token |
| `NOTION_DB_ID` | `ALL` | Comma-separated database IDs, or `ALL` |
| `PG_HOST` | `localhost` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_USER` | `postgres` | PostgreSQL user |
| `PG_PASSWORD` | — | PostgreSQL password (encrypted at rest) |
| `PG_DBNAME` | `notion_mirror` | Database name (auto-created if missing) |
| `SYNC_INTERVAL_MINUTES` | `5` | How often to sync |
| `MEDIA_PROXY_PORT` | `8080` | Port for the local media proxy |
| `CLAUDE_MCP_NAME` | `Notion Local DB` | MCP server name in Claude Desktop |
| `WINDOWS_STARTUP_ENABLED` | `0` | `1` to add a registry auto-start entry |

## Notes

- Block-level content (page body text) is fetched but rate-limited by Notion. Warnings about rate limiting in the logs are normal on the first sync of a large workspace.
- The app creates the PostgreSQL database automatically if it doesn't exist.
- Secrets are stored encrypted using Fernet (`secret.key` in the app directory). The key is machine-local; do not commit `secret.key` or `.env` to source control (both are in `.gitignore`).
- MCP server name changes take effect after clicking **Auto-configure** and fully restarting Claude Desktop.
