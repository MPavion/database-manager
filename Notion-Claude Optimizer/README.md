# Notion-Claude Optimizer — Technical Reference

A Windows tray application that mirrors your Notion workspace into a local database (SQLite by default, PostgreSQL optional) and exposes it to Claude Desktop via MCP — up to 98% faster and 97% cheaper per query than hitting the Notion API directly.

> **New here?** Start with the [root README](../README.md) and the beginner-friendly [WEAVE guide](../WEAVE.md) first.

---

## What it does

- **Continuous sync** — pulls all Notion databases and pages into a local database on a configurable timer
- **Claude-optimised index** — after every sync, rebuilds a `page_index` table with titles, keywords, page type, token estimates, and compact summaries so Claude can navigate your entire workspace in a single tool call
- **MCP server** — exposes 5 read tools to Claude Desktop; local DB is always tried first with automatic Notion API fallback
- **Nightly backup** — copies the database to any folder (Google Drive compatible) with configurable retention
- **Self-healing** — persistent sync errors are diagnosed by Claude Opus 4.7, which can patch source code and verify the fix automatically
- **Media proxy** — downloads Notion attachments and images locally and serves them on `http://localhost:8080`
- **Encrypted secrets** — API keys and passwords stored with Fernet symmetric encryption; never plain-text on disk
- **Desktop launcher** — double-click `Notion-Claude Optimizer Launcher.exe` to start without a terminal
- **Windows auto-start** — optional registry entry to launch on login

---

## System tray states

| Colour | Meaning |
|--------|---------|
| Green | Connected and idle — last sync succeeded |
| Amber | Sync in progress |
| Red | DB unreachable or sync error — tooltip and balloon notification show the reason |

Right-click the tray icon to force a sync, open Settings, or quit.

---

## Setup

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10 or 11 | |
| Python 3.11+ | [python.org](https://www.python.org/downloads/) — tick "Add Python to PATH" during install |
| Notion account | [notion.so](https://www.notion.so) — free plan is fine |
| Claude Desktop | [claude.ai/download](https://claude.ai/download) |

PostgreSQL is **not required**. SQLite is built into Python and is the default database backend.

### First-time setup

```powershell
git clone https://github.com/MPavion/Notion-Claude-Optimizer.git
cd "Notion-Claude Optimizer"
setup_and_run.bat
```

`setup_and_run.bat` creates a virtual environment, installs dependencies, and launches the interactive setup wizard on first run (or when no `.env` file exists). Run with `--wizard` to reconfigure at any time:

```powershell
setup_and_run.bat --wizard
```

### Setup wizard steps

The wizard walks through everything in about 5 minutes:

1. **Prerequisites check** — Python version and Claude Desktop detection
2. **Notion integration token** — create at [notion.so/my-integrations](https://www.notion.so/my-integrations) and paste it; the wizard tests the connection
3. **Database selection** — sync all shared databases (`ALL`) or specific IDs
4. **Database backend** — SQLite (default, zero install) or PostgreSQL (if already running)
5. **AI self-healing** — optional Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
6. **Nightly backup** — optional backup folder path
7. **Save configuration** — writes `.env`
8. **Claude Desktop MCP** — auto-configures `claude_desktop_config.json`

### Connecting Notion databases

Before pages sync, each database must be connected to your integration:

1. Open the database in Notion
2. Click `…` (top-right) → **Connections** → search for your integration name → **Confirm**

To share your entire workspace at once: open the top-level workspace page → **Share** → connect the integration.

### After the wizard

Fully quit Claude Desktop (right-click tray icon → **Quit**, not just close the window) and reopen it. The MCP server will appear in Claude's tools list.

---

## MCP tools

All tools try the local DB first and fall back to the Notion API if the DB is unavailable.

| Tool | When to use |
|------|-------------|
| `get_stats()` | First call — confirms the DB is live and shows how fresh the mirror is |
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

Load `get_index()` once at the start of a session — it gives Claude a full map of your workspace without reading every page in full.

---

## Project layout

```
Notion-Claude Optimizer/
  setup_and_run.bat       # entry point: venv, deps, wizard, launch
  setup_wizard.py         # interactive first-time setup wizard
  src/
    core/
      config.py           # env/secret loading, Windows startup, paths
      security.py         # Fernet encryption for API keys and passwords
      proxy.py            # local media HTTP proxy (port 8080)
      http.py             # retry HTTP session builder
    db/
      database.py         # dual-backend DB manager (SQLite + PostgreSQL)
      indexer.py          # builds page_index after each sync
      change_tracking.py  # content hashing, timestamp comparison, normalisation
      schema_loader.py    # selects schema.sql or schema_sqlite.sql at runtime
      backup.py           # nightly backup (sqlite3.backup() or pg_dump)
      sql/
        schema.sql         # PostgreSQL: tables, GIN indexes, search function
        schema_sqlite.sql  # SQLite: equivalent tables with TEXT JSON columns
    mcp/
      server.py           # FastMCP server (5 MCP tools)
      configurator.py     # writes claude_desktop_config.json
    sync/
      engine.py           # Notion → local DB sync engine
      healer.py           # self-healing agent (Claude Opus 4.7)
    ui/
      tray.py             # system tray application (PySide6)
      settings.py         # settings dialog
    main.py               # entry point
```

---

## Database backends

### SQLite (default)

Zero installation required — `sqlite3` is part of Python's standard library. The database is a single file at `data/notion_mirror.db` by default (configurable via `SQLITE_PATH`).

- Text columns store JSON as strings; the app normalises them on read
- Keyword search uses LIKE queries across title, summary, content, and keywords
- Backup uses Python's `sqlite3.backup()` API — no external tools needed
- WAL mode enabled for safe concurrent access

### PostgreSQL (advanced)

Requires a running PostgreSQL 14+ server. Offers GIN-indexed full-text search and a `search_workspace()` function using a CTE UNION approach for independent index use per search branch. Typical query time: 20–50 ms.

To use PostgreSQL, run the setup wizard and choose option 2 at the database backend step, or set `DB_BACKEND=postgresql` in `.env` and fill in the `PG_*` variables.

---

## Database tables

### `workspace_mirror`
One row per active Notion page or database. Stores the raw Notion JSON, a plain-text AI summary, local media file paths, content hash, and timestamps.

### `page_index`
Rebuilt after every successful sync. Contains titles, keywords (auto-extracted from titles, status/select properties, and page body text), compact summaries, page type, and token estimates. This is the primary table Claude reads — small, fast, and avoids loading full JSON.

---

## Configuration reference

All settings can be configured through the **Settings dialog** (right-click tray icon) or by editing `.env` directly. Copy `.env.example` to `.env` as a starting point — it has comments explaining every variable.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `NOTION_TOKEN_ENCRYPTED` | — | Fernet-encrypted Notion integration token |
| `NOTION_DB_ID` | `ALL` | Comma-separated database IDs, or `ALL` |
| `SYNC_INTERVAL_MINUTES` | `5` | Pull interval in minutes |
| `CLAUDE_MCP_NAME` | `Notion Local DB` | MCP server name as shown in Claude Desktop |
| `WINDOWS_STARTUP_ENABLED` | `1` | `1` to add a Windows startup registry entry |

### Database backend

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_BACKEND` | `sqlite` | `sqlite` or `postgresql` |
| `SQLITE_PATH` | *(auto)* | Full path to the `.db` file; empty = `data/notion_mirror.db` |
| `PG_HOST` | `localhost` | PostgreSQL host |
| `PG_PORT` | `5432` | PostgreSQL port |
| `PG_USER` | `postgres` | PostgreSQL user |
| `PG_PASSWORD_ENCRYPTED` | — | Fernet-encrypted PostgreSQL password |
| `PG_DBNAME` | `notion_mirror` | PostgreSQL database name (auto-created if missing) |

### Backup

| Variable | Default | Description |
|----------|---------|-------------|
| `BACKUP_DIR` | — | Folder for nightly backups (e.g. a Google Drive folder) |
| `BACKUP_RETENTION_DAYS` | `7` | Days of backup history to keep |
| `BACKUP_HOUR` | `2` | Hour (0–23 local) when the backup runs |
| `PG_DUMP_PATH` | — | Full path to `pg_dump.exe` if not on PATH (PostgreSQL only) |

### AI self-healing

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY_ENCRYPTED` | — | Fernet-encrypted Anthropic API key |
| `ANTHROPIC_HEALER_ENABLED` | `1` | `0` to disable the self-healing agent |

### Media proxy

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_PROXY_PORT` | `8080` | Port for the local image/attachment proxy |

---

## Notes

- **Rate limiting on first sync** — Notion limits how quickly block content can be fetched. Rate limit warnings in the log during the first sync of a large workspace are normal — page metadata syncs immediately and block body text fills in over subsequent cycles.
- **First sync duration** — syncing a large workspace (thousands of pages) takes longer on the first run. The app is fully usable while the initial sync is in progress.
- **Secrets are machine-local** — `secret.key` and `.env` are generated on first run and are listed in `.gitignore`. Setting up the app on a new machine requires fresh configuration via the wizard or Settings dialog.
- **Self-healing log** — `logs/healer.jsonl` records every error pattern the agent has diagnosed. Delete this file to re-analyse previously seen errors.
- **MCP config restart** — after clicking "Auto-configure Claude Desktop", fully quit Claude Desktop (File → Quit or right-click tray → Quit; closing the window is not enough) and reopen it.
- **OpenAI / Gemini** — the MCP server runs locally via stdio and is only accessible to Claude Desktop. Cloud-based AI services cannot connect to a local MCP server directly; they would require the server to be exposed via a public HTTPS endpoint.

---

## Disclaimer

This software is provided **"as is"** under the [MIT Licence](../LICENSE), without warranty of any kind. By installing or running it you accept the following terms.

**Your Notion data is never modified.** This app creates a read-only local mirror of your Notion workspace. It does not create, edit, delete, or move any page or database in Notion. That said, you remain solely responsible for maintaining your own backups of your Notion workspace. The author accepts no liability for data loss, data corruption, sync gaps, or unauthorised access to your Notion account or its contents.

**The self-healing agent modifies source files.** When sync errors occur and an Anthropic API key is configured, the self-healing agent (powered by Claude Opus 4.7) may autonomously read and overwrite Python source files inside `src/sync/` and `src/db/`. The author accepts no liability for unintended behaviour, data loss, or system changes caused by AI-generated code modifications. Disable this feature by setting `ANTHROPIC_HEALER_ENABLED=0` in `.env` if you prefer manual control.

**Third-party services.** This app communicates with Notion's API and, if configured, Anthropic's API. You are responsible for your own compliance with their respective terms of service and acceptable-use policies. The author has no affiliation with Notion, Anthropic, or any other third-party service referenced in this project.

**No guarantee of sync accuracy or completeness.** The local mirror may be incomplete or out of date due to Notion API rate limits, network errors, sync failures, or bugs. Do not treat the local database as your primary or sole copy of any data.

**No warranty; no liability.** To the maximum extent permitted by applicable law, the author shall not be liable for any direct, indirect, incidental, special, consequential, or exemplary damages arising from the use of or inability to use this software, even if advised of the possibility of such damages.
