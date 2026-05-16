import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv, set_key

try:
    import winreg
except ImportError:  # pragma: no cover - only available on Windows
    winreg = None

# Define paths relative to this file's location inside the src/core structure
BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = BASE_DIR / ".env"
KEY_PATH = BASE_DIR / "secret.key"
MEDIA_DIR = BASE_DIR / "data" / "media"
BACKUP_DIR = BASE_DIR / "data" / "backups"
LOG_DIR = BASE_DIR / "logs"
APP_LOG_FILE = LOG_DIR / "app.log"
ACTIVITY_LOG_FILE = LOG_DIR / "activity_log.jsonl"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ico")
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_STARTUP_VALUE_NAME = "NotionLocalSync"
SecurityManager = None


def _get_security_manager():
    global SecurityManager
    if SecurityManager is None:
        from src.core.security import SecurityManager as _SecurityManager

        SecurityManager = _SecurityManager
    return SecurityManager()

DEFAULT_ENV = {
    "PG_HOST": "localhost",
    "PG_PORT": "5432",
    "PG_USER": "postgres",
    "PG_PASSWORD": "",
    "PG_DBNAME": "notion_mirror",
    "PG_PASSWORD_ENCRYPTED": "",
    "MEDIA_PROXY_PORT": "8080",
    "SYNC_INTERVAL_MINUTES": "2",
    "MAINTENANCE_POLL_MINUTES": "60",
    "MAINTENANCE_RUNS_NIGHT_ONLY": "1",
    "MAINTENANCE_START_TIME": "02:30",
    "MAINTENANCE_WINDOW_HOURS": "4",
    "MAINTENANCE_RUN_ON_WAKE": "0",
    "MAINTENANCE_WINDOW_START_HOUR": "2",
    "MAINTENANCE_WINDOW_END_HOUR": "6",
    "INSTANCE_LOCK_STALE_MINUTES": "10",
    "LOCAL_DB_MAINTENANCE_HOURS": "12",
    "LOCAL_DB_RETENTION_DAYS": "45",
    "LOCAL_DB_MIN_SNAPSHOTS": "5",
    "NOTION_MAINTENANCE_ENABLED": "1",
    "NOTION_MAINTENANCE_INTERVAL_HOURS": "24",
    "NOTION_MAINTENANCE_MODE": "built-in",
    "NOTION_DONE_RETENTION_DAYS": "30",
    "NOTION_PENDING_REVIEW_DAYS": "60",
    "NOTION_STALE_IN_PROGRESS_DAYS": "90",
    "NOTION_MAINTENANCE_REPORT_PAGE_ID": "",
    "CLICKUP_MAINTENANCE_ENABLED": "0",
    "CLICKUP_MAINTENANCE_INTERVAL_HOURS": "24",
    "CLICKUP_TOKEN": "",
    "CLICKUP_TOKEN_ENCRYPTED": "",
    "CLICKUP_TEAM_ID": "",
    "CLICKUP_LIST_IDS": "",
    "CLICKUP_API_BASE": "https://api.clickup.com/api/v2",
    "CLICKUP_WEBHOOK_ENABLED": "1",
    "CLICKUP_WEBHOOK_HOST": "127.0.0.1",
    "CLICKUP_WEBHOOK_PORT": "8765",
    "CLICKUP_WEBHOOK_PATH": "/clickup/webhook",
    "CLICKUP_WEBHOOK_SECRET": "",
    "CLICKUP_CLOSED_STATUS_NAME": "complete",
    "CLICKUP_STALE_DAYS": "14",
    "WORDPRESS_MAINTENANCE_ENABLED": "0",
    "WORDPRESS_MAINTENANCE_INTERVAL_HOURS": "24",
    "WORDPRESS_URL": "",
    "WORDPRESS_USERNAME": "",
    "WORDPRESS_APP_PASSWORD": "",
    "WORDPRESS_APP_PASSWORD_ENCRYPTED": "",
    "WORDPRESS_OLD_DRAFT_DAYS": "120",
    "WORDPRESS_TRASH_RETENTION_DAYS": "30",
    "INTERNAL_HOUSEKEEPING_HOURS": "24",
    "MAINTENANCE_RESULT_RETENTION_DAYS": "30",
    "MAINTENANCE_LOG_MAX_MB": "5",
    "MAINTENANCE_TIMEOUT_SECONDS": "900",
    "BACKUP_PROVIDER": "local",
    "BACKUP_INTERVAL_MINUTES": "10",
    "S3_BUCKET": "",
    "S3_REGION": "us-east-1",
    "S3_ACCESS_KEY_ID": "",
    "S3_SECRET_ACCESS_KEY": "",
    "S3_SECRET_ACCESS_KEY_ENCRYPTED": "",
    "S3_PREFIX": "notion-local-sync",
    "BACKUP_MODE": "incremental",
    "BACKUP_RETENTION_COUNT": "14",
    "WINDOWS_STARTUP_ENABLED": "1",
    "OPEN_DASHBOARD_ON_LAUNCH": "0",
    "N8N_API_URL": "",
    "N8N_API_KEY": "",
    "N8N_API_KEY_ENCRYPTED": "",
    "N8N_VERIFY_SSL": "1",
    "MAUTIC_BASE_URL": "",
    "MAUTIC_ACCESS_TOKEN": "",
    "MAUTIC_ACCESS_TOKEN_ENCRYPTED": "",
    "MAUTIC_CLIENT_ID": "",
    "MAUTIC_CLIENT_SECRET": "",
    "MAUTIC_CLIENT_SECRET_ENCRYPTED": "",
    "MAUTIC_USERNAME": "",
    "MAUTIC_PASSWORD": "",
    "MAUTIC_PASSWORD_ENCRYPTED": "",
    "MAUTIC_VERIFY_SSL": "1",
    "MAUTIC_SSH_HOST": "",
    "MAUTIC_SSH_PORT": "22",
    "MAUTIC_SSH_USERNAME": "",
    "MAUTIC_SSH_PASSWORD": "",
    "MAUTIC_SSH_PASSWORD_ENCRYPTED": "",
    "MAUTIC_SSH_KEY_PATH": "",
    "MAUTIC_DB_NAME": "",
    "MAUTIC_DB_USER": "",
    "MAUTIC_DB_PASSWORD": "",
    "APP_DISPLAY_NAME": "Martin's Brain Dump Manager",
    "APP_VERSION": "2.0 Preview",
    "APP_TAGLINE": "A local AI operations hub for your synced knowledge and connected services.",
    "CLAUDE_MCP_NAME": "Business Brain",
}

# Ensure directories exist
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Setup Logger
logger = logging.getLogger("NotionMiddleware")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    try:
        max_log_mb = max(1, int(str(os.getenv("MAINTENANCE_LOG_MAX_MB", "5") or "5").strip()))
    except (TypeError, ValueError):
        max_log_mb = 5

    file_handler = RotatingFileHandler(
        APP_LOG_FILE,
        maxBytes=max_log_mb * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

def load_config():
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)
        logger.info("Configuration loaded.")
    else:
        logger.warning("No .env file found. Wizard will prompt for setup.")

    for key, value in DEFAULT_ENV.items():
        if not os.getenv(key):
            os.environ[key] = value

    try:
        sync_windows_startup()
    except Exception as exc:
        logger.warning(f"Could not update the Windows startup setting automatically: {exc}")

def save_env_var(key: str, value: str):
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), key, value)
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    logger.info(f"Updated environment variable: {key}")


def save_secret(key: str, value: str, encrypted_key: str | None = None):
    cleaned_value = str(value or "").strip()
    target_key = encrypted_key or f"{key}_ENCRYPTED"

    if not cleaned_value:
        save_env_var(target_key, "")
        save_env_var(key, "")
        return

    try:
        encrypted_value = _get_security_manager().encrypt(cleaned_value)
        save_env_var(target_key, encrypted_value)
        save_env_var(key, "")
    except Exception as exc:
        logger.warning(f"Could not encrypt {key}; saving the fallback plain value instead: {exc}")
        save_env_var(target_key, "")
        save_env_var(key, cleaned_value)


def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, DEFAULT_ENV.get(key, default))


def get_secret(key: str, default: str = "", encrypted_key: str | None = None) -> str:
    encrypted_name = encrypted_key or f"{key}_ENCRYPTED"
    encrypted_value = str(os.getenv(encrypted_name, DEFAULT_ENV.get(encrypted_name, "")) or "").strip()

    if encrypted_value:
        try:
            decrypted_value = _get_security_manager().decrypt(encrypted_value)
            if decrypted_value:
                return decrypted_value.strip()
        except Exception as exc:
            logger.warning(f"Could not decrypt {encrypted_name}: {exc}")

    return str(get_env(key, default) or default).strip()


def get_app_display_name() -> str:
    return (get_env("APP_DISPLAY_NAME", "Martin's Brain Dump Manager") or "Martin's Brain Dump Manager").strip()


def get_app_version() -> str:
    return (get_env("APP_VERSION", "2.0 Preview") or "2.0 Preview").strip()


def get_app_tagline() -> str:
    return (
        get_env(
            "APP_TAGLINE",
            "A local AI operations hub for your synced knowledge and connected services.",
        )
        or "A local AI operations hub for your synced knowledge and connected services."
    ).strip()


def get_tray_icon_path() -> Path | None:
    candidate_dirs = [BASE_DIR / "images", BASE_DIR.parent / "images"]
    preferred_stems = ("Logo", "logo", "AppIcon", "Icon", "Braindump", "Brain Dump", "BrainDump")

    for directory in candidate_dirs:
        if not directory.exists():
            continue

        for stem in preferred_stems:
            for extension in IMAGE_EXTENSIONS:
                candidate = directory / f"{stem}{extension}"
                if candidate.exists():
                    return candidate

        for candidate in directory.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            normalized = candidate.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
            if "logo" in normalized:
                return candidate

    return None


def get_brand_logo_path() -> Path | None:
    return get_tray_icon_path()


def get_claude_mcp_name() -> str:
    return (get_env("CLAUDE_MCP_NAME", "Business Brain") or "Business Brain").strip()


def is_windows_startup_enabled() -> bool:
    value = str(get_env("WINDOWS_STARTUP_ENABLED", "1")).strip().lower()
    return value not in {"0", "false", "off", "no"}


def build_windows_startup_command(app_root: Path | str | None = None, python_executable: str | None = None) -> str:
    root = Path(app_root) if app_root else BASE_DIR
    script_path = root / "src" / "main.py"
    executable = Path(python_executable or sys.executable)

    if executable.name.lower() == "python.exe":
        executable = executable.with_name("pythonw.exe")

    return f'"{executable}" "{script_path}" --startup'


def sync_windows_startup(enabled: bool | None = None, app_root: Path | str | None = None, python_executable: str | None = None) -> tuple[bool, str]:
    desired = is_windows_startup_enabled() if enabled is None else bool(enabled)

    if os.name != "nt" or winreg is None:
        return False, "Windows startup registration is only available on Windows."

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
        if desired:
            command = build_windows_startup_command(app_root=app_root, python_executable=python_executable)
            winreg.SetValueEx(key, WINDOWS_STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
            saved_command, _ = winreg.QueryValueEx(key, WINDOWS_STARTUP_VALUE_NAME)
            return True, f"Open when Windows starts is on. ({saved_command})"

        try:
            winreg.DeleteValue(key, WINDOWS_STARTUP_VALUE_NAME)
        except FileNotFoundError:
            pass
        return True, "Open when Windows starts is off."
