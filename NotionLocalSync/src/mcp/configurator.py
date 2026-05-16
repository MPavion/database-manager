import json
import os
import sys
from pathlib import Path

from src.core.config import get_claude_mcp_name, get_env, logger, save_env_var


def _build_mcp_command() -> tuple[str, list[str]]:
    app_root = Path(__file__).resolve().parents[2]
    main_script = app_root / "src" / "main.py"

    if getattr(sys, "frozen", False):
        return str(Path(sys.executable)), ["--mcp-server"]

    python_command = Path(sys.executable)
    if python_command.name.lower() == "pythonw.exe":
        console_python = python_command.with_name("python.exe")
        if console_python.exists():
            python_command = console_python

    return str(python_command), [str(main_script), "--mcp-server"]


def configure_claude_mcp():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        logger.error("APPDATA environment variable not found.")
        return False

    config_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
    config_data = {}

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read Claude config: {e}")

    if "mcpServers" not in config_data:
        config_data["mcpServers"] = {}

    server_name = get_claude_mcp_name()
    last_server_name = (get_env("CLAUDE_MCP_LAST_NAME", "") or "").strip()
    command, args = _build_mcp_command()

    for old_name in {"local_notion_mirror", last_server_name}:
        if old_name and old_name != server_name:
            config_data["mcpServers"].pop(old_name, None)

    config_data["mcpServers"][server_name] = {
        "command": command,
        "args": args,
        "env": {
            "PYTHONUTF8": "1",
        },
    }

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
        save_env_var("CLAUDE_MCP_LAST_NAME", server_name)
        logger.info(f"Claude MCP config updated successfully as '{server_name}'.")
        return True
    except Exception as e:
        logger.error(f"Failed to write Claude config: {e}")
        return False
