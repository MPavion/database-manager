"""
Nightly pg_dump backup manager.

Exports the local PostgreSQL mirror to a portable .dump file (custom format)
in a user-configured directory. Old backups are pruned to retain only the
most recent N days.

The backup folder can be a Google Drive / OneDrive sync folder — the .dump
files are written atomically (temp file → rename) so there is no risk of the
cloud client picking up a partial export.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.core.config import get_env, get_secret, logger


# ── pg_dump discovery ─────────────────────────────────────────────────────────

def _find_pg_dump() -> str | None:
    """Return the path to pg_dump, or None if not found."""
    # 1. User override
    override = get_env("PG_DUMP_PATH", "").strip()
    if override and Path(override).is_file():
        return override

    # 2. PATH
    found = shutil.which("pg_dump")
    if found:
        return found

    # 3. Common Windows installation paths
    if os.name == "nt":
        prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        for version in range(17, 11, -1):
            candidate = prog / "PostgreSQL" / str(version) / "bin" / "pg_dump.exe"
            if candidate.is_file():
                return str(candidate)

    return None


# ── BackupManager ─────────────────────────────────────────────────────────────

class BackupManager:
    FILE_PREFIX = "notion_mirror_"
    FILE_SUFFIX = ".dump"

    def __init__(self):
        self._backup_dir     = Path(get_env("BACKUP_DIR", "")).expanduser() if get_env("BACKUP_DIR") else None
        self._retention_days = max(1, int(get_env("BACKUP_RETENTION_DAYS", "7") or "7"))
        self._pg_dump        = _find_pg_dump()

    def is_configured(self) -> bool:
        return bool(self._backup_dir)

    def backup_dir_exists(self) -> bool:
        return bool(self._backup_dir and self._backup_dir.is_dir())

    def today_backup_exists(self) -> bool:
        if not self._backup_dir:
            return False
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return (self._backup_dir / f"{self.FILE_PREFIX}{date_str}{self.FILE_SUFFIX}").exists()

    def run_backup(self) -> tuple[bool, str]:
        """
        Run pg_dump, save to backup_dir, prune old backups.
        Returns (success, message).
        """
        # Reload config in case settings changed since __init__
        backup_dir_str = get_env("BACKUP_DIR", "").strip()
        if not backup_dir_str:
            return False, "Backup directory not configured."

        self._backup_dir     = Path(backup_dir_str).expanduser()
        self._retention_days = max(1, int(get_env("BACKUP_RETENTION_DAYS", "7") or "7"))
        self._pg_dump        = _find_pg_dump()

        if not self._pg_dump:
            return False, (
                "pg_dump not found. Install PostgreSQL client tools, or set "
                "PG_DUMP_PATH in Settings."
            )

        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return False, f"Cannot create backup directory: {exc}"

        date_str  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        dest_name = f"{self.FILE_PREFIX}{date_str}{self.FILE_SUFFIX}"
        dest_path = self._backup_dir / dest_name

        host   = get_env("PG_HOST",   "localhost")
        port   = get_env("PG_PORT",   "5432")
        user   = get_env("PG_USER",   "postgres")
        dbname = get_env("PG_DBNAME", "notion_mirror")
        passwd = get_secret("PG_PASSWORD", "")

        env = {**os.environ, "PGPASSWORD": passwd}

        # Write to a temp file first, then rename — safe for cloud-sync folders
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix=dest_name, dir=self._backup_dir
            )
            os.close(fd)
        except Exception as exc:
            return False, f"Cannot write to backup directory: {exc}"

        cmd = [
            self._pg_dump,
            "-h", host,
            "-p", port,
            "-U", user,
            "-F", "c",        # custom format (compressed, restorable with pg_restore)
            "-f", tmp_path,
            dbname,
        ]

        try:
            result = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                Path(tmp_path).unlink(missing_ok=True)
                return False, f"pg_dump failed: {result.stderr.strip()}"

            Path(tmp_path).rename(dest_path)
            size_mb = dest_path.stat().st_size / (1024 * 1024)
            logger.info(f"Backup written: {dest_path} ({size_mb:.1f} MB)")

        except subprocess.TimeoutExpired:
            Path(tmp_path).unlink(missing_ok=True)
            return False, "pg_dump timed out after 5 minutes."
        except Exception as exc:
            Path(tmp_path).unlink(missing_ok=True)
            return False, f"Backup error: {exc}"

        self._prune_old_backups()
        return True, f"Backup saved: {dest_name} ({size_mb:.1f} MB)"

    def _prune_old_backups(self):
        if not self._backup_dir or not self._backup_dir.is_dir():
            return
        cutoff = datetime.now(tz=timezone.utc).timestamp() - (self._retention_days * 86400)
        for f in self._backup_dir.glob(f"{self.FILE_PREFIX}*{self.FILE_SUFFIX}"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info(f"Pruned old backup: {f.name}")
            except Exception as exc:
                logger.warning(f"Could not prune {f.name}: {exc}")
