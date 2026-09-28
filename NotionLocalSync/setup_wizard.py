#!/usr/bin/env python3
"""
Notion Local Sync — First-time setup wizard.

Called automatically by setup_and_run.bat on first launch, or with --wizard to reconfigure.
Requires the virtual environment to be active (setup_and_run.bat handles this).
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

HERE     = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"


# ── Terminal helpers ───────────────────────────────────────────────────────────

def _hr(char="─", width=62):
    print(char * width)

def _step(n: int, title: str):
    print()
    _hr()
    print(f"  Step {n}  —  {title}")
    _hr()
    print()

def _ok(msg: str = ""):
    print(f"  ✓  {msg}")

def _warn(msg: str = ""):
    print(f"  !  {msg}")

def _err(msg: str = ""):
    print(f"  ✗  {msg}")

def _info(msg: str = ""):
    print(f"     {msg}")

def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    display = f" [{default}]" if default and not secret else ""
    full    = f"  → {prompt}{display}: "
    val     = getpass.getpass(full) if secret else input(full).strip()
    return val or default

def _ask_yn(prompt: str, default: str = "y") -> bool:
    choice = "[Y/n]" if default.lower() == "y" else "[y/N]"
    raw    = input(f"  → {prompt} {choice}: ").strip().lower() or default.lower()
    return raw in ("y", "yes")

def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out

def _write_env(updates: dict[str, str]):
    current = _read_env()
    current.update({k: v for k, v in updates.items() if v != ""})
    lines = [f"{k}={v}" for k, v in current.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ok(f"Saved to {ENV_PATH.name}")


# ── Welcome ────────────────────────────────────────────────────────────────────

def welcome():
    print()
    _hr("═")
    print()
    print("        NOTION LOCAL SYNC — Setup Wizard")
    print()
    print("  This wizard takes about 5 minutes and will configure")
    print("  the app so Claude Desktop can search your Notion")
    print("  workspace instantly.")
    print()
    print("  You will need:")
    print("    • A Notion account (any plan)")
    print("    • PostgreSQL installed and running")
    print("    • Claude Desktop installed")
    print()
    _hr("═")
    input("\n  Press Enter to begin, or Ctrl+C to cancel...\n")


# ── Step 1: Prerequisites ──────────────────────────────────────────────────────

def check_prerequisites():
    _step(1, "Checking prerequisites")

    # Python version
    v = sys.version_info
    if v >= (3, 11):
        _ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        _err(f"Python {v.major}.{v.minor} found — version 3.11 or later required.")
        _info("Download from: https://www.python.org/downloads/")
        _info("Make sure to check 'Add Python to PATH' during install.")
        sys.exit(1)

    # PostgreSQL
    pg_found = False
    try:
        import psycopg2
        conn = psycopg2.connect(
            host="localhost", port=5432, user="postgres",
            dbname="postgres", connect_timeout=3,
        )
        conn.close()
        pg_found = True
        _ok("PostgreSQL is running on localhost:5432")
    except Exception:
        pass

    if not pg_found:
        _warn("Could not reach PostgreSQL on localhost:5432.")
        print()
        _info("If PostgreSQL is not installed:")
        _info("  1. Download: https://www.postgresql.org/download/windows/")
        _info("  2. Run the installer — accept all defaults")
        _info("  3. Set a password for the 'postgres' user — write it down!")
        _info("  4. When install finishes, re-run this wizard.")
        print()
        if not _ask_yn("Continue anyway (you can fix this in Settings later)?", default="n"):
            sys.exit(0)

    # Claude Desktop
    appdata = Path(os.environ.get("APPDATA", ""))
    if (appdata / "Claude").exists():
        _ok("Claude Desktop detected")
    else:
        _warn("Claude Desktop not found at %APPDATA%\\Claude")
        _info("Download from: https://claude.ai/download")
        _info("You can continue and run 'Auto-configure' from Settings later.")


# ── Step 2: Notion token ────────────────────────────────────────────────────────

def setup_notion() -> str:
    _step(2, "Notion integration token")

    _info("Notion Local Sync needs permission to read your workspace.")
    _info("You grant this by creating an 'Integration' in Notion — it takes 2 minutes.")
    print()
    _info("What to do:")
    _info("  1. Go to: https://www.notion.so/my-integrations")
    _info("  2. Click '+ New integration'")
    _info("  3. Give it any name — e.g. 'Local Sync'")
    _info("  4. Leave all settings as defaults and click 'Submit'")
    _info("  5. Click 'Show' next to Internal Integration Token")
    _info("  6. Copy the token — it starts with  secret_")
    print()
    _info("After creating the token, you must connect the integration to")
    _info("each Notion database you want to sync:")
    _info("  Open the database in Notion → click ··· (top right)")
    _info("  → 'Connect to' → select your integration")
    print()

    if _ask_yn("Open notion.so/my-integrations in your browser now?"):
        webbrowser.open("https://www.notion.so/my-integrations")
        input("\n  (Press Enter when you have your token ready...)\n")

    token = ""
    while True:
        token = _ask("Paste your Integration Token", secret=True)
        if not token:
            if _ask_yn("No token entered. Skip for now and set it in Settings later?"):
                _warn("Token skipped — set it in Settings before the app can sync.")
                return ""
            continue

        if not token.startswith("secret_"):
            _warn("That doesn't look right — the token should start with  secret_")
            if not _ask_yn("Try again?"):
                break
            continue

        _info("Testing connection to Notion...")
        try:
            from notion_client import Client
            c    = Client(auth=token)
            me   = c.users.me()
            name = me.get("name") or me.get("id", "unknown")
            _ok(f"Connected as: {name}")
            return token
        except Exception as exc:
            _err(f"Connection failed: {exc}")
            if not _ask_yn("Try a different token?"):
                break

    return token


# ── Step 3: Database selection ─────────────────────────────────────────────────

def choose_databases(token: str) -> str:
    _step(3, "Which Notion databases to sync")

    _info("Choose what to mirror into the local database.")
    print()
    _info("  1  Sync ALL databases  (recommended)")
    _info("     The app mirrors every database your integration can see.")
    _info("     This is the easiest option and works well for most people.")
    print()
    _info("  2  Sync specific databases only")
    _info("     Useful if you have many databases and only want certain ones.")
    print()

    choice = ""
    while choice not in ("1", "2"):
        choice = input("  → Your choice [1 or 2]: ").strip()

    if choice == "1":
        _ok("Will sync ALL accessible databases")
        return "ALL"

    print()
    _info("To find a database ID:")
    _info("  Open the database in Notion → click 'Share' → 'Copy link'")
    _info("  The ID is the 32-character string at the end of the URL")
    _info("  Example URL:  notion.so/My-Notes-abc123def456...")
    _info("  Example ID:   abc123def456...  (with or without hyphens)")
    print()
    raw = _ask("Paste database IDs (comma-separated if multiple)")
    ids = ",".join(i.strip() for i in raw.split(",") if i.strip())
    if ids:
        _ok(f"Will sync: {ids}")
        return ids
    else:
        _warn("No IDs entered — defaulting to ALL")
        return "ALL"


# ── Step 4: PostgreSQL ─────────────────────────────────────────────────────────

def setup_postgres() -> tuple[str, str, str, str, str]:
    _step(4, "PostgreSQL connection")

    _info("The app stores your Notion mirror in a local PostgreSQL database.")
    _info("The database will be created automatically if it doesn't exist.")
    print()

    host     = _ask("Host",          default="localhost")
    port     = _ask("Port",          default="5432")
    user     = _ask("Username",      default="postgres")
    password = _ask("Password",      secret=True)
    dbname   = _ask("Database name", default="notion_mirror")

    print()
    _info("Testing connection...")
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=port, user=user, password=password,
            dbname="postgres", connect_timeout=5,
        )
        conn.close()
        _ok(f"Connected to PostgreSQL at {host}:{port} as {user}")
    except Exception as exc:
        _warn(f"Connection test failed: {exc}")
        _info("The app will retry when it starts. You can update credentials in Settings.")

    return host, port, user, password, dbname


# ── Step 5: Anthropic API key (optional) ─────────────────────────────────────

def setup_anthropic() -> str:
    _step(5, "AI self-healing — optional")

    _info("If sync errors ever occur, the app can automatically diagnose")
    _info("and fix them using Claude Opus 4.7. This requires an Anthropic")
    _info("API key but is completely optional — everything works without it.")
    print()
    _info("Get a key at: https://console.anthropic.com/")
    print()

    if not _ask_yn("Set up the Anthropic API key now?", default="n"):
        _info("You can add this later in Settings → AI Self-Healing.")
        return ""

    key = _ask("Anthropic API key (sk-ant-...)", secret=True)
    if key:
        _ok("Key noted — will be encrypted when the app starts.")
    else:
        _warn("No key entered. Skipping.")
    return key


# ── Step 6: Backup folder (optional) ─────────────────────────────────────────

def setup_backup() -> str:
    _step(6, "Nightly backup — optional")

    _info("The app can export a compressed backup of your local database")
    _info("every night. A Google Drive or OneDrive folder works perfectly.")
    print()

    if not _ask_yn("Set up automatic nightly backups?", default="n"):
        _info("You can configure this later in Settings → Backup.")
        return ""

    folder = _ask("Backup folder path  (e.g. C:\\Users\\You\\Google Drive\\Backups)")
    if not folder:
        return ""

    p = Path(folder)
    if not p.exists():
        if _ask_yn(f"Folder doesn't exist. Create it?"):
            try:
                p.mkdir(parents=True, exist_ok=True)
                _ok(f"Created: {folder}")
            except Exception as exc:
                _warn(f"Could not create: {exc}")
    else:
        _ok(f"Backup folder: {folder}")

    return folder


# ── Step 7: Save configuration ─────────────────────────────────────────────────

def write_config(token: str, db_ids: str, pg_host: str, pg_port: str,
                 pg_user: str, pg_password: str, pg_dbname: str,
                 anthropic_key: str, backup_dir: str):
    _step(7, "Saving configuration")

    updates = {
        "NOTION_DB_ID":              db_ids,
        "PG_HOST":                   pg_host,
        "PG_PORT":                   pg_port,
        "PG_USER":                   pg_user,
        "PG_DBNAME":                 pg_dbname,
        "SYNC_INTERVAL_MINUTES":     "5",
        "CLAUDE_MCP_NAME":           "Notion Local DB",
        "WINDOWS_STARTUP_ENABLED":   "1",
        "ANTHROPIC_HEALER_ENABLED":  "1" if anthropic_key else "0",
    }
    if token:
        updates["NOTION_TOKEN"] = token
    if pg_password:
        updates["PG_PASSWORD"] = pg_password
    if anthropic_key:
        updates["ANTHROPIC_API_KEY"] = anthropic_key
    if backup_dir:
        updates["BACKUP_DIR"]            = backup_dir
        updates["BACKUP_RETENTION_DAYS"] = "7"
        updates["BACKUP_HOUR"]           = "2"

    _write_env(updates)
    print()
    _info("Credentials are stored in .env (gitignored — never uploaded to GitHub).")
    _info("The app encrypts passwords automatically on first start.")


# ── Step 8: Configure Claude Desktop ─────────────────────────────────────────

def configure_mcp():
    _step(8, "Connecting to Claude Desktop")

    appdata   = Path(os.environ.get("APPDATA", ""))
    claude_dir = appdata / "Claude"

    if not claude_dir.exists():
        _warn("Claude Desktop config folder not found.")
        _info("Install Claude Desktop, then: right-click tray → Settings")
        _info("→ Claude Desktop MCP → Auto-configure.")
        return

    try:
        sys.path.insert(0, str(HERE))
        from src.mcp.configurator import configure_claude_mcp
        ok = configure_claude_mcp()
        if ok:
            _ok("Claude Desktop MCP entry written")
            print()
            _info("IMPORTANT: Fully quit and reopen Claude Desktop to pick up")
            _info("the new configuration. 'Close window' is not enough —")
            _info("right-click the Claude tray icon → Quit, then reopen it.")
        else:
            _warn("Auto-configure returned an error.")
            _info("Try: right-click tray → Settings → Claude Desktop MCP → Auto-configure.")
    except Exception as exc:
        _warn(f"Could not auto-configure: {exc}")
        _info("Try: right-click tray → Settings → Claude Desktop MCP → Auto-configure.")


# ── Summary ────────────────────────────────────────────────────────────────────

def summary():
    print()
    _hr("═")
    print()
    print("  All done! Here's what happens next:")
    print()
    print("  1. The app starts in the system tray (bottom-right near the clock)")
    print("  2. The icon shows amber while syncing — green when done")
    print("  3. First sync takes longer for large workspaces (this is normal)")
    print("  4. Fully quit and reopen Claude Desktop to activate the MCP tools")
    print("  5. Ask Claude: 'What's in my Notion workspace?'")
    print()
    print("  Right-click the tray icon at any time to:")
    print("    • Force a sync now")
    print("    • Open Settings to change any configuration")
    print("    • Quit the app")
    print()
    print("  Logs: NotionLocalSync\\logs\\app.log")
    print()
    _hr("═")
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    welcome()
    check_prerequisites()

    token       = setup_notion()
    db_ids      = choose_databases(token)
    pg_host, pg_port, pg_user, pg_password, pg_dbname = setup_postgres()
    ant_key     = setup_anthropic()
    backup_dir  = setup_backup()

    write_config(
        token, db_ids,
        pg_host, pg_port, pg_user, pg_password, pg_dbname,
        ant_key, backup_dir,
    )
    configure_mcp()
    summary()

    if _ask_yn("Launch the app now?"):
        python  = Path(sys.executable)
        pythonw = python.parent / "pythonw.exe"
        exe     = str(pythonw) if pythonw.exists() else str(python)
        subprocess.Popen([exe, "-m", "src.main"], cwd=str(HERE))
        print()
        _ok("App launched — look for the icon in the system tray.")
        print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled.")
