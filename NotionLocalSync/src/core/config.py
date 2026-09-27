import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv, set_key

try:
    import winreg
except ImportError:
    winreg = None

BASE_DIR   = Path(__file__).resolve().parent.parent.parent
ENV_PATH   = BASE_DIR / ".env"
KEY_PATH   = BASE_DIR / "secret.key"
MEDIA_DIR  = BASE_DIR / "data" / "media"
LOG_DIR    = BASE_DIR / "logs"
APP_LOG    = LOG_DIR / "app.log"

IMAGE_EXTENSIONS         = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico")
WINDOWS_RUN_KEY          = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_STARTUP_VALUE    = "NotionLocalSync"

_SecurityManager = None

def _get_security_manager():
    global _SecurityManager
    if _SecurityManager is None:
        from src.core.security import SecurityManager as _SM
        _SecurityManager = _SM
    return _SecurityManager()


DEFAULT_ENV = {
    # PostgreSQL
    "PG_HOST":               "localhost",
    "PG_PORT":               "5432",
    "PG_USER":               "postgres",
    "PG_PASSWORD":           "",
    "PG_PASSWORD_ENCRYPTED": "",
    "PG_DBNAME":             "notion_mirror",
    "PG_CONNECT_TIMEOUT":    "5",
    # Notion
    "NOTION_TOKEN":           "",
    "NOTION_TOKEN_ENCRYPTED": "",
    "NOTION_DB_ID":           "ALL",
    "NOTION_VERSION":         "2022-06-28",
    "NOTION_API_BASE":        "https://api.notion.com/v1",
    # Sync
    "SYNC_INTERVAL_MINUTES":  "5",
    # Media proxy
    "MEDIA_PROXY_PORT":       "8080",
    # App
    "APP_DISPLAY_NAME":       "Notion Local Sync",
    "APP_VERSION":            "3.0",
    "CLAUDE_MCP_NAME":        "Notion Local DB",
    "CLAUDE_MCP_LAST_NAME":   "",
    "WINDOWS_STARTUP_ENABLED": "1",
    "INSTANCE_LOCK_STALE_MINUTES": "10",
}

# Ensure runtime directories exist
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logger ────────────────────────────────────────────────────────────────────
logger = logging.getLogger("NotionLocalSync")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    _fh = RotatingFileHandler(APP_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)

    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    logger.addHandler(_ch)


# ── Config loading ────────────────────────────────────────────────────────────
def load_config():
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)
        logger.info("Configuration loaded.")
    else:
        logger.warning("No .env file found — defaults will be used until Settings are saved.")

    for key, value in DEFAULT_ENV.items():
        if not os.getenv(key):
            os.environ[key] = value

    try:
        sync_windows_startup()
    except Exception as exc:
        logger.warning(f"Could not update Windows startup entry: {exc}")


# ── Env var helpers ───────────────────────────────────────────────────────────
def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, DEFAULT_ENV.get(key, default))


def get_secret(key: str, default: str = "", encrypted_key: str | None = None) -> str:
    enc_name = encrypted_key or f"{key}_ENCRYPTED"
    enc_val  = str(os.getenv(enc_name, DEFAULT_ENV.get(enc_name, "")) or "").strip()

    if enc_val:
        try:
            decrypted = _get_security_manager().decrypt(enc_val)
            if decrypted:
                return decrypted.strip()
        except Exception as exc:
            logger.warning(f"Could not decrypt {enc_name}: {exc}")

    return str(get_env(key, default) or default).strip()


def save_env_var(key: str, value: str):
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), key, value)
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    logger.info(f"Saved env var: {key}")


def save_secret(key: str, value: str, encrypted_key: str | None = None):
    cleaned = str(value or "").strip()
    target  = encrypted_key or f"{key}_ENCRYPTED"

    if not cleaned:
        save_env_var(target, "")
        save_env_var(key, "")
        return

    try:
        enc = _get_security_manager().encrypt(cleaned)
        save_env_var(target, enc)
        save_env_var(key, "")
    except Exception as exc:
        logger.warning(f"Could not encrypt {key}; saving plain fallback: {exc}")
        save_env_var(target, "")
        save_env_var(key, cleaned)


# ── Accessors ─────────────────────────────────────────────────────────────────
def get_app_display_name() -> str:
    return (get_env("APP_DISPLAY_NAME") or "Notion Local Sync").strip()


def get_claude_mcp_name() -> str:
    return (get_env("CLAUDE_MCP_NAME") or "Notion Local DB").strip()


def is_windows_startup_enabled() -> bool:
    return str(get_env("WINDOWS_STARTUP_ENABLED", "1")).strip().lower() not in {"0", "false", "off", "no"}


def get_tray_icon_path() -> Path | None:
    for directory in (BASE_DIR / "images", BASE_DIR.parent / "images"):
        if not directory.exists():
            continue
        for stem in ("Logo", "logo", "AppIcon", "Icon"):
            for ext in IMAGE_EXTENSIONS:
                p = directory / f"{stem}{ext}"
                if p.exists():
                    return p
        for p in directory.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
                n = p.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
                if "logo" in n:
                    return p
    return None


# ── Windows startup ───────────────────────────────────────────────────────────
def build_windows_startup_command(app_root: Path | str | None = None,
                                   python_executable: str | None = None) -> str:
    root   = Path(app_root) if app_root else BASE_DIR
    script = root / "src" / "main.py"
    exe    = Path(python_executable or sys.executable)
    if exe.name.lower() == "python.exe":
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" "{script}" --startup'


def sync_windows_startup(enabled: bool | None = None,
                          app_root: Path | str | None = None,
                          python_executable: str | None = None) -> tuple[bool, str]:
    desired = is_windows_startup_enabled() if enabled is None else bool(enabled)

    if os.name != "nt" or winreg is None:
        return False, "Windows startup registration is only supported on Windows."

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
        if desired:
            cmd = build_windows_startup_command(app_root=app_root, python_executable=python_executable)
            winreg.SetValueEx(key, WINDOWS_STARTUP_VALUE, 0, winreg.REG_SZ, cmd)
            return True, f"Auto-start enabled. ({cmd})"
        try:
            winreg.DeleteValue(key, WINDOWS_STARTUP_VALUE)
        except FileNotFoundError:
            pass
        return True, "Auto-start disabled."
