"""
Auto-configures Claude Desktop's claude_desktop_config.json so it points to
this app's MCP server.  Called on every app startup so the config stays current
even when Python or the app moves.

On each run:
  1. Remove ALL entries whose command path or args point to any known version
     of this app (old paths, old server names, legacy "Business Brain" names).
  2. Write a fresh entry under the current CLAUDE_MCP_NAME.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from src.core.config import get_claude_mcp_name, get_env, logger, save_env_var

# Names this app has used in the past — all will be removed before re-adding
_LEGACY_SERVER_NAMES = {
    "Business Brain",
    "business_brain",
    "local_notion_mirror",
    "NotionLocalSync",
}

# Path fragments that identify our app's scripts/executables
_OUR_SCRIPT_FRAGMENTS = (
    "Notion-Claude Optimizer",
    "NotionLocalSync",
    "notion_local_sync",
    "notion_claude_optimizer",
    "business_brain_mcp",
    "server.py",
    "main.py",
)


def _build_mcp_command() -> tuple[str, list[str]]:
    app_root    = Path(__file__).resolve().parents[2]
    main_script = app_root / "src" / "main.py"

    if getattr(sys, "frozen", False):
        return str(Path(sys.executable)), ["--mcp-server"]

    python = Path(sys.executable)
    # Always use the console python.exe for MCP (needs stdout for JSON-RPC)
    if python.name.lower() == "pythonw.exe":
        console = python.with_name("python.exe")
        if console.exists():
            python = console

    return str(python), [str(main_script), "--mcp-server"]


def _looks_like_our_entry(name: str, entry: dict) -> bool:
    """Return True if this config entry belongs to a previous version of our app."""
    if name in _LEGACY_SERVER_NAMES:
        return True

    cmd  = str(entry.get("command") or "")
    args = " ".join(str(a) for a in (entry.get("args") or []))

    for fragment in _OUR_SCRIPT_FRAGMENTS:
        if fragment.lower() in cmd.lower() or fragment.lower() in args.lower():
            return True

    return False


def configure_claude_mcp() -> bool:
    """
    Write (or refresh) the MCP server entry in Claude Desktop's config.
    Returns True on success.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        logger.error("APPDATA not set — cannot configure Claude Desktop.")
        return False

    config_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
    config: dict = {}

    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception as exc:
            logger.warning(f"Could not read existing Claude config: {exc}")
            config = {}

    servers: dict = config.setdefault("mcpServers", {})
    server_name   = get_claude_mcp_name()

    # Remove all stale / legacy entries for this app
    stale = [n for n, e in servers.items() if _looks_like_our_entry(n, e)]
    for name in stale:
        servers.pop(name, None)
        logger.info(f"Removed old MCP entry: '{name}'")

    # Also remove the previously saved name if it differs
    last_name = (get_env("CLAUDE_MCP_LAST_NAME") or "").strip()
    if last_name and last_name != server_name:
        servers.pop(last_name, None)

    command, args = _build_mcp_command()
    servers[server_name] = {
        "command": command,
        "args":    args,
        "env":     {"PYTHONUTF8": "1"},
    }

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        save_env_var("CLAUDE_MCP_LAST_NAME", server_name)
        logger.info(
            f"Claude MCP config updated: '{server_name}' → {command} {' '.join(args)}"
        )
        return True
    except Exception as exc:
        logger.error(f"Failed to write Claude Desktop config: {exc}")
        return False
