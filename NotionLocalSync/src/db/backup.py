"""
Nightly backup manager.

For SQLite: copies the database file using the sqlite3 backup API (atomic).
For PostgreSQL: exports via pg_dump to a portable .dump file (custom format).

Both write atomically (temp file → rename) so cloud-sync folders are safe.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.core.config import BASE_DIR, get_env, get_secret, logger


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
    FILE_PREFIX    = "notion_mirror_"
    FILE_SUFFIX_PG = ".dump"
    FILE_SUFFIX_SQ = ".db"

    def __init__(self):
        self._backup_dir     = Path(get_env("BACKUP_DIR", "")).expanduser() if get_env("BACKUP_DIR") else None
        self._retention_days = max(1, int(get_env("BACKUP_RETENTION_DAYS", "7") or "7"))
        self._pg_dump        = _find_pg_dump()
        self._backend        = get_env("DB_BACKEND", "sqlite").strip().lower()

    def is_configured(self) -> bool:
        return bool(self._backup_dir)

    def backup_dir_exists(self) -> bool:
        return bool(self._backup_dir and self._backup_dir.is_dir())

    @property
    def _file_suffix(self) -> str:
        return self.FILE_SUFFIX_SQ if self._backend == "sqlite" else self.FILE_SUFFIX_PG

    def today_backup_exists(self) -> bool:
        if not self._backup_dir:
            return False
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return (self._backup_dir / f"{self.FILE_PREFIX}{date_str}{self._file_suffix}").exists()

    def run_backup(self) -> tuple[bool, str]:
        """
        Run a backup, save to backup_dir, prune old backups.
        Returns (success, message).
        """
        # Reload config in case settings changed since __init__
        backup_dir_str = get_env("BACKUP_DIR", "").strip()
        if not backup_dir_str:
            return False, "Backup directory not configured."

        self._backup_dir     = Path(backup_dir_str).expanduser()
        self._retention_days = max(1, int(get_env("BACKUP_RETENTION_DAYS", "7") or "7"))
        self._backend        = get_env("DB_BACKEND", "sqlite").strip().lower()

        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return False, f"Cannot create backup directory: {exc}"

        ts        = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest_name = f"{self.FILE_PREFIX}{ts}_full{self._file_suffix}"
        dest_path = self._backup_dir / dest_name

        if self._backend == "sqlite":
            ok, msg = self._run_sqlite_backup(dest_path)
        else:
            ok, msg = self._run_pg_backup(dest_path)

        if ok:
            self._prune_old_backups()

        return ok, msg

    # ── SQLite backup ─────────────────────────────────────────────────────────
    def _run_sqlite_backup(self, dest_path: Path) -> tuple[bool, str]:
        db_path_str = get_env("SQLITE_PATH", "").strip()
        db_path = Path(db_path_str) if db_path_str else BASE_DIR / "data" / "notion_mirror.db"
        if not db_path.exists():
            return False, "SQLite database file not found."
        tmp = dest_path.with_suffix(".tmp")
        try:
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(str(tmp))
            src.backup(dst)
            dst.close()
            src.close()
            tmp.replace(dest_path)
            size_mb = dest_path.stat().st_size / (1024 * 1024)
            logger.info(f"SQLite backup written: {dest_path} ({size_mb:.1f} MB)")
            return True, f"Backup saved: {dest_path.name} ({size_mb:.1f} MB)"
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            return False, str(exc)

    # ── PostgreSQL backup ─────────────────────────────────────────────────────
    def _run_pg_backup(self, dest_path: Path) -> tuple[bool, str]:
        self._pg_dump = _find_pg_dump()
        if not self._pg_dump:
            return False, (
                "pg_dump not found. Install PostgreSQL client tools, or set "
                "PG_DUMP_PATH in Settings."
            )

        host   = get_env("PG_HOST",   "localhost")
        port   = get_env("PG_PORT",   "5432")
        user   = get_env("PG_USER",   "postgres")
        dbname = get_env("PG_DBNAME", "notion_mirror")
        passwd = get_secret("PG_PASSWORD", "")

        env = {**os.environ, "PGPASSWORD": passwd}

        # Write to a temp file first, then rename — safe for cloud-sync folders
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix=dest_path.name, dir=self._backup_dir
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
            logger.info(f"PostgreSQL backup written: {dest_path} ({size_mb:.1f} MB)")
            return True, f"Backup saved: {dest_path.name} ({size_mb:.1f} MB)"

        except subprocess.TimeoutExpired:
            Path(tmp_path).unlink(missing_ok=True)
            return False, "pg_dump timed out after 5 minutes."
        except Exception as exc:
            Path(tmp_path).unlink(missing_ok=True)
            return False, f"Backup error: {exc}"

    def _prune_old_backups(self):
        if not self._backup_dir or not self._backup_dir.is_dir():
            return
        suffix = self._file_suffix
        cutoff = datetime.now(tz=timezone.utc).timestamp() - (self._retention_days * 86400)
        for f in self._backup_dir.glob(f"{self.FILE_PREFIX}*{suffix}"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info(f"Pruned old backup: {f.name}")
            except Exception as exc:
                logger.warning(f"Could not prune {f.name}: {exc}")
