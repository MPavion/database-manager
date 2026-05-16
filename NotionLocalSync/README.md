# Notion Local Sync & MCP Middleware

This project is a Windows-native Python application designed to act as a high-performance middleware between Notion, a local PostgreSQL database, and the Claude Desktop application via the Model Context Protocol (MCP).

## Features
- **PySide6 UI**: Runs quietly in the system tray with a configuration dashboard.
- **Setup Wizard**: Guides you through configuring Notion API, local PostgreSQL, and Claude MCP.
- **Delta Syncing**: Fast, efficient synchronization of your Notion workspace to local Postgres.
- **Temporal Versioning**: Point-in-time recovery using `valid_from` and `valid_to` database fields.
- **Local Media Proxy**: Downloads PDFs and images locally and serves them via a lightweight local HTTP server for Claude.
- **Automatic MCP Config**: Injects Postgres server details into Claude's `claude_desktop_config.json`.
- **Scheduled Clean-Up**: Automatically prunes older local snapshots, refreshes PostgreSQL statistics, and can trigger the optional `notion_maintenance.py` remote housekeeping script on a schedule.
- **Security**: API keys are encrypted at rest using Fernet symmetric encryption. Note: Database rows remain unencrypted to ensure Claude's MCP SQL queries remain lightning-fast.

## Setup
1. Run `setup_and_run.bat` the first time to create the virtual environment and install dependencies.
2. After the initial setup, use `run_app.bat` for faster everyday launches without reinstalling packages each time.
3. The UI Wizard will launch on first run to configure your `.env` variables securely.

## Project Layout
- The live application code is under `src/`.
- `run_app.bat` is the quickest way to start the desktop tray app once setup is complete.
- The extra top-level wrappers in the parent workspace are legacy convenience entry points; `NotionLocalSync/src` is the main source of truth.

## Claude-Friendly Catalog Layer
To keep Claude fast and token-efficient, the app now auto-creates a small discovery layer in Postgres:

- `workspace_catalog` — one current row per active page with a short preview, media count, property names, and an approximate token estimate.
- `workspace_catalog_stats` — quick counts so Claude can understand the size and freshness of the mirror in one tiny query.
- `search_workspace_catalog(search_text, result_limit)` — a lightweight search helper for finding likely matches before loading any full records.

Recommended query flow for Claude Desktop MCP:
1. `SELECT * FROM workspace_catalog_stats;`
2. `SELECT * FROM search_workspace_catalog('your topic here', 8);`
3. `SELECT * FROM get_workspace_page_context('your-notion-id');`
4. Only if absolutely needed, fetch `raw_json` from `workspace_mirror` for that one record.

This keeps discovery cheap, returns a plain-language property preview first, and only pulls the heavier JSON when it is actually needed.

Suggested startup prompt for Claude:
> Use `workspace_catalog_stats` for a quick overview, then `search_workspace_catalog` to narrow the search, then `get_workspace_page_context` for the chosen record. Only query `workspace_mirror.raw_json` when the lighter catalog and context views do not answer the question.

## Automatic Clean-Up & Maintenance
- The app now runs a scheduled clean-up check in the background.
- Local PostgreSQL upkeep prunes older inactive snapshots and runs `VACUUM ANALYZE` so the mirror stays fast.
- You can trigger the same process manually from the tray menu or dashboard with `Run Clean-Up Now`.
- If `notion_maintenance.py` plus its support files (`maintenance_config.json` and `notion_controller.py`) are present in the parent workspace folder, the app will also launch the remote Notion housekeeping jobs on their schedule.

## Safe Local Editing Flow
1. Open the dashboard or tray menu and choose `Queue Local Edit`.
2. Pick a mirrored page, adjust its local title or summary, and save it.
3. The record is marked as `pending push` so the next `Push Pending Local Changes` or two-way sync will send it back to Notion.
4. While a page is pending push, pull syncs protect that local edit instead of overwriting it immediately.

## Bulk Local Search & Replace
For large cleanup jobs, use the new local bulk replace flow instead of asking the AI to rewrite each page one by one.

### In the Windows app
1. Open the dashboard.
2. Choose `Bulk search & replace`.
3. Enter the text to find, the replacement text, and whether to update titles, notes, or both.
4. Preview the impact locally, then apply it.
5. Review the result and run `Push local changes only` when you are ready to send it to Notion.

### Through Claude MCP with minimal tokens
Because the MCP connection points at PostgreSQL, Claude can now trigger the heavy work locally with one light SQL call:

```sql
SELECT * FROM preview_workspace_bulk_replace('old text', 'title,ai_summary', FALSE, 12);
SELECT * FROM apply_workspace_bulk_replace('old text', 'new text', 'title,ai_summary', FALSE);
```

This keeps the bulk work inside the local app/database, reduces token usage, and returns only a small summary back to the AI.

## Naming Conventions for Better AI Search
When Claude is connected to the default `Business Brain` MCP server, precise names make discovery more accurate and reduce wasted tokens.

### Recommended database naming
Use:
- `Area - Category`
- `Business - Projects`
- `Business - Contacts`
- `Operations - SOPs`
- `Personal - Journal`

### Recommended page naming
Use short, specific titles that can stand on their own:
- `SOP - Token Optimization`
- `Coaching - Ines - 2026-02-20`
- `Research - Mens Therapy Hub - Competitor Review`
- `App - Notification System`
- `Journal - 2023-01-06 - Course Ideas`

Avoid vague names like `Untitled`, date-only titles, or one-word pages such as `Competitor` when a more specific title is available.

## Recommended Claude-to-App Update Workflow
For rename cleanups and other large organizational edits, let Claude propose the wording and let the local app perform the actual updates.

### Why this flow works best
- **Lower token use**: Claude searches the lightweight catalog first and returns a small action plan.
- **Safer updates**: the app can preview changes before anything is pushed back to Notion.
- **Better targeting**: use `notion_id` when possible so duplicate page titles do not cause the wrong page to be renamed.

### Suggested workflow
1. Claude searches the mirror with `workspace_catalog_stats`, `search_workspace_catalog(...)`, and `get_workspace_page_context(...)`.
2. Claude returns a structured rename plan.
3. The app previews the proposed updates locally.
4. You approve the list.
5. The app applies the edits, marks them as `needs_push = TRUE`, and then pushes them to Notion.

### Recommended handoff payload
```json
{
  "action": "rename_pages",
  "workspace": "Business Brain",
  "mode": "preview",
  "items": [
    {
      "notion_id": "page-id-1",
      "current_title": "Competitor",
      "new_title": "Research - Mens Therapy Hub - Competitor Review",
      "reason": "Too vague; adds subject and purpose"
    },
    {
      "notion_id": "page-id-2",
      "current_title": "2023-01-06",
      "new_title": "Journal - 2023-01-06 - Course Ideas",
      "reason": "Date-only title needs topic context"
    }
  ]
}
```

Use `"mode": "preview"` first, then switch to `"mode": "apply"` after review.

> Rule of thumb: Claude should decide the wording; the app should execute the updates.

## Copy-Ready Prompt for Claude Cloud
Use the prompt below when you want Claude to review `Business Brain` and prepare a safe cleanup plan.

```text
Review my Business Brain workspace for page titles that are vague, duplicated, date-only, or labeled Untitled. Use these naming patterns when suggesting replacements:

- SOP - [Process]
- Coaching - [Person] - [YYYY-MM-DD]
- Research - [Topic] - [Angle]
- App - [Feature]
- Ops - [Process]
- Journal - [YYYY-MM-DD] - [Topic]

Please do not make blind edits. First, search efficiently and propose a rename plan. Return the result as structured JSON using this format:

{
  "action": "rename_pages",
  "workspace": "Business Brain",
  "mode": "preview",
  "items": [
    {
      "notion_id": "page-id",
      "current_title": "old title",
      "new_title": "clear new title",
      "reason": "short explanation"
    }
  ]
}

Prefer `notion_id` when available so duplicate titles do not cause mistakes. Group the most important cleanup candidates first, especially Untitled pages and date-only titles.
```

This keeps the cloud prompt short, clear, and efficient while still giving the app enough structure to act on the result safely.

## Example Cleanup Session
Here is a simple end-to-end example of how a naming cleanup can work.

### Step 1: Ask Claude Cloud to review the workspace
Paste the copy-ready prompt above into Claude Cloud and ask it to identify weak page titles in `Business Brain`.

### Step 2: Claude returns a preview plan
Example response:

```json
{
  "action": "rename_pages",
  "workspace": "Business Brain",
  "mode": "preview",
  "items": [
    {
      "notion_id": "32fb5266-4d9d-814d-b066-d42005cf38ac",
      "current_title": "Competitor",
      "new_title": "Research - Mens Therapy Hub - Competitor Review",
      "reason": "Adds the company name and clarifies the page purpose."
    },
    {
      "notion_id": "d8f4d0f1-1111-2222-3333-444444444444",
      "current_title": "2023-01-06",
      "new_title": "Journal - 2023-01-06 - Course Ideas",
      "reason": "Date-only titles are hard to find later without topic context."
    }
  ]
}
```

### Step 3: Preview locally in the app
The app should show a small review list such as:
- `Competitor` → `Research - Mens Therapy Hub - Competitor Review`
- `2023-01-06` → `Journal - 2023-01-06 - Course Ideas`

### Step 4: Approve and apply
Once the preview looks right, switch the payload from:

```json
"mode": "preview"
```

to:

```json
"mode": "apply"
```

The app then updates the local mirror, marks those records as `needs_push = TRUE`, and lets you run `Push local changes only` when ready.

### Step 5: Push to Notion
After the final review, push the approved local changes back to Notion.

> This approach is safer and more token-friendly than asking Claude Cloud to rewrite many pages directly, one by one.
