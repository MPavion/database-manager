from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

import time as _time

from src.core.config import APP_LOG_FILE, get_env, get_secret, logger
from src.core.http import build_retry_session


def _get_sync_engine_class():
    from src.sync.engine import SyncEngine
    return SyncEngine


class MaintenanceManager:
    TASK_LOCAL = "local_database"
    TASK_NOTION = "notion_database"
    TASK_CLICKUP = "clickup_tasks"
    TASK_WORDPRESS = "wordpress_site"
    TASK_INTERNAL = "internal_housekeeping"

    TASK_LABELS = {
        TASK_LOCAL: "Local DB",
        TASK_NOTION: "Notion DB",
        TASK_CLICKUP: "ClickUp",
        TASK_WORDPRESS: "WordPress",
        TASK_INTERNAL: "Housekeeping",
    }

    MANUAL_JOB_TARGETS = {
        "full_notion_dedup": [TASK_NOTION],
        "knowledge_library_check": [TASK_NOTION],
        "full_clickup_dedup": [TASK_CLICKUP],
        "full_wordpress_dedup": [TASK_WORDPRESS],
        "post_weekly_report": [TASK_INTERNAL],
    }

    def __init__(self, db_manager=None, app_root: Path | str | None = None, workspace_root: Path | str | None = None):
        self.db_manager = db_manager
        self.app_root = Path(app_root) if app_root else Path(__file__).resolve().parents[2]
        self.workspace_root = Path(workspace_root) if workspace_root else self.app_root.parent
        self.config_path = self.workspace_root / "maintenance_config.json"
        self.local_config_path = self.workspace_root / "local_config.json"
        self.notion_script_path = self.workspace_root / "notion_maintenance.py"
        self.controller_path = self.workspace_root / "notion_controller.py"
        self.log_dir = self.app_root / "logs"
        self.results_dir = self.workspace_root / "results"
        self.state_path = self.log_dir / "maintenance_status.json"
        self.http = build_retry_session(user_agent="NotionLocalSync-Maintenance/2.0")

        self._readiness_cache: dict[str, tuple[float, dict]] = {}
        self._snapshot_cache: tuple[float, dict] | None = None
        self._cache_ttl = 60.0  # seconds

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_int(value: Any, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(str(value).strip()))
        except Exception:
            return max(minimum, default)

    @staticmethod
    def _safe_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() not in {"0", "false", "off", "no", ""}

    @staticmethod
    def _parse_csv(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [part.strip() for part in str(value or "").split(",") if part.strip()]

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit():
            try:
                return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
            except Exception:
                return None

        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _normalize_name(value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip()).lower()
        text = re.sub(r"[^a-z0-9\s_-]+", "", text)
        return text.strip()

    @staticmethod
    def _merge_dicts(base: dict, override: dict | None) -> dict:
        merged = dict(base or {})
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = MaintenanceManager._merge_dicts(merged.get(key) or {}, value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _default_config() -> dict:
        return {
            "jobs": {
                MaintenanceManager.TASK_LOCAL: {
                    "enabled": True,
                    "interval_hours": 12,
                    "manual_jobs": ["run_full_maintenance"],
                },
                MaintenanceManager.TASK_NOTION: {
                    "enabled": True,
                    "interval_hours": 24,
                    "manual_jobs": ["full_notion_dedup", "knowledge_library_check"],
                },
                MaintenanceManager.TASK_CLICKUP: {
                    "enabled": True,
                    "interval_hours": 24,
                    "manual_jobs": ["full_clickup_dedup"],
                },
                MaintenanceManager.TASK_WORDPRESS: {
                    "enabled": True,
                    "interval_hours": 24,
                    "manual_jobs": ["full_wordpress_dedup"],
                },
                MaintenanceManager.TASK_INTERNAL: {
                    "enabled": True,
                    "interval_hours": 24,
                    "manual_jobs": ["post_weekly_report"],
                },
            },
            "thresholds": {
                "notion_done_retention_days": 30,
                "notion_pending_review_days": 60,
                "notion_stale_in_progress_days": 90,
                "notion_empty_page_char_limit": 24,
                "clickup_stale_days": 14,
                "wordpress_old_draft_days": 120,
                "wordpress_trash_retention_days": 30,
                "result_retention_days": 30,
                "log_max_mb": 5,
            },
            "reporting": {
                "weekday": "sunday",
                "notion_log_page_id": "",
            },
        }

    @staticmethod
    def _default_state() -> dict:
        return {
            MaintenanceManager.TASK_LOCAL: {
                "last_run_at": None,
                "last_status": "never",
                "last_summary": "Local database clean-up has not run yet.",
                "last_details": {},
            },
            MaintenanceManager.TASK_NOTION: {
                "last_run_at": None,
                "last_status": "never",
                "last_summary": "Notion maintenance has not run yet.",
                "last_details": {},
            },
            MaintenanceManager.TASK_CLICKUP: {
                "last_run_at": None,
                "last_status": "never",
                "last_summary": "ClickUp maintenance has not run yet.",
                "last_details": {},
            },
            MaintenanceManager.TASK_WORDPRESS: {
                "last_run_at": None,
                "last_status": "never",
                "last_summary": "WordPress maintenance has not run yet.",
                "last_details": {},
            },
            MaintenanceManager.TASK_INTERNAL: {
                "last_run_at": None,
                "last_status": "never",
                "last_summary": "Internal housekeeping has not run yet.",
                "last_details": {},
            },
        }

    @staticmethod
    def is_task_due(last_run_at: str | None, interval_hours: int, now: datetime | None = None) -> bool:
        if interval_hours <= 0:
            return True

        if not last_run_at:
            return True

        current_time = now or datetime.now(timezone.utc)

        try:
            last_run = datetime.fromisoformat(str(last_run_at).replace("Z", "+00:00"))
        except Exception:
            return True

        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)

        return current_time - last_run >= timedelta(hours=interval_hours)

    @staticmethod
    def _format_timestamp(value: str | None) -> str:
        if not value:
            return "not run yet"
        try:
            dt_value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt_value.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(value)

    @classmethod
    def format_status_summary(cls, state: dict) -> str:
        snapshot = cls._default_state()
        labels = []

        for task_name in (
            cls.TASK_LOCAL,
            cls.TASK_NOTION,
            cls.TASK_CLICKUP,
            cls.TASK_WORDPRESS,
            cls.TASK_INTERNAL,
        ):
            entry = dict(snapshot.get(task_name) or {})
            entry.update((state or {}).get(task_name) or {})
            summary = entry.get("last_summary") or snapshot[task_name]["last_summary"]
            labels.append(
                f"{cls.TASK_LABELS.get(task_name, task_name)}: {summary} "
                f"(last check: {cls._format_timestamp(entry.get('last_run_at'))})"
            )

        return " | ".join(labels)

    def load_maintenance_config(self) -> dict:
        config = self._default_config()
        if not self.config_path.exists():
            return config

        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                return self._merge_dicts(config, loaded)
        except Exception as exc:
            logger.warning(f"Could not read maintenance_config.json: {exc}")

        return config

    def load_local_config(self) -> dict:
        if not self.local_config_path.exists():
            return {}

        try:
            with open(self.local_config_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
        except Exception as exc:
            logger.warning(f"Could not read local_config.json: {exc}")
            return {}

    def _task_enabled(self, task_name: str) -> bool:
        config = self.load_maintenance_config()
        enabled_in_config = self._safe_bool((config.get("jobs", {}).get(task_name, {}) or {}).get("enabled", True), True)
        env_map = {
            self.TASK_NOTION: "NOTION_MAINTENANCE_ENABLED",
            self.TASK_CLICKUP: "CLICKUP_MAINTENANCE_ENABLED",
            self.TASK_WORDPRESS: "WORDPRESS_MAINTENANCE_ENABLED",
        }
        env_key = env_map.get(task_name)
        return enabled_in_config and self._safe_bool(get_env(env_key, "1"), True) if env_key else enabled_in_config

    def _task_interval_hours(self, task_name: str, fallback: int) -> int:
        config = self.load_maintenance_config()
        configured = (config.get("jobs", {}).get(task_name, {}) or {}).get("interval_hours", fallback)
        env_map = {
            self.TASK_LOCAL: "LOCAL_DB_MAINTENANCE_HOURS",
            self.TASK_NOTION: "NOTION_MAINTENANCE_INTERVAL_HOURS",
            self.TASK_CLICKUP: "CLICKUP_MAINTENANCE_INTERVAL_HOURS",
            self.TASK_WORDPRESS: "WORDPRESS_MAINTENANCE_INTERVAL_HOURS",
            self.TASK_INTERNAL: "INTERNAL_HOUSEKEEPING_HOURS",
        }
        return self._safe_int(get_env(env_map.get(task_name, ""), str(configured)), self._safe_int(configured, fallback), minimum=0)

    def load_state(self) -> dict:
        state = self._default_state()
        if not self.state_path.exists():
            return state

        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                for key, default_entry in state.items():
                    entry = loaded.get(key)
                    if isinstance(entry, dict):
                        default_entry.update(entry)
        except Exception as exc:
            logger.warning(f"Could not read maintenance status file: {exc}")

        return state

    def save_state(self, state: dict):
        try:
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"Could not save maintenance status file: {exc}")

    def update_task_state(self, task_name: str, status: str, summary: str, details: dict | None = None) -> dict:
        state = self.load_state()
        state.setdefault(task_name, {})
        state[task_name].update(
            {
                "last_run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "last_status": status,
                "last_summary": summary,
                "last_details": details or {},
            }
        )
        self.save_state(state)
        return state

    def _save_result_file(self, task_name: str, status: str, summary: str, details: dict | None = None) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_path = self.results_dir / f"{task_name}_{timestamp}.json"
        payload = {
            "task": task_name,
            "label": self.TASK_LABELS.get(task_name, task_name),
            "status": status,
            "summary": summary,
            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "details": details or {},
        }
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, default=str)
        return result_path

    def _log_activity(self, action: str, status: str, summary: str, details: dict | None = None):
        if self.db_manager and hasattr(self.db_manager, "log_activity"):
            try:
                self.db_manager.log_activity(
                    category="maintenance",
                    action=action,
                    status=status,
                    summary=summary,
                    details=details or {},
                )
            except Exception as exc:
                logger.warning(f"Could not write maintenance activity log entry: {exc}")

    def _finalize_task(
        self,
        task_name: str,
        success: bool,
        summary: str,
        details: dict | None = None,
        ran: bool = True,
        status: str | None = None,
    ) -> tuple[bool, str, bool]:
        normalized_status = status or ("success" if success else "error")
        payload = dict(details or {})
        try:
            payload["result_file"] = str(self._save_result_file(task_name, normalized_status, summary, payload))
        except Exception as exc:
            logger.warning(f"Could not save maintenance result file for {task_name}: {exc}")
        self.update_task_state(task_name, normalized_status, summary, payload)
        self._log_activity(task_name, normalized_status, summary, payload)
        return success, summary, ran

    def is_notion_maintenance_enabled(self) -> bool:
        return self._task_enabled(self.TASK_NOTION)

    def is_clickup_maintenance_enabled(self) -> bool:
        return self._task_enabled(self.TASK_CLICKUP)

    def is_wordpress_maintenance_enabled(self) -> bool:
        return self._task_enabled(self.TASK_WORDPRESS)

    def _load_clickup_settings(self) -> dict:
        local = (self.load_local_config().get("clickup") or {}) if isinstance(self.load_local_config(), dict) else {}
        return {
            "token": str(local.get("token") or get_secret("CLICKUP_TOKEN", "")).strip(),
            "team_id": str(local.get("team_id") or get_env("CLICKUP_TEAM_ID", "")).strip(),
            "list_ids": self._parse_csv(local.get("list_ids") or get_env("CLICKUP_LIST_IDS", "")),
            "api_base": str(local.get("api_base") or "https://api.clickup.com/api/v2").rstrip("/"),
            "closed_status_name": str(local.get("closed_status_name") or get_env("CLICKUP_CLOSED_STATUS_NAME", "complete")).strip() or "complete",
        }

    def _load_wordpress_settings(self) -> dict:
        local = (self.load_local_config().get("wordpress") or {}) if isinstance(self.load_local_config(), dict) else {}
        return {
            "base_url": str(local.get("base_url") or get_env("WORDPRESS_URL", "")).strip(),
            "username": str(local.get("username") or get_env("WORDPRESS_USERNAME", "")).strip(),
            "application_password": str(local.get("application_password") or get_secret("WORDPRESS_APP_PASSWORD", "")).strip(),
            "site_label": str(local.get("site_label") or get_env("WORDPRESS_SITE_LABEL", "WordPress site")).strip() or "WordPress site",
        }

    def _load_notion_reporting_settings(self) -> dict:
        local = (self.load_local_config().get("notion") or {}) if isinstance(self.load_local_config(), dict) else {}
        config = self.load_maintenance_config()
        reporting = config.get("reporting", {}) or {}
        return {
            "report_page_id": str(local.get("report_page_id") or get_env("NOTION_MAINTENANCE_REPORT_PAGE_ID", "") or reporting.get("notion_log_page_id", "")).strip(),
            "weekday": str(local.get("report_weekday") or reporting.get("weekday", "sunday")).strip().lower() or "sunday",
        }

    def get_notion_maintenance_readiness(self) -> dict:
        disabled = not self.is_notion_maintenance_enabled()
        missing: list[str] = []
        if not self.config_path.exists():
            missing.append("maintenance_config.json")
        if not self.db_manager or not getattr(self.db_manager, "conn", None):
            missing.append("database connection")

        # Check Notion readiness via env vars only — no API calls.
        notion_token = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        notion_ready = bool(notion_token)
        db_id_setting = str(get_env("NOTION_DB_ID", "") or "").strip()
        db_ready = bool(db_id_setting)

        if not notion_ready:
            missing.append("Notion access token")
        if not db_ready:
            missing.append("Notion database ID(s)")

        if self.notion_script_path.exists() and not self.controller_path.exists():
            missing.append("notion_controller.py")

        missing = list(dict.fromkeys(missing))
        if missing:
            summary = "Waiting for " + ", ".join(missing) + " before Notion maintenance can run."
            return {"ready": False, "summary": summary, "missing": missing, "disabled": False}

        if disabled:
            return {
                "ready": False,
                "summary": "Notion maintenance is currently turned off in the settings.",
                "missing": [],
                "disabled": True,
            }

        return {
            "ready": True,
            "summary": "Notion housekeeping is ready inside the app and can archive duplicates, stale items, and old finished pages.",
            "missing": [],
            "disabled": False,
        }

    def get_clickup_maintenance_readiness(self) -> dict:
        settings = self._load_clickup_settings()
        disabled = not self.is_clickup_maintenance_enabled()
        missing: list[str] = []
        if not self.config_path.exists():
            missing.append("maintenance_config.json")
        if not settings.get("token"):
            missing.append("ClickUp token")
        if not settings.get("list_ids"):
            missing.append("ClickUp list IDs")

        if missing:
            return {
                "ready": False,
                "summary": "Waiting for " + ", ".join(missing) + " before ClickUp maintenance can run.",
                "missing": missing,
                "disabled": False,
            }

        if disabled:
            return {
                "ready": False,
                "summary": "ClickUp maintenance is currently turned off in the settings.",
                "missing": [],
                "disabled": True,
            }

        return {
            "ready": True,
            "summary": f"ClickUp maintenance is ready for {len(settings['list_ids'])} list(s), including duplicate cleanup and hygiene tags.",
            "missing": [],
            "disabled": False,
        }

    def get_wordpress_maintenance_readiness(self) -> dict:
        settings = self._load_wordpress_settings()
        disabled = not self.is_wordpress_maintenance_enabled()
        missing: list[str] = []
        if not self.config_path.exists():
            missing.append("maintenance_config.json")
        if not settings.get("base_url"):
            missing.append("WordPress URL")
        if not settings.get("username"):
            missing.append("WordPress username")
        if not settings.get("application_password"):
            missing.append("WordPress app password")

        if missing:
            return {
                "ready": False,
                "summary": "Waiting for " + ", ".join(missing) + " before WordPress maintenance can run.",
                "missing": missing,
                "disabled": False,
            }

        if disabled:
            return {
                "ready": False,
                "summary": "WordPress maintenance is currently turned off in the settings.",
                "missing": [],
                "disabled": True,
            }

        return {
            "ready": True,
            "summary": f"WordPress maintenance is ready for {settings['site_label']}, including drafts, spam, and trash cleanup.",
            "missing": [],
            "disabled": False,
        }

    def get_internal_housekeeping_readiness(self) -> dict:
        return {
            "ready": True,
            "summary": "Internal housekeeping is ready to rotate logs, prune result files, and prepare a weekly report.",
            "missing": [],
            "disabled": False,
        }

    def get_task_readiness(self, task_name: str) -> dict:
        now = _time.monotonic()
        cached = self._readiness_cache.get(task_name)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        readiness_map = {
            self.TASK_NOTION: self.get_notion_maintenance_readiness,
            self.TASK_CLICKUP: self.get_clickup_maintenance_readiness,
            self.TASK_WORDPRESS: self.get_wordpress_maintenance_readiness,
            self.TASK_INTERNAL: self.get_internal_housekeeping_readiness,
            self.TASK_LOCAL: lambda: {
                "ready": bool(self.db_manager),
                "summary": "Local database clean-up is ready to prune older snapshots and refresh PostgreSQL statistics."
                if self.db_manager
                else "Local database clean-up is waiting for the database connection.",
                "missing": [] if self.db_manager else ["database manager"],
                "disabled": False,
            },
        }
        result = readiness_map.get(task_name, self.get_internal_housekeeping_readiness)()
        self._readiness_cache[task_name] = (now, result)
        return result

    @staticmethod
    def _parse_time_of_day(value: Any, default_minutes: int = 150) -> int:
        text = str(value or "").strip()
        if not text:
            return default_minutes

        match = re.match(r"^(\d{1,2}):(\d{2})$", text)
        if not match:
            return default_minutes

        hour = max(0, min(int(match.group(1)), 23))
        minute = max(0, min(int(match.group(2)), 59))
        return hour * 60 + minute

    @staticmethod
    def _format_minutes_of_day(total_minutes: int) -> str:
        normalized = int(total_minutes or 0) % (24 * 60)
        return f"{normalized // 60:02d}:{normalized % 60:02d}"

    def get_maintenance_window(self) -> tuple[bool, int, int]:
        enabled = self._safe_bool(get_env("MAINTENANCE_RUNS_NIGHT_ONLY", "1"), True)
        window_hours = max(1, self._safe_int(get_env("MAINTENANCE_WINDOW_HOURS", "4"), 4, minimum=1))

        start_time_text = str(get_env("MAINTENANCE_START_TIME", "02:30") or "").strip()
        if start_time_text:
            start_minutes = self._parse_time_of_day(start_time_text, default_minutes=150)
        else:
            start_hour = min(23, self._safe_int(get_env("MAINTENANCE_WINDOW_START_HOUR", "2"), 2, minimum=0))
            start_minutes = start_hour * 60

        end_minutes = (start_minutes + (window_hours * 60)) % (24 * 60)
        return enabled, start_minutes, end_minutes

    def describe_maintenance_window(self) -> str:
        enabled, start_minutes, end_minutes = self.get_maintenance_window()
        if not enabled:
            return "any time"
        return f"{self._format_minutes_of_day(start_minutes)}–{self._format_minutes_of_day(end_minutes)}"

    def should_catch_up_after_wake(self) -> bool:
        return self._safe_bool(get_env("MAINTENANCE_RUN_ON_WAKE", "0"), False)

    def get_latest_scheduled_start(self, now: datetime | None = None) -> datetime:
        current_time = now or datetime.now(timezone.utc)
        local_time = current_time.astimezone() if current_time.tzinfo is not None else current_time
        _enabled, start_minutes, _end_minutes = self.get_maintenance_window()
        scheduled = local_time.replace(
            hour=start_minutes // 60,
            minute=start_minutes % 60,
            second=0,
            microsecond=0,
        )
        if local_time < scheduled:
            scheduled -= timedelta(days=1)
        return scheduled

    def is_within_maintenance_window(self, now: datetime | None = None) -> bool:
        enabled, start_minutes, end_minutes = self.get_maintenance_window()
        if not enabled or start_minutes == end_minutes:
            return True

        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is not None:
            current_time = current_time.astimezone()

        current_minutes = (current_time.hour * 60) + current_time.minute
        if start_minutes < end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    def get_due_tasks(
        self,
        state: dict | None,
        local_interval_hours: int = 12,
        notion_interval_hours: int = 24,
        now: datetime | None = None,
        clickup_interval_hours: int | None = None,
        wordpress_interval_hours: int | None = None,
        internal_interval_hours: int | None = None,
    ) -> list[str]:
        provided_state = state is not None
        state = state or self.load_state()
        current_time = now or datetime.now(timezone.utc)
        intervals = {
            self.TASK_LOCAL: local_interval_hours,
            self.TASK_NOTION: notion_interval_hours,
            self.TASK_CLICKUP: clickup_interval_hours if clickup_interval_hours is not None else self._task_interval_hours(self.TASK_CLICKUP, 24),
            self.TASK_WORDPRESS: wordpress_interval_hours if wordpress_interval_hours is not None else self._task_interval_hours(self.TASK_WORDPRESS, 24),
            self.TASK_INTERNAL: internal_interval_hours if internal_interval_hours is not None else self._task_interval_hours(self.TASK_INTERNAL, 24),
        }

        within_window = self.is_within_maintenance_window(now=current_time)
        catch_up_allowed = self.should_catch_up_after_wake()
        latest_scheduled_start = self.get_latest_scheduled_start(current_time)

        due_tasks: list[str] = []
        for task_name, interval in intervals.items():
            if provided_state and task_name not in state and task_name not in {self.TASK_LOCAL, self.TASK_NOTION}:
                continue

            task_state = state.get(task_name) or {}
            last_run_at = self._parse_timestamp(task_state.get("last_run_at"))
            due_now = self.is_task_due(task_state.get("last_run_at"), interval, now=current_time)
            if not due_now:
                continue

            may_run_now = within_window or (
                catch_up_allowed
                and (last_run_at is None or last_run_at.astimezone() < latest_scheduled_start)
            )
            if not may_run_now:
                continue

            if task_name == self.TASK_LOCAL:
                due_tasks.append(task_name)
                continue

            readiness = self.get_task_readiness(task_name)
            if readiness.get("ready"):
                due_tasks.append(task_name)
        return due_tasks

    def get_status_snapshot(self) -> dict:
        now = _time.monotonic()
        if self._snapshot_cache and (now - self._snapshot_cache[0]) < self._cache_ttl:
            return self._snapshot_cache[1]

        state = self.load_state()
        for task_name in (self.TASK_LOCAL, self.TASK_NOTION, self.TASK_CLICKUP, self.TASK_WORDPRESS, self.TASK_INTERNAL):
            readiness = self.get_task_readiness(task_name)
            entry = state.get(task_name) or {}
            if entry.get("last_status") in {"never", "skipped"}:
                entry["last_summary"] = readiness.get("summary") or entry.get("last_summary")
                state[task_name] = entry

        self._snapshot_cache = (now, state)
        return state

    def get_status_text(self) -> str:
        return self.format_status_summary(self.get_status_snapshot())

    @staticmethod
    def _extract_notion_title(page: dict) -> str:
        props = (page or {}).get("properties", {}) or {}
        for prop in props.values():
            if prop.get("type") == "title":
                title = "".join(item.get("plain_text", "") for item in prop.get("title", []))
                if title.strip():
                    return title.strip()
        return "Untitled"

    @staticmethod
    def _extract_notion_status(page: dict) -> str:
        props = (page or {}).get("properties", {}) or {}
        weighted: list[tuple[int, str]] = []

        for name, prop in props.items():
            prop_type = prop.get("type")
            value = ""
            if prop_type == "status":
                value = str((prop.get("status") or {}).get("name") or "")
            elif prop_type == "select":
                value = str((prop.get("select") or {}).get("name") or "")
            elif prop_type == "multi_select":
                value = ", ".join(item.get("name", "") for item in prop.get("multi_select", []))

            if value.strip():
                priority = 0 if any(keyword in str(name).lower() for keyword in ("status", "state", "stage")) else 1
                weighted.append((priority, value.strip().lower()))

        weighted.sort(key=lambda item: item[0])
        return weighted[0][1] if weighted else ""

    @staticmethod
    def _extract_notion_text(page: dict) -> str:
        props = (page or {}).get("properties", {}) or {}
        parts: list[str] = []

        for prop in props.values():
            prop_type = prop.get("type")
            if prop_type == "title":
                parts.append("".join(item.get("plain_text", "") for item in prop.get("title", [])))
            elif prop_type == "rich_text":
                parts.append("".join(item.get("plain_text", "") for item in prop.get("rich_text", [])))

        return " ".join(part.strip() for part in parts if part and str(part).strip())

    @staticmethod
    def _classify_notion_bucket(database_title: str) -> str:
        label = str(database_title or "").lower()
        if "contact" in label:
            return "contacts"
        if "project" in label:
            return "projects"
        if any(keyword in label for keyword in ("knowledge", "library", "wiki", "sop")):
            return "knowledge"
        return "general"

    def _should_use_legacy_notion_script(self) -> bool:
        mode = str(get_env("NOTION_MAINTENANCE_MODE", "built-in")).strip().lower()
        return mode in {"legacy", "legacy-script", "script"} and self.notion_script_path.exists()

    def _run_external_notion_script(self, manual_job: str | None = None) -> tuple[bool, str, bool]:
        timeout_seconds = self._safe_int(get_env("MAINTENANCE_TIMEOUT_SECONDS", "900"), 900, minimum=60)
        command = [sys.executable, str(self.notion_script_path)]

        if manual_job == "knowledge_library_check":
            command.extend(["--job", "Knowledge Library Integrity Check"])

        extra_args = str(get_env("NOTION_MAINTENANCE_ARGS", "")).strip()
        if extra_args:
            command.extend(shlex.split(extra_args, posix=False))

        if self._safe_bool(get_env("NOTION_MAINTENANCE_DRY_RUN", "0")):
            command.append("--dry-run")

        try:
            result = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip()).strip()
            summary = output.splitlines()[-1] if output else "Remote Notion clean-up finished."
            details = {"command": command, "returncode": result.returncode, "output": output}
            if result.returncode == 0:
                return self._finalize_task(self.TASK_NOTION, True, summary, details, ran=True)
            return self._finalize_task(
                self.TASK_NOTION,
                False,
                f"Remote Notion clean-up returned exit code {result.returncode}. {summary}".strip(),
                details,
                ran=True,
            )
        except Exception as exc:
            return self._finalize_task(
                self.TASK_NOTION,
                False,
                f"Remote Notion clean-up could not start: {exc}",
                {"command": command},
                ran=False,
            )

    def _append_notion_report(self, engine, title: str, lines: list[str]) -> bool:
        report_settings = self._load_notion_reporting_settings()
        page_id = report_settings.get("report_page_id")
        if not page_id or not engine.notion:
            return False

        blocks = []
        if title:
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": title[:1800]}}]},
                }
            )

        for line in lines[:20]:
            clean_line = " ".join(str(line or "").split())
            if not clean_line:
                continue
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": clean_line[:1800]}}]},
                }
            )

        if not blocks:
            return False

        try:
            engine.notion.blocks.children.append(block_id=page_id, children=blocks)
            return True
        except Exception as exc:
            logger.warning(f"Could not post the maintenance summary back into Notion: {exc}")
            return False

    def run_local_database_maintenance(self) -> tuple[bool, str, bool]:
        if not self.db_manager:
            return self._finalize_task(
                self.TASK_LOCAL,
                True,
                "Local database clean-up is not available yet because the database manager is missing.",
                {"missing": ["database manager"]},
                ran=False,
                status="skipped",
            )

        retention_days = self._safe_int(get_env("LOCAL_DB_RETENTION_DAYS", "45"), 45, minimum=1)
        min_versions = self._safe_int(get_env("LOCAL_DB_MIN_SNAPSHOTS", "5"), 5, minimum=1)

        success, summary, details = self.db_manager.run_maintenance(
            retention_days=retention_days,
            min_versions_to_keep=min_versions,
        )
        return self._finalize_task(self.TASK_LOCAL, success, summary, details, ran=True)

    def run_notion_database_maintenance(self, manual_job: str | None = None) -> tuple[bool, str, bool]:
        readiness = self.get_notion_maintenance_readiness()
        if not readiness.get("ready"):
            return self._finalize_task(
                self.TASK_NOTION,
                True,
                readiness.get("summary", "Notion maintenance is not ready yet."),
                readiness,
                ran=False,
                status="skipped",
            )

        if self._should_use_legacy_notion_script() and self.controller_path.exists():
            return self._run_external_notion_script(manual_job=manual_job)

        engine = _get_sync_engine_class()(self.db_manager)
        if not engine.notion:
            return self._finalize_task(
                self.TASK_NOTION,
                True,
                "Notion maintenance is waiting for a valid Notion token.",
                readiness,
                ran=False,
                status="skipped",
            )

        target_db_ids = engine._target_db_ids()
        if not target_db_ids:
            return self._finalize_task(
                self.TASK_NOTION,
                True,
                "Notion maintenance is waiting for at least one shared target database.",
                readiness,
                ran=False,
                status="skipped",
            )

        config = self.load_maintenance_config()
        thresholds = config.get("thresholds", {}) or {}
        done_days = self._safe_int(get_env("NOTION_DONE_RETENTION_DAYS", str(thresholds.get("notion_done_retention_days", 30))), 30, minimum=1)
        pending_days = self._safe_int(get_env("NOTION_PENDING_REVIEW_DAYS", str(thresholds.get("notion_pending_review_days", 60))), 60, minimum=1)
        stale_days = self._safe_int(get_env("NOTION_STALE_IN_PROGRESS_DAYS", str(thresholds.get("notion_stale_in_progress_days", 90))), 90, minimum=1)
        empty_limit = self._safe_int(thresholds.get("notion_empty_page_char_limit", 24), 24, minimum=0)
        dry_run = self._safe_bool(get_env("NOTION_MAINTENANCE_DRY_RUN", "0"))
        now = datetime.now(timezone.utc)
        focus_knowledge = manual_job == "knowledge_library_check"
        report_only = manual_job == "post_weekly_report"

        accessible = engine.discover_accessible_databases()
        database_titles = {item.get("id"): item.get("title", "Untitled database") for item in accessible}
        planned_archives: dict[str, str] = {}
        counters = defaultdict(int)
        errors: list[str] = []

        if not report_only:
            duplicate_groups: dict[tuple[str, str, str], list[tuple[datetime, dict]]] = defaultdict(list)

            for db_id in target_db_ids:
                db_title = database_titles.get(db_id, "Untitled database")
                bucket = self._classify_notion_bucket(db_title)
                if focus_knowledge and bucket != "knowledge":
                    continue

                for page in engine._query_database_pages(db_id):
                    if (page or {}).get("archived"):
                        continue

                    title = self._extract_notion_title(page)
                    normalized_title = self._normalize_name(title)
                    status_text = self._extract_notion_status(page)
                    text_blob = self._extract_notion_text(page)
                    last_activity = self._parse_timestamp(page.get("last_edited_time")) or self._parse_timestamp(page.get("created_time")) or now
                    age_days = max(0, int((now - last_activity).total_seconds() // 86400))
                    page_id = str(page.get("id") or "").strip()
                    if not page_id:
                        continue

                    if normalized_title:
                        duplicate_groups[(db_id, bucket, normalized_title)].append((last_activity, page))

                    if "cancel" in status_text:
                        planned_archives.setdefault(page_id, "cancelled")
                    elif any(keyword in status_text for keyword in ("done", "complete", "completed")) and age_days >= done_days:
                        planned_archives.setdefault(page_id, "old_done")
                    elif "pending review" in status_text and age_days >= pending_days:
                        planned_archives.setdefault(page_id, "pending_review")
                    elif "in progress" in status_text and age_days >= stale_days:
                        planned_archives.setdefault(page_id, "stale_progress")

                    if bucket == "knowledge" and (not normalized_title or normalized_title in {"untitled", "new page"}) and len(text_blob.strip()) <= empty_limit:
                        planned_archives.setdefault(page_id, "empty_knowledge")

            for (db_id, bucket, _normalized_title), items in duplicate_groups.items():
                if len(items) <= 1:
                    continue

                items.sort(key=lambda item: item[0], reverse=True)
                for _last_activity, page in items[1:]:
                    page_id = str(page.get("id") or "").strip()
                    if not page_id:
                        continue
                    reason = {
                        "contacts": "duplicate_contact",
                        "projects": "duplicate_project",
                        "knowledge": "duplicate_knowledge",
                    }.get(bucket, "duplicate_item")
                    planned_archives.setdefault(page_id, reason)

            for page_id, reason in planned_archives.items():
                counters[reason] += 1
                if dry_run:
                    continue
                try:
                    engine.notion.pages.update(page_id=page_id, archived=True)
                    counters["archived_total"] += 1
                except Exception as exc:
                    errors.append(f"{page_id}: {exc}")

        summary_lines = []
        if report_only:
            summary_lines.append("Prepared the weekly maintenance report on demand.")
        else:
            if counters["duplicate_contact"]:
                summary_lines.append(f"deduplicated {counters['duplicate_contact']} contact page(s)")
            if counters["duplicate_project"]:
                summary_lines.append(f"deduplicated {counters['duplicate_project']} project page(s)")
            if counters["cancelled"]:
                summary_lines.append(f"archived {counters['cancelled']} cancelled item(s)")
            if counters["old_done"]:
                summary_lines.append(f"archived {counters['old_done']} older done item(s)")
            if counters["duplicate_knowledge"] or counters["empty_knowledge"]:
                summary_lines.append(
                    f"ran the knowledge check ({counters['duplicate_knowledge']} duplicate item(s), {counters['empty_knowledge']} empty untitled page(s))"
                )
            if counters["pending_review"]:
                summary_lines.append(f"cleared {counters['pending_review']} long-pending review item(s)")
            if counters["stale_progress"]:
                summary_lines.append(f"cleared {counters['stale_progress']} stale in-progress item(s)")

        if not summary_lines:
            summary_lines.append("found nothing that needed tidying this round")

        note_lines = [
            f"Notion maintenance ran on {now.astimezone().strftime('%Y-%m-%d %H:%M')}",
            "- " + "\n- ".join(summary_lines),
        ]
        report_posted = self._append_notion_report(engine, "Weekly tidy log", note_lines) if (report_only or focus_knowledge or now.weekday() == 6) else False

        details = {
            "dry_run": dry_run,
            "manual_job": manual_job,
            "target_databases": target_db_ids,
            "counts": dict(counters),
            "errors": errors,
            "notion_report_posted": report_posted,
        }
        success = not errors
        prefix = "Notion maintenance report" if report_only else "Notion tidy"
        summary = f"{prefix}: " + ", ".join(summary_lines) + "."
        return self._finalize_task(self.TASK_NOTION, success, summary, details, ran=True)

    @staticmethod
    def _clickup_task_is_closed(task: dict) -> bool:
        status = task.get("status") or {}
        status_type = str(status.get("type") or "").strip().lower()
        status_name = str(status.get("status") or status.get("name") or "").strip().lower()
        return status_type == "closed" or status_name in {"closed", "complete", "completed", "done"}

    @staticmethod
    def _clickup_tag_names(task: dict) -> list[str]:
        tags = []
        for tag in (task or {}).get("tags", []) or []:
            if isinstance(tag, dict):
                tags.append(str(tag.get("name") or "").strip())
            elif str(tag).strip():
                tags.append(str(tag).strip())
        return [tag for tag in tags if tag]

    def _clickup_headers(self, token: str) -> dict:
        return {"Authorization": token, "Content-Type": "application/json"}

    def _clickup_fetch_tasks(self, settings: dict) -> list[dict]:
        tasks: list[dict] = []
        headers = self._clickup_headers(settings["token"])

        for list_id in settings.get("list_ids", []):
            page = 0
            while True:
                response = self.http.get(
                    f"{settings['api_base']}/list/{list_id}/task",
                    headers=headers,
                    params={"page": page, "include_closed": "true", "subtasks": "true"},
                    timeout=30,
                )
                response.raise_for_status()
                batch = response.json().get("tasks", []) or []
                if not batch:
                    break
                tasks.extend(batch)
                if len(batch) < 100:
                    break
                page += 1

        return tasks

    def _clickup_update_task(self, settings: dict, task_id: str, payload: dict):
        response = self.http.put(
            f"{settings['api_base']}/task/{task_id}",
            headers=self._clickup_headers(settings["token"]),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()

    def _clickup_delete_task(self, settings: dict, task_id: str):
        response = self.http.delete(
            f"{settings['api_base']}/task/{task_id}",
            headers=self._clickup_headers(settings["token"]),
            timeout=30,
        )
        response.raise_for_status()

    def run_clickup_maintenance(self, manual_job: str | None = None) -> tuple[bool, str, bool]:
        readiness = self.get_clickup_maintenance_readiness()
        if not readiness.get("ready"):
            return self._finalize_task(
                self.TASK_CLICKUP,
                True,
                readiness.get("summary", "ClickUp maintenance is not ready yet."),
                readiness,
                ran=False,
                status="skipped",
            )

        settings = self._load_clickup_settings()
        config = self.load_maintenance_config()
        thresholds = config.get("thresholds", {}) or {}
        stale_days = self._safe_int(get_env("CLICKUP_STALE_DAYS", str(thresholds.get("clickup_stale_days", 14))), 14, minimum=1)
        dry_run = self._safe_bool(get_env("CLICKUP_MAINTENANCE_DRY_RUN", get_env("NOTION_MAINTENANCE_DRY_RUN", "0")))
        now = datetime.now(timezone.utc)
        overdue_tag = "overdue"
        stale_tag = f"stale-{stale_days}d"
        errors: list[str] = []

        try:
            tasks = self._clickup_fetch_tasks(settings)
        except Exception as exc:
            return self._finalize_task(
                self.TASK_CLICKUP,
                False,
                f"ClickUp maintenance could not read the configured lists: {exc}",
                {"manual_job": manual_job},
                ran=True,
            )

        duplicates = defaultdict(list)
        counts = defaultdict(int)
        for task in tasks:
            name_key = self._normalize_name(task.get("name"))
            if name_key:
                updated_at = self._parse_timestamp(task.get("date_updated")) or now
                duplicates[name_key].append((updated_at, task))

        for _name_key, grouped in duplicates.items():
            if len(grouped) <= 1:
                continue
            grouped.sort(key=lambda item: item[0], reverse=True)
            for _updated_at, task in grouped[1:]:
                counts["duplicates_removed"] += 1
                if dry_run:
                    continue
                try:
                    self._clickup_delete_task(settings, str(task.get("id")))
                except Exception as exc:
                    errors.append(f"delete {task.get('id')}: {exc}")

        for task in tasks:
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                continue

            status_text = str(((task.get("status") or {}).get("status") or "")).strip().lower()
            is_closed = self._clickup_task_is_closed(task)
            current_tags = self._clickup_tag_names(task)
            desired_tags = set(current_tags)
            updated_at = self._parse_timestamp(task.get("date_updated")) or now
            due_at = self._parse_timestamp(task.get("due_date"))

            if "cancel" in status_text and not is_closed:
                counts["cancelled_closed"] += 1
                if not dry_run:
                    try:
                        self._clickup_update_task(settings, task_id, {"status": settings["closed_status_name"]})
                    except Exception as exc:
                        errors.append(f"close {task_id}: {exc}")

            if is_closed:
                before = set(desired_tags)
                desired_tags -= {overdue_tag, stale_tag}
                if desired_tags != before:
                    counts["resolved_tags_cleared"] += 1
            else:
                if due_at and due_at < now:
                    desired_tags.add(overdue_tag)
                if now - updated_at >= timedelta(days=stale_days):
                    desired_tags.add(stale_tag)
                if overdue_tag in desired_tags:
                    counts["overdue_flagged"] += 1
                if stale_tag in desired_tags:
                    counts["stale_flagged"] += 1

            if set(current_tags) != desired_tags and not dry_run:
                try:
                    self._clickup_update_task(settings, task_id, {"tags": sorted(desired_tags)})
                except Exception as exc:
                    errors.append(f"tag {task_id}: {exc}")

        summary = (
            f"ClickUp tidy: removed {counts['duplicates_removed']} duplicate task(s), "
            f"closed {counts['cancelled_closed']} cancelled task(s), and refreshed overdue/stale tags on "
            f"{counts['resolved_tags_cleared'] + counts['overdue_flagged'] + counts['stale_flagged']} task(s)."
        )
        details = {
            "manual_job": manual_job,
            "dry_run": dry_run,
            "list_ids": settings.get("list_ids", []),
            "task_count": len(tasks),
            "counts": dict(counts),
            "errors": errors,
        }
        return self._finalize_task(self.TASK_CLICKUP, not errors, summary, details, ran=True)

    def _wordpress_headers(self, settings: dict) -> dict:
        token = base64.b64encode(f"{settings['username']}:{settings['application_password']}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _wordpress_get_all(self, settings: dict, resource: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        base_url = settings["base_url"].rstrip("/") + "/wp-json/wp/v2/"

        while True:
            response = self.http.get(
                urljoin(base_url, resource),
                headers=self._wordpress_headers(settings),
                params={**(params or {}), "per_page": 100, "page": page},
                timeout=30,
            )

            if response.status_code == 400 and "rest_post_invalid_page_number" in response.text:
                break

            response.raise_for_status()
            batch = response.json() or []
            if not batch:
                break
            items.extend(batch)
            total_pages = self._safe_int(response.headers.get("X-WP-TotalPages", "1"), 1, minimum=1)
            if page >= total_pages:
                break
            page += 1

        return items

    def _wordpress_update(self, settings: dict, resource: str, item_id: Any, payload: dict, method: str = "POST"):
        base_url = settings["base_url"].rstrip("/") + "/wp-json/wp/v2/"
        response = self.http.request(
            method,
            urljoin(base_url, f"{resource}/{item_id}"),
            headers=self._wordpress_headers(settings),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()

    def _wordpress_delete(self, settings: dict, resource: str, item_id: Any, force: bool = False):
        base_url = settings["base_url"].rstrip("/") + "/wp-json/wp/v2/"
        response = self.http.delete(
            urljoin(base_url, f"{resource}/{item_id}"),
            headers=self._wordpress_headers(settings),
            params={"force": str(bool(force)).lower()},
            timeout=30,
        )
        response.raise_for_status()

    def run_wordpress_maintenance(self, manual_job: str | None = None) -> tuple[bool, str, bool]:
        readiness = self.get_wordpress_maintenance_readiness()
        if not readiness.get("ready"):
            return self._finalize_task(
                self.TASK_WORDPRESS,
                True,
                readiness.get("summary", "WordPress maintenance is not ready yet."),
                readiness,
                ran=False,
                status="skipped",
            )

        settings = self._load_wordpress_settings()
        config = self.load_maintenance_config()
        thresholds = config.get("thresholds", {}) or {}
        old_draft_days = self._safe_int(get_env("WORDPRESS_OLD_DRAFT_DAYS", str(thresholds.get("wordpress_old_draft_days", 120))), 120, minimum=1)
        trash_days = self._safe_int(get_env("WORDPRESS_TRASH_RETENTION_DAYS", str(thresholds.get("wordpress_trash_retention_days", 30))), 30, minimum=1)
        dry_run = self._safe_bool(get_env("WORDPRESS_MAINTENANCE_DRY_RUN", get_env("NOTION_MAINTENANCE_DRY_RUN", "0")))
        now = datetime.now(timezone.utc)
        errors: list[str] = []
        counts = defaultdict(int)

        try:
            posts = self._wordpress_get_all(settings, "posts", {"status": "draft,publish,trash"})
            pages = self._wordpress_get_all(settings, "pages", {"status": "draft,trash"})
            comments = self._wordpress_get_all(settings, "comments", {"status": "hold,spam,trash"})
            media_items = self._wordpress_get_all(settings, "media", {"status": "inherit,trash"})
        except Exception as exc:
            return self._finalize_task(
                self.TASK_WORDPRESS,
                False,
                f"WordPress maintenance could not reach {settings['site_label']}: {exc}",
                {"manual_job": manual_job, "site": settings.get("base_url")},
                ran=True,
            )

        def older_than(item: dict, days: int, field_name: str = "date") -> bool:
            dt = self._parse_timestamp(item.get(field_name) or item.get("modified") or item.get("date_gmt"))
            return bool(dt and now - dt >= timedelta(days=days))

        published_groups = defaultdict(list)
        for post in posts:
            title = ((post.get("title") or {}).get("rendered") or "").strip()
            if title and post.get("status") == "publish":
                published_groups[self._normalize_name(title)].append(post)

            if post.get("status") == "draft" and older_than(post, old_draft_days):
                counts["old_drafts_trashed"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "posts", post.get("id"), force=False)
                    except Exception as exc:
                        errors.append(f"trash draft {post.get('id')}: {exc}")

            if post.get("status") == "trash" and older_than(post, trash_days):
                counts["old_trash_purged"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "posts", post.get("id"), force=True)
                    except Exception as exc:
                        errors.append(f"purge post {post.get('id')}: {exc}")

        for _title_key, grouped in published_groups.items():
            if len(grouped) <= 1:
                continue
            grouped.sort(key=lambda item: self._parse_timestamp(item.get("modified")) or now, reverse=True)
            for duplicate in grouped[1:]:
                counts["duplicate_posts_trashed"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "posts", duplicate.get("id"), force=False)
                    except Exception as exc:
                        errors.append(f"trash duplicate {duplicate.get('id')}: {exc}")

        for page in pages:
            if page.get("status") == "draft" and older_than(page, old_draft_days):
                counts["old_drafts_trashed"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "pages", page.get("id"), force=False)
                    except Exception as exc:
                        errors.append(f"trash page draft {page.get('id')}: {exc}")
            if page.get("status") == "trash" and older_than(page, trash_days):
                counts["old_trash_purged"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "pages", page.get("id"), force=True)
                    except Exception as exc:
                        errors.append(f"purge page {page.get('id')}: {exc}")

        for comment in comments:
            status = str(comment.get("status") or "").strip().lower()
            if status == "hold":
                counts["pending_comments_approved"] += 1
                if not dry_run:
                    try:
                        self._wordpress_update(settings, "comments", comment.get("id"), {"status": "approve"})
                    except Exception as exc:
                        errors.append(f"approve comment {comment.get('id')}: {exc}")
            elif status in {"spam", "trash"} and older_than(comment, trash_days, field_name="date"):
                counts["spam_comments_purged"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "comments", comment.get("id"), force=True)
                    except Exception as exc:
                        errors.append(f"purge comment {comment.get('id')}: {exc}")

        for media_item in media_items:
            parent_id = int(media_item.get("post") or 0)
            status = str(media_item.get("status") or "").strip().lower()
            if status == "trash" and older_than(media_item, trash_days):
                counts["old_trash_purged"] += 1
                if not dry_run:
                    try:
                        self._wordpress_delete(settings, "media", media_item.get("id"), force=True)
                    except Exception as exc:
                        errors.append(f"purge media {media_item.get('id')}: {exc}")
            elif parent_id == 0:
                counts["orphaned_media_skipped"] += 1

        summary = (
            f"WordPress tidy: trashed {counts['old_drafts_trashed']} old draft(s), removed {counts['duplicate_posts_trashed']} duplicate post(s), "
            f"approved {counts['pending_comments_approved']} pending comment(s), purged {counts['old_trash_purged']} old trash item(s), "
            f"kept {counts['orphaned_media_skipped']} orphaned media file(s), and purged {counts['spam_comments_purged']} spam or trashed comment(s)."
        )
        details = {
            "manual_job": manual_job,
            "dry_run": dry_run,
            "site": settings.get("base_url"),
            "counts": dict(counts),
            "errors": errors,
        }
        return self._finalize_task(self.TASK_WORDPRESS, not errors, summary, details, ran=True)

    def _rotate_log_file(self, path: Path, max_bytes: int) -> bool:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated = path.with_name(f"{path.stem}_{timestamp}{path.suffix}")
        path.replace(rotated)
        return True

    def _delete_old_results(self, retention_days: int) -> int:
        deleted = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        for path in self.results_dir.glob("*.json"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        return deleted

    def _should_post_weekly_report(self, state: dict, now: datetime | None = None) -> bool:
        current_time = now or datetime.now(timezone.utc)
        weekday_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        report_settings = self._load_notion_reporting_settings()
        target_day = weekday_map.get(report_settings.get("weekday", "sunday"), 6)
        if current_time.weekday() != target_day:
            return False

        last_report_at = ((state.get(self.TASK_INTERNAL) or {}).get("last_details") or {}).get("weekly_report_posted_at")
        return self.is_task_due(last_report_at, 24 * 6, now=current_time)

    def _build_weekly_report_lines(self, state: dict) -> list[str]:
        lines = ["Weekly maintenance summary"]
        for task_name in (self.TASK_LOCAL, self.TASK_NOTION, self.TASK_CLICKUP, self.TASK_WORDPRESS, self.TASK_INTERNAL):
            entry = state.get(task_name) or {}
            lines.append(
                f"{self.TASK_LABELS.get(task_name, task_name)} — {entry.get('last_status', 'never')}: "
                f"{entry.get('last_summary', 'No activity yet.') }"
            )
        return lines

    def run_internal_housekeeping(self, manual_job: str | None = None) -> tuple[bool, str, bool]:
        config = self.load_maintenance_config()
        thresholds = config.get("thresholds", {}) or {}
        retention_days = self._safe_int(get_env("MAINTENANCE_RESULT_RETENTION_DAYS", str(thresholds.get("result_retention_days", 30))), 30, minimum=1)
        max_log_mb = self._safe_int(get_env("MAINTENANCE_LOG_MAX_MB", str(thresholds.get("log_max_mb", 5))), 5, minimum=1)
        max_bytes = max_log_mb * 1024 * 1024

        rotated_logs = 0
        for log_path in [APP_LOG_FILE, self.workspace_root / "notion_maintenance.log"]:
            try:
                if self._rotate_log_file(log_path, max_bytes):
                    rotated_logs += 1
            except Exception as exc:
                logger.warning(f"Could not rotate log file {log_path}: {exc}")

        deleted_results = self._delete_old_results(retention_days)
        state = self.get_status_snapshot()
        report_requested = manual_job == "post_weekly_report" or self._should_post_weekly_report(state)
        report_posted = False
        weekly_report_path = ""

        if report_requested:
            lines = self._build_weekly_report_lines(state)
            report_name = f"weekly_maintenance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            report_path = self.results_dir / report_name
            report_path.write_text("\n".join(f"- {line}" if index else f"# {line}" for index, line in enumerate(lines)), encoding="utf-8")
            weekly_report_path = str(report_path)

            engine = _get_sync_engine_class()(self.db_manager)
            if engine.notion:
                report_posted = self._append_notion_report(engine, "Weekly maintenance report", lines)

        details = {
            "manual_job": manual_job,
            "rotated_logs": rotated_logs,
            "deleted_result_files": deleted_results,
            "weekly_report_posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds") if report_requested else None,
            "weekly_report_path": weekly_report_path,
            "report_posted_to_notion": report_posted,
        }
        summary_bits = [f"rotated {rotated_logs} log file(s)", f"deleted {deleted_results} older result file(s)"]
        if report_requested:
            summary_bits.append("posted the weekly report to Notion" if report_posted else "saved the weekly report locally")
        summary = "Internal housekeeping: " + ", ".join(summary_bits) + "."
        return self._finalize_task(self.TASK_INTERNAL, True, summary, details, ran=True)

    def _run_tasks(self, tasks: list[str], manual_job: str | None = None) -> tuple[bool, str, dict]:
        overall_success = True
        ran_any = False
        summaries = []

        for task_name in tasks:
            if task_name == self.TASK_LOCAL:
                success, summary, task_ran = self.run_local_database_maintenance()
            elif task_name == self.TASK_NOTION:
                success, summary, task_ran = self.run_notion_database_maintenance(manual_job=manual_job)
            elif task_name == self.TASK_CLICKUP:
                success, summary, task_ran = self.run_clickup_maintenance(manual_job=manual_job)
            elif task_name == self.TASK_WORDPRESS:
                success, summary, task_ran = self.run_wordpress_maintenance(manual_job=manual_job)
            elif task_name == self.TASK_INTERNAL:
                success, summary, task_ran = self.run_internal_housekeeping(manual_job=manual_job)
            else:
                continue

            overall_success = overall_success and success
            ran_any = ran_any or task_ran
            if summary:
                summaries.append(summary)

        state = self.get_status_snapshot()
        combined_summary = " ".join(summaries).strip() or self.format_status_summary(state)
        return overall_success, combined_summary, {"ran_any": ran_any, "tasks": tasks, "manual_job": manual_job, "state": state}

    def preview_due_maintenance(self, state: dict | None = None, now: datetime | None = None) -> tuple[bool, str, dict]:
        snapshot = state or self.get_status_snapshot()
        current_time = now or datetime.now(timezone.utc)
        intervals = {
            self.TASK_LOCAL: self._task_interval_hours(self.TASK_LOCAL, 12),
            self.TASK_NOTION: self._task_interval_hours(self.TASK_NOTION, 24),
            self.TASK_CLICKUP: self._task_interval_hours(self.TASK_CLICKUP, 24),
            self.TASK_WORDPRESS: self._task_interval_hours(self.TASK_WORDPRESS, 24),
            self.TASK_INTERNAL: self._task_interval_hours(self.TASK_INTERNAL, 24),
        }

        due_tasks = self.get_due_tasks(
            snapshot,
            local_interval_hours=intervals[self.TASK_LOCAL],
            notion_interval_hours=intervals[self.TASK_NOTION],
            clickup_interval_hours=intervals[self.TASK_CLICKUP],
            wordpress_interval_hours=intervals[self.TASK_WORDPRESS],
            internal_interval_hours=intervals[self.TASK_INTERNAL],
            now=current_time,
        )
        if not due_tasks:
            if not self.is_within_maintenance_window(now=current_time):
                wake_note = " If the machine was asleep, it can catch up after wake." if self.should_catch_up_after_wake() else ""
                summary = f"Scheduled clean-up is waiting for the night window ({self.describe_maintenance_window()}).{wake_note}"
                return False, summary, {
                    "ran_any": False,
                    "tasks": [],
                    "state": snapshot,
                    "window": self.describe_maintenance_window(),
                    "now": current_time,
                    "intervals": intervals,
                }
            return False, self.format_status_summary(snapshot), {
                "ran_any": False,
                "tasks": [],
                "state": snapshot,
                "now": current_time,
                "intervals": intervals,
            }

        return True, f"Scheduled clean-up is due for {len(due_tasks)} task(s).", {
            "ran_any": False,
            "tasks": due_tasks,
            "state": snapshot,
            "now": current_time,
            "intervals": intervals,
        }

    def run_due_maintenance(self) -> tuple[bool, str, dict]:
        should_run, preview_summary, preview_details = self.preview_due_maintenance()
        if not should_run:
            return True, preview_summary, preview_details

        state = preview_details.get("state") or self.get_status_snapshot()
        now = preview_details.get("now") or datetime.now(timezone.utc)
        intervals = preview_details.get("intervals") or {
            self.TASK_LOCAL: self._task_interval_hours(self.TASK_LOCAL, 12),
            self.TASK_NOTION: self._task_interval_hours(self.TASK_NOTION, 24),
            self.TASK_CLICKUP: self._task_interval_hours(self.TASK_CLICKUP, 24),
            self.TASK_WORDPRESS: self._task_interval_hours(self.TASK_WORDPRESS, 24),
            self.TASK_INTERNAL: self._task_interval_hours(self.TASK_INTERNAL, 24),
        }

        for task_name, interval in intervals.items():
            readiness = self.get_task_readiness(task_name)
            task_state = state.get(task_name) or {}
            if not readiness.get("ready") and self.is_task_due(task_state.get("last_run_at"), interval, now=now):
                state = self.update_task_state(task_name, "skipped", readiness.get("summary", f"{task_name} is not ready yet."), readiness)

        due_tasks = self.get_due_tasks(
            state,
            local_interval_hours=intervals[self.TASK_LOCAL],
            notion_interval_hours=intervals[self.TASK_NOTION],
            clickup_interval_hours=intervals[self.TASK_CLICKUP],
            wordpress_interval_hours=intervals[self.TASK_WORDPRESS],
            internal_interval_hours=intervals[self.TASK_INTERNAL],
            now=now,
        )
        if not due_tasks:
            snapshot = self.get_status_snapshot()
            if not self.is_within_maintenance_window(now=now):
                wake_note = " If the machine was asleep, it can catch up after wake." if self.should_catch_up_after_wake() else ""
                summary = f"Scheduled clean-up is waiting for the night window ({self.describe_maintenance_window()}).{wake_note}"
                return True, summary, {"ran_any": False, "tasks": [], "state": snapshot, "window": self.describe_maintenance_window()}
            return True, self.format_status_summary(snapshot), {"ran_any": False, "tasks": [], "state": snapshot}

        return self._run_tasks(due_tasks)

    def run_all_maintenance(self) -> tuple[bool, str, dict]:
        tasks = [self.TASK_LOCAL, self.TASK_NOTION, self.TASK_CLICKUP, self.TASK_WORDPRESS, self.TASK_INTERNAL]
        return self._run_tasks(tasks)

    def run_manual_maintenance(self, job_name: str | None = None) -> tuple[bool, str, dict]:
        normalized = self._normalize_name(job_name)
        if not normalized or normalized in {"all", "full", "run full maintenance", "run_full_maintenance"}:
            return self.run_all_maintenance()

        if normalized == "orphaned_media_cleanup":
            return True, "Orphaned media cleanup has been removed for safety and will not run.", {
                "ran_any": False,
                "tasks": [],
                "manual_job": normalized,
                "removed_job": True,
            }

        task_lookup = self.MANUAL_JOB_TARGETS.get(normalized)
        if not task_lookup:
            return self.run_all_maintenance()
        return self._run_tasks(task_lookup, manual_job=normalized)

