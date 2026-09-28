# Notion Local Sync

A lightweight Windows tray application that mirrors your Notion workspace into a local PostgreSQL database and exposes it to Claude Desktop via MCP — significantly faster, cheaper, and offline-capable compared to querying Notion directly.

> **New to this project?** Start with the beginner-friendly [WEAVE guide](../WEAVE.md) and the [root README](../README.md) first.

---

## What it does

- **Continuous sync** — pulls all Notion databases and pages into local PostgreSQL on a configurable timer
- **Claude-optimised index** — after every sync, rebuilds a `page_index` table with titles, keywords, page type, token estimates, and compact summaries so Claude can navigate your entire workspace in a single tool call
- **MCP server** — exposes 5 read tools to Claude Desktop; local DB is always tried first with automatic Notion API fallback
- **Nightly backup** — exports a compressed `pg_dump` to any folder (Google Drive compatible) with configurable retention
- **Self-healing** — persistent sync errors are diagnosed by Claude Opus 4.7, which patches the source code and verifies the fix
- **Media proxy** — downloads Notion attachments and images locally and serves them on `http://localhost:8080`
- **Encrypted secrets** — API keys and passwords stored with Fernet symmetric encryption; never plain-text on disk
- **Desktop launcher** — double-click `Database Manager Launcher.exe` to start without a terminal
- **Windows auto-start** — optional registry entry to launch on login

---

## System tray states

| Colour | Meaning |
|--------|---------|
| Green | Connected and idle — last sync succeeded |
| Amber | Sync in progress |
| Red | DB unreachable or sync error — tooltip and balloon show the reason |

Right-click the tray icon to force a sync, open Settings, or quit.

---

## Setup

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10 or 11 | |
| Python 3.10+ | [python.org](https://www.python.org/downloads/) — tick "Add Python to PATH" during install |
| PostgreSQL 14+ | [postgresql.org](https://www.postgresql.org/download/windows/) — note your password during install |
| Notion account | [notion.so](https://www.notion.so) — free plan is sufficient |
| Claude Desktop | [claude.ai/download](https://claude.ai/download) |

### 1 — Clone and run the setup script

```powershell
git clone https://github.com/MPavion/Database-Manager.git
cd "Database Manager\NotionLocalSync"
setup_and_run.bat
```

The script creates a virtual environment, installs all dependencies, and launches the app. The tray icon appears in the system tray (bottom-right of your taskbar).

### 2 — Create a Notion integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and click **New integration**
2. Give it a name (e.g. "Local Sync"), select your workspace, click **Submit**
3. Copy the **Internal Integration Token** — it starts with `secret_`

### 3 — Share your databases with the integration

For each Notion database you want to sync:

1. Open the database in Notion
2. Click the `…` menu (top-right corner) → **Connections**
3. Search for your integration name → click **Confirm**

> If you want to sync your entire workspace, you can open your top-level workspace page and connect the integration there — it will automatically access all child databases.

### 4 — Configure in the Settings dialog

Right-click the tray icon → **Settings**, then fill in each section:

**Notion:**
- **Integration Token** — paste your `secret_…` token
- **Database IDs** — enter `ALL` to sync all shared databases, or paste specific IDs separated by commas

To find a database ID: open the database in Notion and copy the URL. The 32-character hex string after the last `/` (before the `?`) is the ID.

**PostgreSQL:**
- Host: `localhost` (unless PostgreSQL is on another machine)
- Port: `5432` (default)
- User: `postgres` (or whatever you set during install)
- Password: your PostgreSQL password
- Database: `notion_mirror` (auto-created if it doesn't exist)

Click **Test DB Connection** to verify before saving.

**Sync:**
- Sync interval — how often to pull from Notion (default: 5 minutes)
- Windows startup — tick to launch automatically on login

**Backup:**
- Choose a folder for nightly `pg_dump` backups (a Google Drive folder works perfectly)
- Set retention period and backup hour

**AI Self-Healing (optional):**
- Paste an Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- Enable the checkbox — when a persistent sync error occurs, Claude Opus 4.7 will diagnose and patch it automatically

**Claude Desktop MCP:**
- Leave the server name as `Notion Local DB` (or customise it)
- Click **Auto-configure Claude Desktop** — this writes the MCP entry into `%APPDATA%\Claude\claude_desktop_config.json`

### 5 — Restart Claude Desktop

Fully quit Claude Desktop (including from the tray) and reopen it. The `Notion Local DB` MCP server should appear in the tools list.

---

## MCP tools

All tools try the local DB first and fall back to the Notion API if the DB is unavailable.

| Tool | When to use |
|------|-------------|
| `get_stats()` | First call — confirms the DB is live and mirror is fresh |
| `get_index()` | Full workspace map in one call — title, keywords, summary, type, token estimate for every page |
| `search(query, limit=10)` | Ranked full-text + keyword search; returns matched pages with summaries |
| `get_page(notion_id)` | Complete page content — title, AI summary, all properties, media paths, raw JSON |
| `list_recent(limit=20)` | Pages sorted by most recently updated |

### Recommended query flow

```
1. get_stats()      → confirm mirror is fresh
2. get_index()      → load the full workspace map (once per session)
3. search("topic")  → find specific pages if needed
4. get_page(id)     → fetch full content for a specific page
```

Load `get_index()` once at the start of a conversation — it gives Claude a full map of your workspace without reading every page in full.

---

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
      backup.py          # nightly pg_dump backup manager
      sql/schema.sql     # table definitions, GIN indexes, search function
    mcp/
      server.py          # FastMCP server (5 MCP tools)
      configurator.py    # writes claude_desktop_config.json
    sync/
      engine.py          # Notion → PostgreSQL sync
      healer.py          # self-healing agent (Claude Opus 4.7)
    ui/
      tray.py            # system tray application
      settings.py        # settings dialog
    main.py              # entry point
```

---

## Database tables

### `workspace_mirror`
One row per active Notion page or database. Stores the raw Notion JSON, a plain-text AI summary, local media file paths, content hash, and timestamps. Use this for full page content.

### `page_index`
Rebuilt after every successful sync by `indexer.py`. Contains titles, keywords (auto-extracted from titles, status/select properties, and page body), compact summaries, page type classification, and token estimates. This is the primary table Claude reads — small, fast, and avoids loading full JSON.

### `search_workspace(query, limit)` function
PostgreSQL function using a CTE UNION approach so each search branch (full-text, keyword array, title LIKE) uses its own GIN index independently. Typical query time: 20–50 ms.

---

## Configuration reference

All variables can be set in the **Settings dialog** or by editing `.env` directly.
Copy `.env.example` to `.env` as a starting point — it has comments explaining every variable.

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTION_TOKEN_ENCRYPTED` | — | Fernet-encrypted Notion integration token |
| `NOTION_DB_ID` | `ALL` | Comma-separated database IDs, or `ALL` |
| `PG_HOST` | `localhost` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_USER` | `postgres` | PostgreSQL user |
| `PG_PASSWORD_ENCRYPTED` | — | Fernet-encrypted PostgreSQL password |
| `PG_DBNAME` | `notion_mirror` | Database name (auto-created if missing) |
| `SYNC_INTERVAL_MINUTES` | `5` | Pull interval in minutes |
| `MEDIA_PROXY_PORT` | `8080` | Port for the local media proxy |
| `CLAUDE_MCP_NAME` | `Notion Local DB` | MCP server name in Claude Desktop |
| `WINDOWS_STARTUP_ENABLED` | `1` | `1` to add registry auto-start entry |
| `BACKUP_DIR` | — | Folder for nightly `pg_dump` backups |
| `BACKUP_RETENTION_DAYS` | `7` | Days of backup history to keep |
| `BACKUP_HOUR` | `2` | Hour (0–23 local) when backup runs |
| `PG_DUMP_PATH` | — | Full path to `pg_dump.exe` if not on PATH |
| `ANTHROPIC_API_KEY_ENCRYPTED` | — | Anthropic API key for self-healing agent |
| `ANTHROPIC_HEALER_ENABLED` | `1` | `0` to disable the self-healing agent |

---

## Notes

- **Rate limiting on first sync** — Notion limits how quickly block content can be fetched. Rate limit warnings in the log are normal during the first sync of a large workspace. Page metadata syncs fine; block body text fills in over subsequent cycles.
- **Database auto-creation** — the app creates the `notion_mirror` PostgreSQL database automatically if it doesn't exist.
- **Secrets are machine-local** — `secret.key` and `.env` are generated on first run. Do not commit either file to source control (both are in `.gitignore`). If you set up the app on a new machine, configure it fresh through the Settings dialog.
- **Self-healing log** — `logs/healer.jsonl` records every error pattern the agent has diagnosed. Delete this file to re-analyse previously seen errors.
- **MCP config** — after clicking "Auto-configure Claude Desktop", fully quit Claude Desktop (not just close the window — use File → Quit or the tray icon) and reopen it.
