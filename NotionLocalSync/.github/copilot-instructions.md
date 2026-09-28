# Copilot Instructions: Notion Local Sync Middleware

## Architecture Overview
This application is a local-first middleware for Notion. It uses `PySide6` for the GUI, runs primarily in the Windows system tray, and orchestrates data syncing between the Notion API and a local PostgreSQL database. Claude Desktop connects to the local DB via MCP for fast, offline-capable workspace queries.

## Technical Stack
- **GUI**: PySide6 (system tray + settings dialog)
- **Database**: PostgreSQL accessed via `psycopg2`; GIN indexes on `page_index` for fast full-text + keyword search
- **Notion**: `notion-client` SDK + raw HTTP fallback for API compatibility
- **Security**: `cryptography` (Fernet) for encrypting API tokens and passwords at rest
- **Media Proxy**: Python `http.server` on `localhost:8080` serving downloaded Notion attachments
- **MCP**: FastMCP server; writes `%APPDATA%\Claude\claude_desktop_config.json`
- **Backup**: `pg_dump` (custom format) written atomically to a configurable folder
- **Self-healing**: `anthropic` SDK — Claude Opus 4.7 diagnoses and patches persistent sync errors

## Key files
- `src/core/config.py` — env loading, secret encryption, Windows startup, tray icon path
- `src/sync/engine.py` — Notion → PostgreSQL pull sync
- `src/sync/healer.py` — self-healing agent (Anthropic API)
- `src/db/database.py` — all PostgreSQL operations
- `src/db/indexer.py` — builds `page_index` after each sync
- `src/db/backup.py` — nightly pg_dump manager
- `src/db/sql/schema.sql` — table definitions, GIN indexes, `search_workspace` CTE UNION function
- `src/mcp/server.py` — 5 MCP tools for Claude Desktop
- `src/ui/tray.py` — system tray (green/amber/red traffic-light icon)
- `src/ui/settings.py` — settings dialog

## State management
- Sync runs on a configurable timer or manually via the tray menu.
- Records are never hard-deleted — `is_active = FALSE` marks inactive pages.
- Database text is unencrypted to guarantee sub-100 ms query performance.
- Error fingerprints stored in `logs/healer.jsonl` prevent re-analysing the same error.

## Coding rules
- No placeholders — all generated code must be structurally complete.
- Use the central logger from `src/core/config.py` for all errors and info messages.
- UI elements must only be updated from the main Qt thread; use `QThread` + `Signal` for background work.
- Guard all Notion SDK responses with `isinstance(result, dict)` — the SDK can return non-dict values.
- Write files atomically: write to `.tmp` first, then `Path.replace()`.
- Secrets are stored encrypted; use `get_secret()` / `save_secret()` from `config.py`, never read `.env` directly.
