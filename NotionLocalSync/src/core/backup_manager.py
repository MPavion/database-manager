import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg2.extras import RealDictCursor

from src.core.config import BACKUP_DIR, get_env, get_secret, logger


class BackupManager:
    def __init__(self, db_manager: object | None, backup_dir: Path | None = None):
        self.db = db_manager
        self.backup_dir = Path(backup_dir or BACKUP_DIR)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.backup_dir / "backup_manifest.json"

    @staticmethod
    def format_bytes(size: int | float | None) -> str:
        if size is None:
            return "Not available yet"

        size_value = float(size)
        units = ["B", "KB", "MB", "GB", "TB"]
        for unit in units:
            if size_value < 1024 or unit == units[-1]:
                return f"{size_value:.1f} {unit}" if unit != "B" else f"{int(size_value)} B"
            size_value /= 1024
        return f"{size_value:.1f} TB"

    @staticmethod
    def describe_interval(minutes: int | str | None) -> str:
        try:
            minutes_value = int(minutes or 0)
        except (TypeError, ValueError):
            minutes_value = 0

        if minutes_value <= 0:
            return "Manual only"
        if minutes_value % 10080 == 0:
            weeks = minutes_value // 10080
            return f"Every {weeks} week(s)"
        if minutes_value % 1440 == 0:
            days = minutes_value // 1440
            return f"Every {days} day(s)"
        if minutes_value % 60 == 0:
            hours = minutes_value // 60
            return f"Every {hours} hour(s)"
        return f"Every {minutes_value} minute(s)"

    @staticmethod
    def normalize_time_text(value: str | None, default: str = "05:00") -> str:
        text = str(value or "").strip()
        if not text:
            return default

        try:
            hour_text, minute_text = text.split(":", 1)
            hour = max(0, min(23, int(hour_text)))
            minute = max(0, min(59, int(minute_text)))
            return f"{hour:02d}:{minute:02d}"
        except Exception:
            return default

    @classmethod
    def describe_schedule(cls, minutes: int | str | None, preferred_time: str | None = None) -> str:
        schedule_text = cls.describe_interval(minutes)
        try:
            minutes_value = int(minutes or 0)
        except (TypeError, ValueError):
            minutes_value = 0

        if minutes_value >= 1440 and schedule_text != "Manual only":
            return f"{schedule_text} at {cls.normalize_time_text(preferred_time)}"
        return schedule_text

    @classmethod
    def is_schedule_due(
        cls,
        last_run_at: str | None,
        interval_minutes: int | str | None,
        preferred_time: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        try:
            minutes_value = int(interval_minutes or 0)
        except (TypeError, ValueError):
            minutes_value = 0

        if minutes_value <= 0:
            return False

        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        last_run = cls._parse_iso(last_run_at)
        if last_run and last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)

        if minutes_value < 1440:
            if not last_run:
                return True
            return current_time.astimezone(timezone.utc) - last_run.astimezone(timezone.utc) >= timedelta(minutes=minutes_value)

        local_now = current_time.astimezone()
        schedule_text = cls.normalize_time_text(preferred_time)
        hour, minute = [int(part) for part in schedule_text.split(":", 1)]
        anchor = datetime(2024, 1, 1, hour, minute, tzinfo=local_now.tzinfo)
        elapsed_minutes = max(0, int((local_now - anchor).total_seconds() // 60))
        window_count = elapsed_minutes // minutes_value
        latest_window = anchor + timedelta(minutes=window_count * minutes_value)

        if not last_run:
            return local_now >= latest_window

        return last_run.astimezone(local_now.tzinfo) < latest_window <= local_now

    @staticmethod
    def _json_default(value):
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _parse_iso(value: str | None):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def load_manifest(self) -> list[dict[str, Any]]:
        if not self.manifest_path.exists():
            return []

        try:
            with open(self.manifest_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.error(f"Could not read backup manifest: {exc}")
            return []

    def _save_manifest(self, entries: list[dict[str, Any]]):
        try:
            with open(self.manifest_path, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"Could not save backup manifest: {exc}")

    def get_database_size_bytes(self, attempt_connect: bool = False) -> int | None:
        if not self.db:
            return None

        if not getattr(self.db, "conn", None):
            if not attempt_connect or not self.db.connect():
                return None

        try:
            with self.db.conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database())")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            logger.error(f"Could not read PostgreSQL database size: {exc}")
            return None

    def get_configured_provider(self) -> str:
        provider = get_env("BACKUP_PROVIDER", "local").strip().lower()
        if not provider:
            return "local"
        if provider in {"local", "s3"}:
            return provider
        return "s3"

    def _provider_label(self, provider: str) -> str:
        labels = {
            "local": "Local folder only",
            "s3": "Amazon S3",
        }
        return labels.get(provider, "Local folder only")

    def _destination_text(self, provider: str) -> str:
        if provider == "s3":
            bucket = get_env("S3_BUCKET")
            prefix = get_env("S3_PREFIX", "notion-local-sync").strip("/")
            if bucket:
                suffix = f"/{prefix}" if prefix else ""
                return f"Amazon S3 bucket `{bucket}{suffix}`"
            return f"Local backup folder `{self.backup_dir}` until an Amazon S3 bucket is added"

        return f"Local backup folder `{self.backup_dir}`"

    def get_status(self, attempt_connect: bool = False) -> dict[str, Any]:
        manifest = self.load_manifest()
        last_backup = manifest[-1] if manifest else None
        provider = self.get_configured_provider()
        retention = max(1, self._safe_int(get_env("BACKUP_RETENTION_COUNT", "14"), 14))
        db_size = self.get_database_size_bytes(attempt_connect=attempt_connect)
        mode = "full"

        return {
            "provider": provider,
            "provider_label": self._provider_label(provider),
            "destination_text": self._destination_text(provider),
            "database_size_bytes": db_size,
            "database_size_text": self.format_bytes(db_size),
            "schedule_text": self.describe_schedule(
                get_env("BACKUP_INTERVAL_MINUTES", "10"),
                get_env("BACKUP_SCHEDULE_TIME", "05:00"),
            ),
            "mode": mode,
            "mode_text": "Always store a full copy",
            "retention_count": retention,
            "retention_text": f"Keep the newest {retention} backup(s) and remove older ones automatically",
            "last_backup_at": (last_backup or {}).get("completed_at"),
            "last_backup_text": (last_backup or {}).get("summary", "No backups have run yet"),
            "backup_count": len(manifest),
            "local_folder": str(self.backup_dir),
        }

    def _collect_backup_rows(self, incremental_since: str | None = None) -> list[dict[str, Any]]:
        if not self.db or not getattr(self.db, "conn", None):
            raise RuntimeError("PostgreSQL is not connected yet.")

        query = """
            SELECT
                notion_id,
                title,
                ai_summary,
                raw_json,
                media_local_paths,
                content_hash,
                source_updated_at,
                pulled_at,
                last_pushed_at,
                updated_at,
                valid_from,
                valid_to,
                is_active,
                needs_push
            FROM workspace_mirror
        """
        params: list = []

        since_dt = self._parse_iso(incremental_since)
        if since_dt:
            query += " WHERE updated_at >= %s OR pulled_at >= %s OR last_pushed_at >= %s"
            params = [since_dt, since_dt, since_dt]

        query += " ORDER BY updated_at ASC NULLS LAST, notion_id ASC"

        with self.db.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def apply_retention(
        self,
        manifest_entries: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        entries = list(manifest_entries if manifest_entries is not None else self.load_manifest())
        keep_count = max(1, self._safe_int(get_env("BACKUP_RETENTION_COUNT", "14"), 14))

        if len(entries) <= keep_count:
            self._save_manifest(entries)
            return entries, []

        removed = entries[:-keep_count]
        kept = entries[-keep_count:]

        for entry in removed:
            local_path = Path(entry.get("local_path", "")) if entry.get("local_path") else None
            if local_path and local_path.exists():
                try:
                    local_path.unlink()
                except Exception as exc:
                    logger.warning(f"Could not delete old backup file {local_path}: {exc}")

        self._save_manifest(kept)
        return kept, removed

    def _upload_to_s3(self, file_path: Path) -> tuple[bool, str, str]:
        bucket = get_env("S3_BUCKET").strip()
        prefix = get_env("S3_PREFIX", "notion-local-sync").strip().strip("/")
        if not bucket:
            return False, "Amazon S3 is selected, but the bucket name is still blank. The local backup was kept safely.", ""

        try:
            import boto3
        except ImportError:
            return False, "Amazon S3 upload is ready in settings, but `boto3` is not installed yet. The local backup was kept safely.", ""

        try:
            client_args = {
                "region_name": get_env("S3_REGION", "us-east-1").strip() or "us-east-1",
            }
            access_key = get_env("S3_ACCESS_KEY_ID").strip()
            secret_key = get_secret("S3_SECRET_ACCESS_KEY").strip()
            if access_key and secret_key:
                client_args["aws_access_key_id"] = access_key
                client_args["aws_secret_access_key"] = secret_key

            s3_client = boto3.client("s3", **client_args)
            object_key = f"{prefix}/{file_path.name}" if prefix else file_path.name
            s3_client.upload_file(str(file_path), bucket, object_key)
            remote_path = f"s3://{bucket}/{object_key}"
            return True, f"Uploaded the backup to {remote_path}.", remote_path
        except Exception as exc:
            logger.error(f"Amazon S3 upload failed: {exc}")
            return False, f"Amazon S3 upload failed: {exc}. The local backup was still saved.", ""

    def _upload_to_google_drive(self, file_path: Path) -> tuple[bool, str, str]:
        credentials_path = get_env("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON").strip()
        folder_id = get_env("GOOGLE_DRIVE_FOLDER_ID").strip()
        if not credentials_path:
            return False, "Google Drive is selected, but the service account JSON path is still blank. The local backup was kept safely.", ""

        credentials_file = Path(credentials_path)
        if not credentials_file.exists():
            return False, "Google Drive credentials file was not found at the saved path. The local backup was kept safely.", ""

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload
        except ImportError:
            return False, "Google Drive upload is ready in settings, but the Google API packages are not installed yet. The local backup was kept safely.", ""

        try:
            scopes = ["https://www.googleapis.com/auth/drive.file"]
            credentials = service_account.Credentials.from_service_account_file(str(credentials_file), scopes=scopes)
            service = build("drive", "v3", credentials=credentials, cache_discovery=False)

            metadata = {"name": file_path.name}
            if folder_id:
                metadata["parents"] = [folder_id]

            media = MediaFileUpload(str(file_path), mimetype="application/gzip", resumable=False)
            response = service.files().create(body=metadata, media_body=media, fields="id, webViewLink").execute()
            remote_path = response.get("webViewLink") or f"Google Drive file ID {response.get('id', 'unknown')}"
            return True, f"Uploaded the backup to Google Drive ({remote_path}).", remote_path
        except Exception as exc:
            logger.error(f"Google Drive upload failed: {exc}")
            return False, f"Google Drive upload failed: {exc}. The local backup was still saved.", ""

    def _upload_backup(self, file_path: Path) -> tuple[bool, str, str]:
        provider = self.get_configured_provider()
        if provider == "s3":
            bucket = get_env("S3_BUCKET").strip()
            if bucket:
                return self._upload_to_s3(file_path)
            return True, f"Stored the backup locally in `{self.backup_dir}`. Add an Amazon S3 bucket any time to mirror future backups to the cloud.", str(file_path)
        return True, f"Stored the backup locally in `{self.backup_dir}`.", str(file_path)

    def create_backup(self, force_mode: str | None = None, reason: str = "manual") -> tuple[bool, str]:
        if self.db and not getattr(self.db, "conn", None) and not self.db.connect():
            return False, "Could not connect to PostgreSQL, so the backup could not start."

        try:
            manifest = self.load_manifest()
            last_entry = manifest[-1] if manifest else None
            requested_mode = "full"
            incremental_since = None
            effective_mode = "full"

            started_at = datetime.now(timezone.utc)
            timestamp_label = started_at.strftime("%Y%m%d-%H%M%S")
            db_name = get_env("PG_DBNAME", "notion_mirror")
            file_path = self.backup_dir / f"{db_name}_{timestamp_label}_{effective_mode}.json.gz"

            rows = self._collect_backup_rows(incremental_since if effective_mode == "incremental" else None)
            database_size = self.get_database_size_bytes(attempt_connect=True)
            payload = {
                "created_at": started_at.isoformat(),
                "reason": reason,
                "database_name": db_name,
                "backup_mode": effective_mode,
                "requested_mode": requested_mode,
                "incremental_since": incremental_since,
                "database_size_bytes": database_size,
                "row_count": len(rows),
                "rows": rows,
            }

            with gzip.open(file_path, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=self._json_default)

            upload_ok, upload_message, remote_path = self._upload_backup(file_path)
            file_size_text = self.format_bytes(file_path.stat().st_size)
            row_text = f"{len(rows)} mirrored row(s)"
            summary = (
                f"{effective_mode.title()} backup complete: {file_size_text}, {row_text}, database size {self.format_bytes(database_size)}."
            )

            manifest.append(
                {
                    "completed_at": started_at.isoformat(),
                    "reason": reason,
                    "mode": effective_mode,
                    "requested_mode": requested_mode,
                    "provider": self.get_configured_provider(),
                    "local_path": str(file_path),
                    "remote_path": remote_path,
                    "row_count": len(rows),
                    "database_size_bytes": database_size,
                    "upload_ok": upload_ok,
                    "summary": summary,
                }
            )
            manifest, removed = self.apply_retention(manifest)
            cleanup_note = f" Removed {len(removed)} older backup(s) automatically." if removed else ""
            result_message = f"{summary} {upload_message}{cleanup_note}".strip()

            if self.db and hasattr(self.db, "log_activity"):
                self.db.log_activity(
                    category="backup",
                    action=f"{reason or 'manual'}_backup",
                    status="success" if upload_ok else "warning",
                    summary=result_message,
                    details={
                        "backup_mode": effective_mode,
                        "requested_mode": requested_mode,
                        "row_count": len(rows),
                        "database_size_bytes": database_size,
                        "provider": self.get_configured_provider(),
                        "remote_path": remote_path,
                    },
                    backup_path=str(file_path),
                )

            return True, result_message
        except Exception as exc:
            logger.error(f"Backup creation failed: {exc}")
            failure_message = f"Backup creation failed: {exc}"
            if self.db and hasattr(self.db, "log_activity"):
                self.db.log_activity(
                    category="backup",
                    action=f"{reason or 'manual'}_backup",
                    status="error",
                    summary=failure_message,
                    details={"requested_mode": force_mode or get_env("BACKUP_MODE", "incremental")},
                )
            return False, failure_message
