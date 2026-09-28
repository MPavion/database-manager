# Notion Local Sync — AI-Powered Personal Knowledge Assistant

Turn your Notion workspace into a fast, searchable knowledge base that Claude can reason over — without hitting Notion's API on every question.

The app runs quietly in your Windows system tray, keeps a local PostgreSQL mirror of your Notion workspace, and connects it to Claude Desktop via MCP. Claude can search, summarise, and cross-reference everything in your workspace in seconds.

---

## What you can build with this

- **A queryable course library** — store notes, PDFs, and summaries of every course you've taken; ask Claude to explain, compare, or connect ideas across them
- **A personal reference database** — books, articles, research, documentation — all searchable by Claude in one query
- **A project knowledge base** — tasks, decisions, meeting notes, SOPs — ask Claude "what did we decide about X?" and get an answer immediately
- **A client or business workspace** — mirror your Notion CRM, project tracker, or wiki; Claude answers questions about it instantly

> See **[WEAVE.md](WEAVE.md)** for a beginner's guide to building your own knowledge base from courses and resources on your PC.

---

## How it works

```
Your Notion workspace
        ↓  (sync every N minutes)
  Local PostgreSQL DB       ←→  Claude Desktop (MCP)
        ↓
  Fast keyword + full-text search index
```

1. The tray app pulls your Notion pages into a local database every few minutes
2. Claude Desktop connects to the local DB via MCP — no Notion API calls needed for most queries
3. Ask Claude anything about your workspace; it searches the local index in milliseconds

---

## Prerequisites

Before you start, make sure you have:

| Requirement | Notes |
|-------------|-------|
| Windows 10 or 11 | The tray app is Windows-only |
| Python 3.10 or later | [python.org](https://www.python.org/downloads/) |
| PostgreSQL 14 or later | [postgresql.org](https://www.postgresql.org/download/windows/) — note your username and password during install |
| A free Notion account | [notion.so](https://www.notion.so) |
| Claude Desktop | [claude.ai/download](https://claude.ai/download) |

---

## Quick Start

### 1 — Get the code

```powershell
git clone https://github.com/MPavion/Database-Manager.git
cd "Database Manager"
```

Or download the ZIP from GitHub and extract it.

### 2 — Run the setup script

```powershell
cd NotionLocalSync
setup_and_run.bat
```

This creates a virtual environment, installs all dependencies, and launches the app. The tray icon appears in the system tray (bottom-right of your taskbar).

### 3 — Connect your Notion account

**Create a Notion integration:**

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations) and click **New integration**
2. Give it a name (e.g. "Local Sync"), select your workspace, click **Submit**
3. Copy the **Internal Integration Token** (starts with `secret_`)

**Share your databases with the integration:**

For each Notion database you want to sync:
1. Open the database in Notion
2. Click the `…` menu → **Connections** → search for your integration name → **Confirm**

### 4 — Configure the app

Right-click the tray icon → **Settings**, then fill in:

- **Notion Integration Token** — paste your `secret_…` token
- **Database IDs** — enter `ALL` to sync every shared database, or paste specific IDs
- **PostgreSQL** — enter the host, port, username, and password from your PostgreSQL install
- **Sync interval** — how often to pull from Notion (default: 5 minutes)

Click **Test** buttons to verify each connection, then **OK** to save.

### 5 — Connect Claude Desktop

In Settings → **Claude Desktop MCP**, click **Auto-configure Claude Desktop**.

Then fully quit and reopen Claude Desktop. You should see `Notion Local DB` in the MCP tools list.

---

## What you'll see in Claude Desktop

Once connected, you can ask Claude things like:

- *"What courses do I have notes on?"*
- *"Summarise my notes on React hooks"*
- *"What were the action items from last week's project review?"*
- *"Find everything related to machine learning in my workspace"*
- *"What's the status of the Alpha project?"*

Claude uses a fast local index — most queries take under 100 ms and cost no Notion API credits.

---

## Features

- **Encrypted secrets** — API keys and passwords stored with Fernet encryption; never plain-text on disk
- **Nightly backup** — automatic `pg_dump` to any folder (including Google Drive) with configurable retention
- **Self-healing** — if a sync error looks like a code bug, Claude Opus 4.7 diagnoses and patches the source automatically
- **Desktop launcher** — double-click `Database Manager Launcher.exe` to start without opening a terminal
- **Windows auto-start** — optional registry entry to launch on login
- **Media proxy** — attachments and images downloaded locally and served on `localhost:8080`

---

## Building your own knowledge base

See **[WEAVE.md](WEAVE.md)** — a step-by-step guide for beginners on how to:

- Set up a Notion knowledge base database from scratch
- Structure it for courses, books, references, or projects
- Import resources from your PC
- Use Claude to query, summarise, and connect ideas across everything you've collected

---

## Detailed documentation

- [NotionLocalSync/README.md](NotionLocalSync/README.md) — full technical reference
- [NotionLocalSync/CLAUDE.md](NotionLocalSync/CLAUDE.md) — Claude MCP integration details
- [NotionLocalSync/AI instructions.md](NotionLocalSync/AI%20instructions.md) — quick reference for Claude

---

## Contributing

Issues and pull requests are welcome. See [NotionLocalSync/README.md](NotionLocalSync/README.md) for the project layout and architecture notes.

---

## Security note

Never commit your `.env` file or `secret.key` to source control — both are listed in `.gitignore`. If you fork this repository, make sure your own secrets are not included.
