# Notion Local Sync — AI-Powered Personal Knowledge Assistant

Turn your Notion workspace into a fast, searchable knowledge base that Claude can reason over — without hitting Notion's API on every question.

The app runs quietly in your Windows system tray, keeps a local mirror of your Notion workspace, and connects it to Claude Desktop via MCP. Claude can search, summarise, and cross-reference everything in your workspace in seconds.

---

## Why this exists

Querying Notion through Claude is slow and expensive. Every question requires multiple live API calls. This app solves that:

| | Direct Notion API | Notion-Claude Optimizer |
|---|---|---|
| Search latency | 500 ms – 2 s | ~30 ms |
| Token cost per query | 2,000 – 8,000 tokens | ~200 tokens |
| Works offline | No | Yes (with API fallback) |
| Setup required | None | ~5 minutes |

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
  Local Database (SQLite or PostgreSQL)   ←→   Claude Desktop (MCP)
        ↓
  Fast keyword + full-text search index
```

1. The tray app pulls your Notion pages into a local database every few minutes
2. Claude Desktop connects to the local database via MCP — no Notion API calls needed for most queries
3. Ask Claude anything about your workspace; it searches the local index in milliseconds

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Windows 10 or 11 | The tray app is Windows-only |
| Python 3.11 or later | [python.org](https://www.python.org/downloads/) — tick "Add Python to PATH" during install |
| A free Notion account | [notion.so](https://www.notion.so) |
| Claude Desktop | [claude.ai/download](https://claude.ai/download) |

**That's it.** SQLite is built into Python — no database installation required.

> PostgreSQL is also supported for advanced users who already have it running. The setup wizard will ask which you prefer.

---

## Quick Start (5 minutes)

### 1 — Get the code

```powershell
git clone https://github.com/MPavion/Notion-Claude-Optimizer.git
cd "Notion-Claude Optimizer"
```

Or download the ZIP from GitHub and extract it.

### 2 — Run the setup script

```powershell
setup_and_run.bat
```

This creates a Python virtual environment, installs all dependencies, and launches an **interactive setup wizard** that walks you through every step.

### 3 — Follow the setup wizard

The wizard takes about 5 minutes and guides you through:

1. **Notion integration token** — create a free integration at [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. **Which databases to sync** — all of them, or specific ones by ID
3. **Database storage** — SQLite (recommended, zero installation) or PostgreSQL
4. **AI self-healing** — optional Anthropic API key for automatic error diagnosis
5. **Nightly backup** — optional backup folder (Google Drive works perfectly)
6. **Claude Desktop** — auto-configures the MCP connection

### 4 — Restart Claude Desktop

After the wizard completes, fully quit Claude Desktop (right-click tray icon → Quit) and reopen it. The MCP tools will be available immediately.

### 5 — Ask Claude about your workspace

```
"What's in my Notion workspace?"
"Find everything I've noted about machine learning"
"What were the action items from last week's meeting?"
"Summarise my notes on React hooks"
```

---

## Connecting Notion databases to the integration

Before pages sync, each Notion database must be connected to your integration:

1. Open the database in Notion
2. Click `…` (top-right) → **Connections** → search for your integration → **Confirm**

To give access to your entire workspace at once, open your top-level workspace page, click **Share**, and connect the integration there.

---

## Features

- **Zero-install database** — SQLite by default; no PostgreSQL or any other server needed
- **Encrypted secrets** — API keys and passwords stored with Fernet encryption; never plain-text on disk
- **Nightly backup** — automatic backup to any folder (including Google Drive) with configurable retention
- **Self-healing** — if a sync error looks like a code bug, Claude Opus 4.7 diagnoses and patches the source automatically
- **Desktop launcher** — double-click `Notion-Claude Optimizer Launcher.exe` to start without opening a terminal
- **Windows auto-start** — optional registry entry to launch on login
- **Media proxy** — attachments and images downloaded locally and served on `localhost:8080`
- **Automatic Notion API fallback** — if the local DB is unreachable, Claude falls back to live Notion queries transparently

---

## What Claude can do with your workspace

Once connected, Claude has access to five tools:

| Tool | What it does |
|------|-------------|
| `get_stats()` | Checks how fresh the mirror is and confirms the DB is live |
| `get_index()` | Loads a complete map of your workspace — every page title, keywords, and summary in one call |
| `search(query)` | Ranked full-text + keyword search across all pages |
| `get_page(id)` | Full content of a specific page including AI summary, all properties, and media |
| `list_recent()` | Pages sorted by most recently updated |

All tools fall back to the live Notion API automatically if the local DB is unavailable.

---

## Building your own knowledge base

See **[WEAVE.md](WEAVE.md)** — a step-by-step guide for beginners on how to:

- Set up a Notion knowledge base from scratch
- Structure it for courses, books, references, or projects
- Import resources from your PC
- Use Claude to query, summarise, and connect ideas across everything you've collected

---

## Detailed documentation

- [Notion-Claude Optimizer/README.md](Notion-Claude Optimizer/README.md) — full technical reference, configuration options, project layout
- [Notion-Claude Optimizer/CLAUDE.md](Notion-Claude Optimizer/CLAUDE.md) — MCP integration details and query patterns

---

## Contributing

Issues and pull requests are welcome. See [Notion-Claude Optimizer/README.md](Notion-Claude Optimizer/README.md) for the project layout and architecture notes.

---

## Licence

[MIT](LICENSE) — free to use, modify, and distribute.

---

## Security note

Never commit your `.env` file or `secret.key` to source control — both are listed in `.gitignore`. If you fork this repository, make sure your own secrets are not included.
