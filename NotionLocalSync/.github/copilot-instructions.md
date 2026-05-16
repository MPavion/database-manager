# Copilot Instructions: Notion Local Sync Middleware

## Architecture Overview
This application is a local-first middleware for Notion. It uses `PySide6` for the GUI, runs primarily in the Windows system tray, and orchestrates data syncing between the Notion API and a local PostgreSQL database.

## Technical Stack
- **GUI**: PySide6
- **Database**: PostgreSQL (accessed via `psycopg2`)
- **Notion**: `notion-client` API wrapper
- **Security**: `cryptography` for Fernet encryption of API tokens.
- **Media Proxy**: Python standard `http.server` running in a daemon thread.
- **MCP**: Edits `%APPDATA%\Claude\claude_desktop_config.json`

## State Management & Frequencies
- Sync runs on a user-defined interval or manually via the system tray.
- Schema is temporal. We NEVER `DELETE` records. We set `valid_to = NOW()` and insert a new row to preserve point-in-time recovery.
- Database text remains unencrypted to guarantee sub-100ms SQL query performance for the MCP server.

## Coding Rules
- **No placeholders**: All generated code must be structurally complete.
- **Error Handling**: Use the central logger in `src/core/config.py` for all errors.
- **Thread Safety**: UI elements must only be updated from the main Qt thread. Sync and proxy run in background threads.
