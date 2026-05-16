import html
import json
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from src.core.backup_manager import BackupManager
from src.core.config import LOG_DIR, get_app_display_name, get_claude_mcp_name, get_env, get_secret, logger, save_env_var
from src.core.maintenance import MaintenanceManager
from src.db.database import DatabaseManager
from src.mcp.configurator import configure_claude_mcp
from src.sync.engine import ClickUpSyncEngine, N8nSyncEngine, SyncEngine
from src.ui.database_browser import DatabaseBrowserWindow
from src.ui.main_window import MainWindow
from src.ui.time_machine import TimeMachineWindow


class SyncWorker(QThread):
    finished = Signal(str, bool, str)

    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode

    def run(self):
        worker_db = DatabaseManager()
        if not worker_db.connect():
            summary = f"Could not reach the local database: {worker_db.last_error or 'unknown error'}"
            self.finished.emit(self.mode, False, summary)
            return

        try:
            engine = SyncEngine(worker_db)
            if self.mode == "pull":
                success = engine.pull_from_notion()
            elif self.mode == "push":
                success = engine.push_to_notion()
            else:
                success = engine.perform_sync()
            self.finished.emit(self.mode, success, engine.last_summary)
        finally:
            worker_db.close()


class MaintenanceWorker(QThread):
    finished = Signal(bool, str, dict)

    def __init__(self, mode: str = "due", job_name: str | None = None):
        super().__init__()
        self.mode = mode
        self.job_name = job_name

    def run(self):
        worker_db = DatabaseManager()
        if not worker_db.connect():
            summary = f"Could not reach the local database: {worker_db.last_error or 'unknown error'}"
            self.finished.emit(False, summary, {"ran_any": False, "error": worker_db.last_error or ""})
            return

        try:
            maintenance_manager = MaintenanceManager(worker_db)
            if self.mode == "manual":
                success, summary, details = maintenance_manager.run_manual_maintenance(self.job_name)
            else:
                success, summary, details = maintenance_manager.run_due_maintenance()
            self.finished.emit(success, summary, details)
        finally:
            worker_db.close()


class N8nSyncWorker(QThread):
    finished = Signal(bool, str)

    def run(self):
        worker_db = DatabaseManager()
        if not worker_db.connect():
            self.finished.emit(False, f"Could not reach the local database: {worker_db.last_error or 'unknown error'}")
            return

        try:
            engine = N8nSyncEngine(worker_db)
            success = engine.sync_workflows()
            self.finished.emit(success, engine.last_summary)
        finally:
            worker_db.close()


class ClickUpSyncWorker(QThread):
    finished = Signal(bool, str)

    def run(self):
        worker_db = DatabaseManager()
        if not worker_db.connect():
            self.finished.emit(False, f"Could not reach the local database: {worker_db.last_error or 'unknown error'}")
            return

        try:
            engine = ClickUpSyncEngine(worker_db)
            success = engine.run_sync()
            self.finished.emit(success, engine.last_summary)
        finally:
            worker_db.close()


class ConnectionResyncWorker(QThread):
    finished = Signal(bool, str, dict)

    def __init__(self, service: str = "all", parent=None):
        super().__init__(parent)
        normalized = str(service or "all").strip().lower() or "all"
        self.service = normalized if normalized in {"all", "notion", "clickup"} else "all"

    def run(self):
        worker_db = DatabaseManager()
        if not worker_db.connect():
            summary = f"Could not reach the local database: {worker_db.last_error or 'unknown error'}"
            self.finished.emit(False, summary, {"error": worker_db.last_error or "", "service": self.service})
            return

        details = {
            "service": self.service,
            "notion": {"status": "skipped", "databases": [], "linked_missing": [], "summary": "Notion not checked yet."},
            "clickup": {"status": "skipped", "lists": [], "summary": "ClickUp not checked yet."},
        }
        message_parts: list[str] = []
        any_configured = False
        had_error = False

        try:
            if self.service in {"all", "notion"}:
                notion_engine = SyncEngine(worker_db)
                if notion_engine.notion:
                    any_configured = True
                    save_env_var("NOTION_DB_ID", "ALL")
                    notion_engine.sync_all_databases = True
                    notion_engine.db_ids = []

                    try:
                        databases = notion_engine.discover_accessible_databases()
                        linked_missing = notion_engine.scan_for_unshared_database_references(databases)
                        notion_success = notion_engine.pull_from_notion() if databases else True
                        notion_summary = (
                            notion_engine.last_summary
                            if databases
                            else "No shared Notion databases are visible yet. Share a database with the integration to bring it in automatically."
                        )
                        details["notion"] = {
                            "status": "success" if notion_success else "error",
                            "databases": list(databases or []),
                            "linked_missing": list(linked_missing or []),
                            "summary": notion_summary,
                        }
                        if not notion_success:
                            had_error = True
                            message_parts.append("Notion hit a refresh issue.")
                        else:
                            extra_note = f"; {len(linked_missing)} linked database(s) still need sharing" if linked_missing else ""
                            message_parts.append(f"Notion now tracks all visible databases ({len(databases)} found{extra_note}).")
                    except Exception as exc:
                        had_error = True
                        details["notion"] = {
                            "status": "error",
                            "databases": [],
                            "linked_missing": [],
                            "summary": f"Notion resync failed: {exc}",
                        }
                        logger.error(f"One-click Notion resync failed: {exc}")
                        message_parts.append("Notion could not refresh right now.")
                else:
                    details["notion"] = {
                        "status": "skipped",
                        "databases": [],
                        "linked_missing": [],
                        "summary": "Notion skipped — add your Notion token in Setup first.",
                    }
                    if self.service == "notion":
                        message_parts.append("Notion skipped — no token saved yet.")

            if self.service in {"all", "clickup"}:
                clickup_engine = ClickUpSyncEngine(worker_db)
                if clickup_engine.api_token:
                    any_configured = True
                    save_env_var("CLICKUP_LIST_IDS", "ALL")
                    clickup_engine.sync_all_lists = True
                    clickup_engine.list_ids = []

                    try:
                        lists = clickup_engine.discover_accessible_lists()
                        team_ids = {
                            str(item.get("team_id") or "").strip()
                            for item in lists
                            if str(item.get("team_id") or "").strip()
                        }
                        if len(team_ids) == 1:
                            clickup_engine.team_id = next(iter(team_ids))
                            save_env_var("CLICKUP_TEAM_ID", clickup_engine.team_id)

                        clickup_success = clickup_engine.run_sync() if lists else True
                        clickup_summary = (
                            clickup_engine.last_summary
                            if lists
                            else "No accessible ClickUp lists are visible yet. Once the token can see more lists, they will be added automatically."
                        )
                        details["clickup"] = {
                            "status": "success" if clickup_success else "error",
                            "lists": list(lists or []),
                            "summary": clickup_summary,
                        }
                        if not clickup_success:
                            had_error = True
                            message_parts.append("ClickUp hit a refresh issue.")
                        else:
                            message_parts.append(f"ClickUp now tracks all visible lists ({len(lists)} found).")
                    except Exception as exc:
                        had_error = True
                        details["clickup"] = {
                            "status": "error",
                            "lists": [],
                            "summary": f"ClickUp resync failed: {exc}",
                        }
                        logger.error(f"One-click ClickUp resync failed: {exc}")
                        message_parts.append("ClickUp could not refresh right now.")
                else:
                    details["clickup"] = {
                        "status": "skipped",
                        "lists": [],
                        "summary": "ClickUp skipped — add your ClickUp token in Setup first.",
                    }
                    if self.service == "clickup":
                        message_parts.append("ClickUp skipped — no token saved yet.")

            if not any_configured:
                if self.service == "notion":
                    summary = "Notion is not ready to resync yet. Add your Notion token in Setup first."
                elif self.service == "clickup":
                    summary = "ClickUp is not ready to resync yet. Add your ClickUp token in Setup first."
                else:
                    summary = "Nothing to resync yet. Add your Notion or ClickUp token in Setup first."
                self.finished.emit(False, summary, details)
                return

            if self.service == "notion":
                summary_prefix = "Notion resync complete." if not had_error else "Notion resync finished with an issue."
            elif self.service == "clickup":
                summary_prefix = "ClickUp resync complete." if not had_error else "ClickUp resync finished with an issue."
            else:
                summary_prefix = "Resync complete." if not had_error else "Resync finished with an issue."

            summary = f"{summary_prefix} {' '.join(part for part in message_parts if part).strip()}".strip()
            self.finished.emit(not had_error, summary, details)
        finally:
            worker_db.close()


class TrayApp(QSystemTrayIcon):
    def __init__(self, icon, db_manager, wizard_class, parent=None):
        super().__init__(icon, parent)
        self.db = db_manager
        self.wizard_class = wizard_class
        self.worker = None
        self.n8n_worker = None
        self.clickup_worker = None
        self.maintenance_worker = None
        self.connection_resync_worker = None
        self.connection_resync_service = "all"
        self.clickup_webhook_server = None
        self.clickup_webhook_status = "ClickUp webhook listener has not started yet."
        self.wizard = None
        self.dashboard = None
        self.time_machine_window = None
        self.database_browser = None
        self.database_browser_window = None
        self.last_summary = "Ready"
        self.notion_access_summary = "Not checked yet. Click 'Check Notion Access' to see which databases are currently shared with the integration."
        self.notion_last_databases: list[dict] = []
        self.notion_last_unlinked: list[dict] = []
        self.backup_manager = BackupManager(self.db)
        self.maintenance_manager = MaintenanceManager(self.db)
        self.backup_summary = "No backups have run yet."
        self.health_summary = "Health check not run yet."
        self.job_history: list[dict] = []
        self._status_light = "ready"
        self._status_detail = self.last_summary
        self._status_flash_on = True
        self.last_sync_name = None
        self.last_sync_at = None
        self.setToolTip(get_app_display_name())

        self.status_flash_timer = QTimer(self)
        self.status_flash_timer.setInterval(500)
        self.status_flash_timer.timeout.connect(self._toggle_status_flash)

        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(lambda: self.run_sync("sync"))

        self.maintenance_timer = QTimer(self)
        self.maintenance_timer.timeout.connect(self.run_due_maintenance)

        self.menu = QMenu()

        self.dashboard_action = QAction("Open Dashboard", self)
        self.dashboard_action.triggered.connect(self.show_dashboard)
        self.menu.addAction(self.dashboard_action)

        self.db_diagnostics_action = QAction("Check DB Connections", self)
        self.db_diagnostics_action.triggered.connect(self.show_db_connection_diagnostics)
        self.menu.addAction(self.db_diagnostics_action)

        self.browser_action = QAction("Open Database Browser", self)
        self.browser_action.triggered.connect(self.show_database_browser)

        self.time_machine_action = QAction("Open Time Machine", self)
        self.time_machine_action.triggered.connect(self.show_time_machine)

        self.sync_action = QAction("Run Smart Sync now", self)
        self.sync_action.triggered.connect(lambda: self.run_sync("sync"))

        self.advanced_menu = QMenu("Advanced tools")

        self.resync_connections_action = QAction("Resync all connections", self)
        self.resync_connections_action.triggered.connect(lambda: self.run_connection_resync("all"))
        self.advanced_menu.addAction(self.resync_connections_action)

        self.pull_action = QAction("Import from Notion only…", self)
        self.pull_action.triggered.connect(lambda: self.run_sync("pull"))
        self.advanced_menu.addAction(self.pull_action)

        self.push_action = QAction("Push local changes only…", self)
        self.push_action.triggered.connect(lambda: self.run_sync("push"))
        self.advanced_menu.addAction(self.push_action)

        self.maintenance_action = QAction("Run full maintenance now", self)
        self.maintenance_action.triggered.connect(lambda: self.run_manual_maintenance())
        self.advanced_menu.addSeparator()
        self.advanced_menu.addAction(self.maintenance_action)

        self.n8n_sync_action = QAction("Sync n8n now", self)
        self.n8n_sync_action.triggered.connect(self.run_n8n_sync)

        self.clickup_sync_action = QAction("Sync ClickUp now", self)
        self.clickup_sync_action.triggered.connect(self.run_clickup_sync)

        self.wordpress_cleanup_action = QAction("Run WordPress clean-up", self)
        self.wordpress_cleanup_action.triggered.connect(lambda: self.run_manual_maintenance("full_wordpress_dedup"))

        self.clickup_webhook_action = QAction("Copy ClickUp webhook details", self)
        self.clickup_webhook_action.triggered.connect(self.copy_clickup_webhook_details)

        self.queue_edit_action = QAction("Queue Local Edit", self)
        self.queue_edit_action.triggered.connect(self.open_local_edit_dialog)

        self.stats_action = QAction("Local mirror: 0 page(s) | Pending push: 0", self)
        self.stats_action.setEnabled(False)

        self.last_sync_action = QAction("Last sync: Never", self)
        self.last_sync_action.setEnabled(False)

        self.interval_action = QAction("Auto-sync: Off", self)
        self.interval_action.setEnabled(False)

        self.check_access_action = QAction("Check Notion Access", self)
        self.check_access_action.triggered.connect(self.check_notion_access)

        self.configure_mcp_action = QAction("Configure Claude MCP", self)
        self.configure_mcp_action.triggered.connect(self.configure_mcp)

        self.settings_action = QAction("Open Setup", self)
        self.settings_action.triggered.connect(self.show_wizard)

        self.logs_action = QAction("Open Logs", self)
        self.logs_action.triggered.connect(self.open_logs)

        # Keep the tray menu intentionally simple: the dashboard is the place for
        # all tools and status details, so the tray itself only offers the two
        # quick actions users asked for.

        self.quit_action = QAction("Exit", self)
        self.quit_action.triggered.connect(self.quit_app)
        self.menu.addAction(self.quit_action)

        self.setContextMenu(self.menu)
        self.activated.connect(self.on_tray_activated)
        # NOTE: Do NOT call refresh_schedule() or ensure_clickup_webhook_listener()
        # here. Those hit the database. They are called later via on_db_ready().

    def on_db_ready(self, connected: bool, error: str = ""):
        """Called from main.py after the background DB connection finishes."""
        if not connected:
            self.set_status_light("error", f"Database not connected: {error or 'unknown'}")
            return

        # Arm background timers as soon as the DB is ready so auto-sync and
        # maintenance polling do not wait for a later manual action.
        self.refresh_schedule()
        self.ensure_clickup_webhook_listener(show_message=False)

        self.set_status_light("ready", "Database connected")
        # Refresh dashboard (pages load one-at-a-time via QTimer)
        if self.dashboard and hasattr(self.dashboard, "refresh_view"):
            QTimer.singleShot(0, self.dashboard.refresh_view)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_dashboard()
            return

        if reason == QSystemTrayIcon.Trigger:
            menu = self.contextMenu()
            if menu is not None:
                menu.popup(QCursor.pos())

    def _build_status_icon(self, state: str, flash_on: bool = True) -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#0f172a"), 2))
        painter.setBrush(QColor("#1f2937"))
        painter.drawRoundedRect(18, 4, 28, 56, 10, 10)

        lights = {
            "red": (32, 15, QColor("#ef4444")),
            "amber": (32, 32, QColor("#f59e0b")),
            "green": (32, 49, QColor("#22c55e")),
        }
        active_light = {"ready": "green", "syncing": "amber", "error": "red"}.get(state, "green")

        for light_name, (center_x, center_y, color) in lights.items():
            is_active = light_name == active_light and (flash_on or active_light != "amber")
            fill = QColor(color if is_active else "#475569")
            fill.setAlpha(255 if is_active else 110)
            painter.setPen(QPen(QColor("#0b1120"), 1))
            painter.setBrush(fill)
            painter.drawEllipse(center_x - 8, center_y - 8, 16, 16)

        painter.end()
        return QIcon(pixmap)

    def _apply_status_icon(self):
        self.setIcon(self._build_status_icon(self._status_light, self._status_flash_on))
        label = {
            "ready": "Green: working",
            "syncing": "Amber: synchronising",
            "error": "Red: stopped or fault",
        }.get(self._status_light, "Green: working")
        detail = " ".join(str(self._status_detail or self.last_summary or "Ready").split())
        if len(detail) > 100:
            detail = f"{detail[:97]}..."
        self.setToolTip(f"{get_app_display_name()} | {label} | {detail}")

    def _toggle_status_flash(self):
        self._status_flash_on = not self._status_flash_on
        self._apply_status_icon()

    def set_status_light(self, state: str, detail: str | None = None):
        normalized_state = state if state in {"ready", "syncing", "error"} else "ready"
        self._status_light = normalized_state
        if detail:
            self._status_detail = detail

        if normalized_state == "syncing":
            self._status_flash_on = True
            if not self.status_flash_timer.isActive():
                self.status_flash_timer.start()
        else:
            if self.status_flash_timer.isActive():
                self.status_flash_timer.stop()
            self._status_flash_on = True

        self._apply_status_icon()

    def showMessage(self, title, message, icon=QSystemTrayIcon.Information, msecs=10000, force_popup: bool = False):
        title_text = str(title or "").strip().lower()
        message_text = str(message or "").strip()
        message_lower = message_text.lower()

        if icon in (QSystemTrayIcon.Critical, QSystemTrayIcon.Warning) or any(
            keyword in title_text for keyword in ("error", "failed", "issue", "setup")
        ):
            state = "error"
        elif any(keyword in title_text for keyword in ("working", "sync in progress", "maintenance in progress")) or any(
            keyword in message_lower for keyword in ("running two-way sync", "importing updates", "pushing pending local changes", "running scheduled clean-up")
        ):
            state = "syncing"
        else:
            state = "ready"

        self.set_status_light(state, message_text or str(title or "Ready"))

        if force_popup:
            super().showMessage(title, message, icon, msecs)

    def has_background_activity(self) -> bool:
        return any(
            worker and worker.isRunning()
            for worker in (
                self.worker,
                self.n8n_worker,
                self.clickup_worker,
                self.maintenance_worker,
                self.connection_resync_worker,
            )
        )

    def get_runtime_stats(self, force_refresh: bool = False) -> dict:
        if not self.db:
            return {}

        background_busy = any(
            worker and worker.isRunning()
            for worker in (
                getattr(self, "worker", None),
                getattr(self, "n8n_worker", None),
                getattr(self, "clickup_worker", None),
                getattr(self, "maintenance_worker", None),
            )
        )
        if background_busy and not force_refresh and hasattr(self.db, "get_cached_stats_snapshot"):
            return self.db.get_cached_stats_snapshot()

        return self.db.get_stats(force_refresh=force_refresh, max_age_seconds=15.0)

    def get_catalog_overview(self, force_refresh: bool = False) -> dict:
        if not self.db:
            return {}

        background_busy = any(
            worker and worker.isRunning()
            for worker in (
                getattr(self, "worker", None),
                getattr(self, "n8n_worker", None),
                getattr(self, "clickup_worker", None),
                getattr(self, "maintenance_worker", None),
            )
        )
        if background_busy and not force_refresh and hasattr(self.db, "get_cached_catalog_stats_snapshot"):
            return self.db.get_cached_catalog_stats_snapshot()

        return self.db.get_catalog_stats(force_refresh=force_refresh, max_age_seconds=15.0)

    @staticmethod
    def _coerce_timestamp(value):
        if not value:
            return None

        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if not text:
                return None
            normalized = text.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                parsed = None
                for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return None

        if parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    @staticmethod
    def _format_relative_time(when: datetime, now: datetime | None = None) -> str:
        current = now or datetime.now()
        if current.tzinfo:
            current = current.astimezone().replace(tzinfo=None)

        delta_seconds = max(0, int((current - when).total_seconds()))
        if delta_seconds < 60:
            return "just now"

        minutes = delta_seconds // 60
        if minutes < 60:
            unit = "minute" if minutes == 1 else "minutes"
            return f"{minutes} {unit} ago"

        hours = delta_seconds // 3600
        if hours < 24:
            unit = "hour" if hours == 1 else "hours"
            return f"{hours} {unit} ago"

        days = delta_seconds // 86400
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} ago"

    @classmethod
    def build_last_sync_status_text(
        cls,
        stats: dict | None = None,
        activity_name: str | None = None,
        activity_at=None,
        now: datetime | None = None,
    ) -> str:
        candidates = []

        session_time = cls._coerce_timestamp(activity_at)
        if session_time is not None:
            candidates.append((activity_name or "Notion sync", session_time))

        stats = stats or {}
        candidates.extend(
            (
                "Notion sync",
                cls._coerce_timestamp(stats.get(key)),
            )
            for key in ("last_pull_at", "last_push_at")
        )

        available = [(label, value) for label, value in candidates if value is not None]
        if not available:
            return "Last sync: Never"

        latest_label, latest_time = max(available, key=lambda item: item[1])
        timestamp_text = latest_time.strftime("%Y-%m-%d %H:%M:%S")
        elapsed_text = cls._format_relative_time(latest_time, now=now)
        return f"Last sync: {latest_label} @ {timestamp_text} ({elapsed_text})"

    def get_last_sync_status_text(self, force_refresh: bool = False) -> str:
        stats = self.get_runtime_stats(force_refresh=force_refresh) if self.db else {}
        text = self.build_last_sync_status_text(
            stats=stats,
            activity_name=getattr(self, "last_sync_name", None),
            activity_at=getattr(self, "last_sync_at", None),
        )
        self.last_sync_action.setText(text)
        return text

    @staticmethod
    def build_confirmation_details(mode: str, pending_push: int = 0) -> tuple[str, str]:
        pending_push = max(0, int(pending_push or 0))

        if mode == "pull":
            title = "Import from Notion only"
            message = (
                "This is a one-way import from Notion into the local mirror. "
                "It refreshes your local mirror without pushing local changes first. "
                f"You currently have {pending_push} local edit(s) waiting to push."
            )
            return title, message

        if mode == "push":
            title = "Push local changes only"
            message = (
                "This is a one-way push from the local mirror back to Notion. "
                f"You currently have {pending_push} queued local change(s) ready to send."
            )
            return title, message

        return (
            "Run Smart Sync now",
            "This compares Notion and your local mirror, then applies the safest updates in both directions.",
        )

    @staticmethod
    def build_notion_access_report(
        databases: list[dict],
        configured_target: str,
        referenced_missing: list[dict] | None = None,
    ) -> dict:
        visible_items = [
            {
                "id": str((item or {}).get("id", "")).strip(),
                "title": ((item or {}).get("title") or "Untitled").strip() or "Untitled",
            }
            for item in (databases or [])
        ]
        visible_by_id = {item["id"]: item["title"] for item in visible_items if item["id"]}
        referenced_missing = list(referenced_missing or [])

        configured_text = (configured_target or "").strip()
        if configured_text.upper() == "ALL":
            configured_ids = []
        else:
            configured_ids = [part.strip() for part in configured_text.split(",") if part.strip() and part.strip() != "Not configured"]

        connected_ids = [item_id for item_id in configured_ids if item_id in visible_by_id]
        missing_ids = [item_id for item_id in configured_ids if item_id not in visible_by_id]

        visible_html = "".join(
            f"<li><b>{html.escape(item['title'])}</b> (<code>{html.escape(item['id'])}</code>)</li>"
            for item in visible_items
        ) or "<li>No shared databases are visible yet.</li>"

        missing_html = "".join(f"<li><code>{html.escape(item_id)}</code></li>" for item_id in missing_ids)
        if not missing_html:
            missing_html = "<li>Nothing is missing from the current target list.</li>"

        referenced_html = "".join(
            "<li>"
            f"<code>{html.escape(str(item.get('id', '')))}</code>"
            f" — referenced by <b>{html.escape(item.get('source_title', 'Untitled database'))}</b>"
            f" via <b>{html.escape(item.get('property_name', 'unknown property'))}</b>"
            "</li>"
            for item in referenced_missing
        )
        if not referenced_html:
            referenced_html = "<li>No linked-but-unshared database references were found in the shared schemas that were scanned.</li>"

        if configured_ids:
            summary = f"{len(connected_ids)} connected target(s) visible; {len(missing_ids)} still need sharing."
        else:
            summary = f"{len(visible_items)} shared database(s) currently visible to the integration."
        if referenced_missing:
            summary += f" Found {len(referenced_missing)} linked database reference(s) that still look unshared."

        details_html = (
            "<b>Shared with the integration right now</b>"
            f"<ul>{visible_html}</ul>"
            "<b>Still missing from the current target list</b>"
            f"<ul>{missing_html}</ul>"
            "<b>Referenced by connected databases but not currently shared</b>"
            f"<ul>{referenced_html}</ul>"
        )
        details_text = (
            "Visible now:\n"
            + "\n".join(f"- {item['title']} ({item['id']})" for item in visible_items)
            + "\n\nMissing from target:\n"
            + ("\n".join(f"- {item_id}" for item_id in missing_ids) or "- Nothing missing")
            + "\n\nReferenced but not currently shared:\n"
            + (
                "\n".join(
                    f"- {item.get('id', '')} referenced by {item.get('source_title', 'Untitled database')} via {item.get('property_name', 'unknown property')}"
                    for item in referenced_missing
                )
                or "- No linked-but-unshared references found"
            )
        )

        return {
            "summary": summary,
            "details_html": details_html,
            "details_text": details_text,
            "has_warning": bool(missing_ids or referenced_missing),
        }

    def refresh_schedule(self):
        try:
            interval_minutes = max(0, int(get_env("SYNC_INTERVAL_MINUTES", "2") or "2"))
        except ValueError:
            interval_minutes = 2

        if interval_minutes > 0:
            self.auto_timer.start(interval_minutes * 60 * 1000)
            self.interval_action.setText(f"Auto-sync: Every {interval_minutes} minute(s)")
        else:
            self.auto_timer.stop()
            self.interval_action.setText("Auto-sync: Off")

        try:
            maintenance_poll_minutes = max(0, int(get_env("MAINTENANCE_POLL_MINUTES", "15") or "15"))
        except ValueError:
            maintenance_poll_minutes = 15

        if maintenance_poll_minutes > 0:
            self.maintenance_timer.start(maintenance_poll_minutes * 60 * 1000)
        else:
            self.maintenance_timer.stop()

        # NOTE: ensure_clickup_webhook_listener is called separately
        # from on_db_ready, not here, to avoid blocking this method.
        try:
            stats = self.get_runtime_stats()
        except Exception:
            stats = {}
        if stats.get("connected"):
            self.stats_action.setText(
                f"Local mirror: {stats.get('active_pages', 0)} page(s) | Pending push: {stats.get('pending_push', 0)}"
            )
        else:
            self.stats_action.setText("Local mirror: not connected yet")
        self.last_sync_action.setText(
            self.build_last_sync_status_text(
                stats=stats,
                activity_name=self.last_sync_name,
                activity_at=self.last_sync_at,
            )
        )

        if self.dashboard:
            if hasattr(self.dashboard, "apply_branding"):
                self.dashboard.apply_branding()
            # Do NOT call dashboard.refresh_status() here — it triggers
            # refresh_view() which re-enters this path.  The dashboard
            # is refreshed separately from on_db_ready.

        if not stats.get("connected"):
            self.set_status_light("error", "Database setup is not finished yet.")
        elif not ((self.worker and self.worker.isRunning()) or (self.maintenance_worker and self.maintenance_worker.isRunning())):
            if self._status_light != "error" or (self.last_summary or "").strip().lower() in {"ready", "working"}:
                self.set_status_light("ready", self.last_summary or "Ready")

    def show_dashboard(self):
        if not self.dashboard:
            self.dashboard = MainWindow(self)
            self.dashboard.destroyed.connect(lambda *_: setattr(self, "dashboard", None))

        if self.dashboard.isMinimized():
            self.dashboard.showNormal()
        else:
            self.dashboard.show()

        self.dashboard.raise_()
        self.dashboard.activateWindow()
        if hasattr(self.dashboard, "refresh_view"):
            QTimer.singleShot(0, self.dashboard.refresh_view)

    def show_database_browser(self):
        self.show_dashboard()
        if self.dashboard and hasattr(self.dashboard, "open_database_browser_page"):
            self.dashboard.open_database_browser_page()
            return

        if not self.database_browser_window:
            self.database_browser_window = DatabaseBrowserWindow(self)
            self.database_browser_window.destroyed.connect(lambda *_: setattr(self, "database_browser_window", None))

        if self.database_browser_window.isMinimized():
            self.database_browser_window.showNormal()
        else:
            self.database_browser_window.show()

        self.database_browser_window.raise_()
        self.database_browser_window.activateWindow()
        self.database_browser_window.refresh_entries()

    def show_time_machine(self):
        self.show_dashboard()
        if self.dashboard and hasattr(self.dashboard, "navigate_to_page") and self.dashboard.navigate_to_page("Time Machine"):
            current_page = self.dashboard.stack.currentWidget() if hasattr(self.dashboard, "stack") else None
            if current_page and hasattr(current_page, "refresh_activity_log"):
                current_page.refresh_activity_log()
            return

        if not self.time_machine_window:
            self.time_machine_window = TimeMachineWindow(self)
            self.time_machine_window.destroyed.connect(lambda *_: setattr(self, "time_machine_window", None))

        if self.time_machine_window.isMinimized():
            self.time_machine_window.showNormal()
        else:
            self.time_machine_window.show()

        self.time_machine_window.raise_()
        self.time_machine_window.activateWindow()
        self.time_machine_window.refresh_activity_log()

    def show_wizard(self, service: str | None = None):
        self.show_dashboard()
        if self.dashboard and hasattr(self.dashboard, "show_setup_page"):
            self.dashboard.show_setup_page(service)
            self.showMessage(
                "Setup",
                "The setup steps are now open inside the main dashboard window.",
                QSystemTrayIcon.Information,
                2500,
            )
            return

        if not self.wizard:
            try:
                self.wizard = self.wizard_class(self.db, self.on_wizard_complete, tray_app=self, service=service)
            except TypeError:
                self.wizard = self.wizard_class(self.db, self.on_wizard_complete)
            self.wizard.destroyed.connect(lambda *_: setattr(self, "wizard", None))
        else:
            if service and hasattr(self.wizard, "_open_service"):
                self.wizard._open_service(service)
            elif hasattr(self.wizard, "_go_to_picker"):
                self.wizard._go_to_picker()

        if self.wizard.isMinimized():
            self.wizard.showNormal()
        else:
            self.wizard.show()

        self.wizard.setWindowState((self.wizard.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.wizard.raise_()
        self.wizard.activateWindow()
        QTimer.singleShot(150, self._focus_wizard)
        self.showMessage(
            "Setup",
            "No terminal input is needed. Use the setup window that just opened.",
            QSystemTrayIcon.Information,
            3500,
        )

    def _focus_wizard(self):
        if not self.wizard:
            return
        self.wizard.show()
        self.wizard.raise_()
        self.wizard.activateWindow()

    def open_logs(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    def show_db_connection_diagnostics(self):
        if not self.db or not hasattr(self.db, "get_db_connection_diagnostics"):
            self.showMessage(
                "Database diagnostics",
                "Database diagnostics are not available yet.",
                QSystemTrayIcon.Warning,
                3000,
            )
            return

        diagnostics = self.db.get_db_connection_diagnostics()
        if not diagnostics.get("connected"):
            error_text = str(diagnostics.get("error") or "Database is not connected.")
            QMessageBox.warning(
                self.dashboard if self.dashboard and self.dashboard.isVisible() else None,
                "Database Connection Diagnostics",
                f"Could not read diagnostics: {error_text}",
            )
            return

        total = int(diagnostics.get("total_connections", 0) or 0)
        usable = int(diagnostics.get("usable_connections", 0) or 0)
        usage_percent = int(diagnostics.get("usage_percent", 0) or 0)
        high_usage = bool(diagnostics.get("high_usage"))

        message = (
            f"Database: {diagnostics.get('database_name') or 'unknown'}\n"
            f"Total connections: {total}\n"
            f"Usable connections: {usable} (max minus reserved)\n"
            f"Current DB connections: {int(diagnostics.get('current_db_connections', 0) or 0)}\n"
            f"Active connections: {int(diagnostics.get('active_connections', 0) or 0)}\n"
            f"Idle connections: {int(diagnostics.get('idle_connections', 0) or 0)}\n"
            f"Usage: {usage_percent}%"
        )

        if high_usage:
            message += "\n\nWarning: connection usage is high. Consider closing idle app or MCP clients."
            icon = QMessageBox.Warning
            tray_icon = QSystemTrayIcon.Warning
        else:
            icon = QMessageBox.Information
            tray_icon = QSystemTrayIcon.Information

        dialog = QMessageBox(self.dashboard if self.dashboard and self.dashboard.isVisible() else None)
        dialog.setIcon(icon)
        dialog.setWindowTitle("Database Connection Diagnostics")
        dialog.setText(message)
        dialog.addButton(QMessageBox.Ok)
        dialog.exec()

        self.showMessage(
            "Database diagnostics",
            f"PostgreSQL usage is {usage_percent}% ({total}/{usable if usable > 0 else 'n/a'} usable connections).",
            tray_icon,
            3200,
        )

    def get_notion_access_summary(self) -> str:
        return self.notion_access_summary

    def get_notion_database_cache(self) -> tuple[list[dict], list[dict]]:
        """Return (connected_databases, unlinked_referenced_databases) from the last access check."""
        return list(self.notion_last_databases), list(self.notion_last_unlinked)

    def get_backup_summary_text(self) -> str:
        try:
            status = self.backup_manager.get_status(attempt_connect=False)
            summary = (status.get("last_backup_text") or "").strip()
            self.backup_summary = summary or "No backups have run yet."
        except Exception as e:
            logger.error(f"Failed to read backup status: {e}")
            self.backup_summary = "Backup status not available yet."
        return self.backup_summary

    def get_activity_log(self, limit: int = 120) -> list[dict]:
        if self.db and hasattr(self.db, "get_activity_log"):
            return self.db.get_activity_log(limit=limit)
        return []

    def set_activity_checked(self, entry_id: int, checked: bool = True) -> bool:
        if self.db and hasattr(self.db, "set_activity_checked"):
            return bool(self.db.set_activity_checked(entry_id, checked=checked))
        return False

    def _record_job_result(self, label: str, success: bool, summary: str):
        cleaned_summary = " ".join(str(summary or "").split())
        if not cleaned_summary:
            cleaned_summary = "Completed successfully." if success else "Needs attention."

        self.job_history.insert(
            0,
            {
                "label": str(label or "Task").strip() or "Task",
                "success": bool(success),
                "status": "Success" if success else "Needs attention",
                "summary": cleaned_summary,
                "finished_at": datetime.now(),
            },
        )
        self.job_history = self.job_history[:25]

    def get_recent_jobs(self, limit: int = 8) -> list[dict]:
        try:
            safe_limit = max(1, int(limit or 8))
        except (TypeError, ValueError):
            safe_limit = 8

        results: list[dict] = []
        if self.job_history:
            for entry in self.job_history[:safe_limit]:
                finished_at = entry.get("finished_at")
                when_text = self._format_relative_time(finished_at) if isinstance(finished_at, datetime) else "recently"
                results.append({**entry, "when_text": when_text})
            return results

        for entry in self.get_activity_log(limit=safe_limit):
            finished_at = self._coerce_timestamp(entry.get("created_at"))
            when_text = self._format_relative_time(finished_at) if finished_at else "recently"
            status_text = str(entry.get("status") or "info").strip().lower()
            results.append(
                {
                    "label": str(entry.get("action_label") or entry.get("action") or "Activity").strip() or "Activity",
                    "success": status_text not in {"error", "failed", "warning"},
                    "status": "Needs attention" if status_text in {"error", "failed", "warning"} else "Success",
                    "summary": str(entry.get("summary") or "Activity recorded.").strip() or "Activity recorded.",
                    "finished_at": finished_at,
                    "when_text": when_text,
                }
            )
        return results

    def get_setup_progress(self) -> dict:
        stats = self.get_runtime_stats()
        mcp_status = self.get_mcp_status_text()
        db_connected = bool(stats.get("connected"))
        db_saved = bool(
            str(get_env("PG_HOST", "")).strip()
            and str(get_env("PG_DBNAME", "")).strip()
            and str(get_env("PG_USER", "")).strip()
            and str(get_secret("PG_PASSWORD", "")).strip()
        )

        items = [
            {
                "label": "Local database",
                "ready": db_connected or db_saved,
                "detail": (
                    "Ready to store and search your local mirror."
                    if db_connected
                    else (
                        "Saved, but run Save & test connection once more to confirm PostgreSQL is reachable."
                        if db_saved
                        else "Finish the PostgreSQL connection in Setup."
                    )
                ),
                "service": "database",
            },
            {
                "label": "Notion",
                "ready": bool(get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED") and get_env("NOTION_DB_ID")),
                "detail": (
                    "Connected and ready to mirror your workspace."
                    if bool(get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED") and get_env("NOTION_DB_ID"))
                    else "Add your Notion token and choose which database to mirror."
                ),
                "service": "notion",
            },
            {
                "label": "Claude Desktop",
                "ready": str(mcp_status or "").lower().startswith("configured as"),
                "detail": (
                    "Linked and ready for AI-assisted actions."
                    if str(mcp_status or "").lower().startswith("configured as")
                    else "Refresh the Claude MCP link after setup so AI tools can use the app safely."
                ),
                "service": "notion",
            },
            {
                "label": "Backups",
                "ready": True,
                "detail": "Local backups are available in the background.",
                "service": "backups",
            },
        ]

        completed = sum(1 for item in items if item.get("ready"))
        next_item = next((item for item in items if not item.get("ready")), None)
        total = len(items)

        return {
            "completed": completed,
            "total": total,
            "percent": int((completed / total) * 100) if total else 0,
            "summary": f"{completed} of {total} core steps are ready.",
            "items": items,
            "next_step": (
                next_item.get("detail")
                if next_item
                else "Core setup is ready. You can add optional services any time."
            ),
            "next_service": next_item.get("service") if next_item else None,
        }

    def get_health_snapshot(self) -> dict:
        stats = self.get_runtime_stats()
        setup = self.get_setup_progress()
        pending_push = max(0, int(stats.get("pending_push", 0) or 0))
        sync_text = self.get_last_sync_status_text(force_refresh=False)
        backup_text = self.get_backup_summary_text()
        maintenance_text = (
            self.maintenance_manager.get_status_text()
            if getattr(self, "maintenance_manager", None)
            else "Maintenance status not available yet."
        )
        notion_text = self.get_notion_access_summary()

        if not stats.get("connected"):
            headline = "Setup still needs attention"
            next_action = "Open Setup and finish the local database connection."
        elif pending_push:
            headline = "Healthy, with queued local changes"
            next_action = f"Review and push the {pending_push} queued local change(s) when you are ready."
        else:
            headline = "Everything looks healthy"
            next_action = setup.get("next_step") or "Run a sync whenever you want the freshest data."

        return {
            "headline": headline,
            "summary": sync_text,
            "items": [
                {"label": "Database", "value": "Connected" if stats.get("connected") else "Not connected yet"},
                {"label": "Pending push", "value": f"{pending_push} page(s)"},
                {"label": "Backups", "value": backup_text},
                {"label": "Maintenance", "value": maintenance_text},
                {"label": "Notion access", "value": notion_text},
            ],
            "next_action": next_action,
        }

    def get_clickup_webhook_status_text(self) -> str:
        return self.clickup_webhook_status or "ClickUp webhook listener has not started yet."

    def ensure_clickup_webhook_listener(self, show_message: bool = False) -> tuple[bool, str]:
        if not self.db or not getattr(self.db, "conn", None):
            self.clickup_webhook_status = "ClickUp webhook listener is waiting for the database to connect."
            return False, self.clickup_webhook_status

        engine = ClickUpSyncEngine(self.db)
        config = engine.get_webhook_config()

        if not config.get("enabled"):
            self.clickup_webhook_status = "ClickUp webhook listener is turned off in the local settings."
            return False, self.clickup_webhook_status

        if self.clickup_webhook_server and self.clickup_webhook_server.is_running():
            self.clickup_webhook_status = self.clickup_webhook_server.last_message
            return True, self.clickup_webhook_status

        self.clickup_webhook_server = engine.create_webhook_server(callback=self._handle_clickup_webhook_payload)
        success, message = self.clickup_webhook_server.start()
        self.clickup_webhook_status = message

        if show_message:
            dialog = QMessageBox.information if success else QMessageBox.warning
            dialog(
                self.dashboard if self.dashboard and self.dashboard.isVisible() else None,
                "ClickUp webhook listener",
                message,
            )

        return success, message

    def _handle_clickup_webhook_payload(self, payload) -> tuple[bool, str]:
        worker_db = DatabaseManager()
        if not worker_db.connect():
            message = f"ClickUp webhook update failed: {worker_db.last_error or 'database unavailable'}"
            self.last_summary = message or self.last_summary
            return False, message

        try:
            engine = ClickUpSyncEngine(worker_db)
            success, message = engine.handle_webhook_payload(payload)
        finally:
            worker_db.close()

        if self.db and hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = message or self.last_summary
        if self.clickup_webhook_server and self.clickup_webhook_server.is_running():
            self.clickup_webhook_status = self.clickup_webhook_server.last_message
        return success, message

    def copy_clickup_webhook_details(self):
        self.ensure_clickup_webhook_listener(show_message=False)
        config = ClickUpSyncEngine(self.db).get_webhook_config()
        signature_note = (
            "Configured — send the matching HMAC SHA256 digest in the X-Signature header."
            if config.get("secret")
            else "Not set — the local listener will accept unsigned payloads."
        )
        clipboard_text = (
            f"ClickUp local webhook URL: {config.get('endpoint')}\n"
            f"Local host: {config.get('host')}\n"
            f"Local port: {config.get('port')}\n"
            f"Path: {config.get('path')}\n"
            f"Signature check: {signature_note}\n"
            f"Status: {self.get_clickup_webhook_status_text()}"
        )
        QApplication.clipboard().setText(clipboard_text)
        self.showMessage(
            "Copied",
            "ClickUp webhook details were copied to the clipboard.",
            QSystemTrayIcon.Information,
            2500,
        )

    def preview_bulk_replace(
        self,
        search_text: str,
        replace_text: str = "",
        fields: list[str] | None = None,
        case_sensitive: bool = False,
    ) -> dict:
        default_preview = {
            "search_text": str(search_text or "").strip(),
            "replace_text": str(replace_text or ""),
            "fields": list(fields or ["title", "ai_summary"]),
            "case_sensitive": bool(case_sensitive),
            "affected_pages": 0,
            "total_matches": 0,
            "matches": [],
        }

        if not self.db:
            return default_preview

        if not getattr(self.db, "conn", None) and not self.db.connect():
            return default_preview

        if not hasattr(self.db, "preview_bulk_replace"):
            return default_preview

        return self.db.preview_bulk_replace(
            search_text,
            replace_text,
            fields=fields,
            case_sensitive=case_sensitive,
        )

    def apply_bulk_replace(
        self,
        search_text: str,
        replace_text: str = "",
        fields: list[str] | None = None,
        case_sensitive: bool = False,
    ) -> tuple[bool, str]:
        if not self.db or (not getattr(self.db, "conn", None) and not self.db.connect()):
            return False, "Please finish the database setup first."

        if not hasattr(self.db, "apply_bulk_replace"):
            return False, "Bulk search and replace is not available in this build yet."

        success, message, _details = self.db.apply_bulk_replace(
            search_text,
            replace_text,
            fields=fields,
            case_sensitive=case_sensitive,
        )
        if hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = message or self.last_summary

        if self.dashboard:
            self.dashboard.update_activity_status(self.last_summary)
            self.dashboard.refresh_status()
        if self.database_browser_window and self.database_browser_window.isVisible():
            self.database_browser_window.refresh_entries()

        self.refresh_schedule()
        self._record_job_result("Bulk replace", success, message)
        return success, message

    def run_backup_now(self, silent: bool = True) -> bool:
        success, message = self.backup_manager.create_backup(reason="manual")
        self.backup_summary = message or self.backup_summary
        self.last_summary = message or self.last_summary

        if self.dashboard:
            self.dashboard.update_activity_status(self.last_summary)
            self.dashboard.refresh_status()

        self._record_job_result("Backup", success, message)

        if silent:
            self.showMessage(
                "Backup complete" if success else "Backup failed",
                message,
                QSystemTrayIcon.Information if success else QSystemTrayIcon.Critical,
                3500,
            )
        else:
            dialog = QMessageBox.information if success else QMessageBox.warning
            dialog(
                self.dashboard if self.dashboard and self.dashboard.isVisible() else None,
                "Backup complete" if success else "Backup failed",
                message,
            )

        return success

    def run_health_check(self, show_message: bool = True) -> tuple[bool, str]:
        db_connected = bool(self.db and (getattr(self.db, "conn", None) or self.db.connect()))
        stats = self.get_runtime_stats()
        self.check_notion_access(show_message=False)
        backup_summary = self.get_backup_summary_text()
        maintenance_summary = self.maintenance_manager.get_status_text()

        if db_connected and stats.get("connected"):
            db_summary = (
                f"Database connection looks good. {int(stats.get('active_pages', 0) or 0)} mirrored page(s), "
                f"{int(stats.get('pending_push', 0) or 0)} waiting to push."
            )
        else:
            db_summary = "Database is not connected yet. Open Setup to finish the PostgreSQL connection."

        notion_summary = self.notion_access_summary or "Notion access has not been checked yet."
        message = "\n\n".join(
            [
                db_summary,
                f"Notion access: {notion_summary}",
                f"Backup: {backup_summary}",
                f"Maintenance: {maintenance_summary}",
            ]
        )

        self.health_summary = message
        self.last_summary = "Health check complete."
        overall_ok = db_connected and not notion_summary.lower().startswith("could not check notion access")
        self._record_job_result("Health check", overall_ok, db_summary)

        if self.dashboard:
            self.dashboard.update_activity_status(self.last_summary)
            self.dashboard.refresh_status()
        self.showMessage(
            "Health check complete" if overall_ok else "Health check found an issue",
            db_summary,
            QSystemTrayIcon.Information if overall_ok else QSystemTrayIcon.Warning,
            3500,
        )

        if show_message:
            dialog = QMessageBox.information if overall_ok else QMessageBox.warning
            dialog(
                self.dashboard if self.dashboard and self.dashboard.isVisible() else None,
                "Health check complete" if overall_ok else "Health check found an issue",
                message,
            )

        return overall_ok, message

    def check_notion_access(self, show_message: bool = True):
        engine = SyncEngine(self.db)
        if not engine.notion:
            self.notion_access_summary = "Notion token not configured yet. Open Setup and save your token first."
            if show_message:
                QMessageBox.information(None, "Review Notion Databases", self.notion_access_summary)
            if self.dashboard:
                self.dashboard.refresh_status()
            return []

        try:
            databases = engine.discover_accessible_databases()
            linked_missing = engine.scan_for_unshared_database_references(databases)
            report = self.build_notion_access_report(databases, get_env("NOTION_DB_ID", ""), linked_missing)
            self.notion_access_summary = report["summary"]
            self.notion_last_databases = list(databases or [])
            self.notion_last_unlinked = list(linked_missing or [])

            if show_message:
                dialog = QMessageBox(self.dashboard if self.dashboard and self.dashboard.isVisible() else None)
                dialog.setWindowTitle("Review Notion Databases")
                dialog.setIcon(QMessageBox.Warning if report["has_warning"] else QMessageBox.Information)
                dialog.setText(report["summary"] or "Database review complete.")
                dialog.setInformativeText(report["details_html"])
                dialog.setDetailedText(report.get("details_text") or "")
                copy_btn = dialog.addButton("Copy Summary", QMessageBox.ActionRole)
                dialog.addButton(QMessageBox.Ok)
                dialog.setTextFormat(Qt.RichText)
                dialog.setStyleSheet("QLabel{min-width:720px;}")
                dialog.exec()

                if dialog.clickedButton() == copy_btn:
                    clipboard_text = (report.get("summary") or "Database review complete.").strip()
                    details_text = (report.get("details_text") or "").strip()
                    if details_text:
                        clipboard_text = f"{clipboard_text}\n\n{details_text}" if clipboard_text else details_text
                    QApplication.clipboard().setText(clipboard_text)
                    self.showMessage(
                        "Copied",
                        "The Notion database review summary was copied to the clipboard.",
                        QSystemTrayIcon.Information,
                        2500,
                    )

            if self.dashboard:
                self.dashboard.refresh_status()
            return databases
        except Exception as e:
            logger.error(f"Failed to check Notion access: {e}")
            self.notion_access_summary = f"Could not check Notion access: {e}"
            if show_message:
                QMessageBox.warning(
                    self.dashboard if self.dashboard and self.dashboard.isVisible() else None,
                    "Review Notion Databases",
                    "Could not refresh the Notion database review. Check the log file for details.",
                )
            if self.dashboard:
                self.dashboard.refresh_status()
            return []

    def get_mcp_status_text(self) -> str:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return "APPDATA not available"

        config_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
        if not config_path.exists():
            return "Not configured yet"

        server_name = get_claude_mcp_name()

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            servers = config_data.get("mcpServers", {})
            if server_name in servers:
                return f"Configured as {server_name} at {config_path}"
            if "local_notion_mirror" in servers:
                return f"Configured as local_notion_mirror at {config_path}"
        except Exception as e:
            logger.error(f"Failed to read Claude MCP config status: {e}")
            return "Config file exists but could not be read"

        return f"Config file exists but {server_name} is missing"

    def configure_mcp(self):
        success = configure_claude_mcp()
        connection_name = get_claude_mcp_name()
        if success:
            self.showMessage(
                "Claude MCP",
                f"Claude connection '{connection_name}' was updated.",
                QSystemTrayIcon.Information,
                2500,
            )
            self.last_summary = f"Claude connection '{connection_name}' updated successfully."
            self._record_job_result("Claude MCP update", True, self.last_summary)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary)
                self.dashboard.refresh_status()
        else:
            self.showMessage(
                "Claude MCP",
                "Could not update the Claude MCP configuration file.",
                QSystemTrayIcon.Critical,
                3000,
            )
            self.last_summary = "Claude MCP update failed. Check the log file."
            self._record_job_result("Claude MCP update", False, self.last_summary)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary)

    def on_wizard_complete(self):
        self.refresh_schedule()
        self.ensure_clickup_webhook_listener(show_message=False)
        self.last_summary = "Configuration complete. Ready to sync."
        self.show_dashboard()
        if self.dashboard:
            self.dashboard.set_status(self.last_summary)
        self.configure_mcp()
        self.showMessage("Ready", self.last_summary, QSystemTrayIcon.Information, 2000)

    def open_local_edit_dialog(self):
        self.show_dashboard()
        if self.dashboard:
            self.dashboard.queue_local_edit()

    def queue_local_edit(self, notion_id: str, title: str, ai_summary: str) -> bool:
        if not self.db.conn and not self.db.connect():
            message = "Please finish the database setup first."
            self.showMessage("Setup needed", message, QSystemTrayIcon.Critical, 2500)
            return False

        success, message = self.db.queue_local_edit(notion_id, title=title, ai_summary=ai_summary)
        if hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = message
        self.showMessage(
            "Local edit queued" if success else "Local edit failed",
            message,
            QSystemTrayIcon.Information if success else QSystemTrayIcon.Critical,
            3500,
        )
        if self.dashboard:
            self.dashboard.set_status(message)
            self.dashboard.refresh_status()
        if self.database_browser_window and self.database_browser_window.isVisible():
            self.database_browser_window.refresh_entries(target_notion_id=notion_id)
        self.refresh_schedule()
        self._record_job_result("Local edit queue", success, message)
        return success

    def restore_from_time_machine(self, restore_requests: list[dict], push_to_notion: bool = False) -> tuple[bool, str]:
        if not self.db or (not self.db.conn and not self.db.connect()):
            message = "Please finish the PostgreSQL setup first before running Time Machine recovery."
            self.last_summary = message
            self.showMessage("Recovery unavailable", message, QSystemTrayIcon.Critical, 3000)
            return False, message

        if not hasattr(self.db, "restore_from_time_machine"):
            message = "Time Machine recovery is not available in this build yet."
            self.last_summary = message
            self.showMessage("Recovery unavailable", message, QSystemTrayIcon.Critical, 3000)
            return False, message

        success, message = self.db.restore_from_time_machine(restore_requests, push_to_notion=push_to_notion)
        if hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = message or self.last_summary
        self.showMessage(
            "Recovery finished" if success else "Recovery issue",
            message,
            QSystemTrayIcon.Information if success else QSystemTrayIcon.Critical,
            4000,
        )
        if self.dashboard:
            self.dashboard.set_status(self.last_summary)
            self.dashboard.refresh_status()
        if self.database_browser_window and self.database_browser_window.isVisible():
            self.database_browser_window.refresh_entries()
        self.refresh_schedule()
        self._record_job_result("Time Machine recovery", success, message)
        return success, message

    def _confirm_one_way_sync(self, mode: str) -> bool:
        if mode not in {"pull", "push"}:
            return True

        stats = self.get_runtime_stats()
        title, message = self.build_confirmation_details(mode, pending_push=stats.get("pending_push", 0))
        reply = QMessageBox.question(
            self.dashboard if self.dashboard and self.dashboard.isVisible() else None,
            title,
            f"{message}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _set_sync_actions_enabled(self, enabled: bool):
        self.sync_action.setEnabled(enabled)
        self.resync_connections_action.setEnabled(enabled)
        self.pull_action.setEnabled(enabled)
        self.push_action.setEnabled(enabled)
        self.n8n_sync_action.setEnabled(enabled)
        self.clickup_sync_action.setEnabled(enabled)
        self.wordpress_cleanup_action.setEnabled(enabled)
        self.clickup_webhook_action.setEnabled(enabled)
        self.queue_edit_action.setEnabled(enabled)
        self.maintenance_action.setEnabled(enabled)
        self.browser_action.setEnabled(enabled)

    def run_due_maintenance(self):
        if self.worker and self.worker.isRunning():
            return

        if self.n8n_worker and self.n8n_worker.isRunning():
            return

        if self.clickup_worker and self.clickup_worker.isRunning():
            return

        if self.maintenance_worker and self.maintenance_worker.isRunning():
            return

        if self.connection_resync_worker and self.connection_resync_worker.isRunning():
            return

        if not self.db or (not self.db.conn and not self.db.connect()):
            return

        try:
            should_run, preview_summary, _preview_details = self.maintenance_manager.preview_due_maintenance()
        except Exception as exc:
            logger.warning(f"Could not preview the maintenance schedule cleanly: {exc}")
            should_run = True
            preview_summary = ""

        if not should_run:
            if preview_summary:
                self.last_summary = preview_summary
            if self.dashboard:
                self.dashboard.set_busy(False)
                self.dashboard.set_status(self.last_summary)
                self.dashboard.refresh_status()
            if not (self.worker and self.worker.isRunning()):
                self.set_status_light("ready", self.last_summary or "Ready")
            return

        self.set_status_light("syncing", "Running scheduled clean-up...")
        self.maintenance_worker = MaintenanceWorker(mode="due")
        self.maintenance_worker.finished.connect(self.on_maintenance_finished)
        self.maintenance_worker.start()

    @staticmethod
    def _describe_manual_maintenance(job_name: str | None = None) -> str:
        labels = {
            None: "Running full maintenance across Notion, ClickUp, WordPress, and the local vault...",
            "full_notion_dedup": "Running the full Notion tidy-up now...",
            "knowledge_library_check": "Running the Knowledge Library check now...",
            "full_clickup_dedup": "Running the ClickUp tidy-up now...",
            "full_wordpress_dedup": "Running the WordPress tidy-up now...",
            "post_weekly_report": "Preparing the weekly maintenance report now...",
        }
        return labels.get(job_name, "Running maintenance now...")

    def run_manual_maintenance(self, job_name: str | None = None):
        if self.worker and self.worker.isRunning():
            self.showMessage("Sync in progress", "Please wait for the current sync to finish first.", QSystemTrayIcon.Information, 2500)
            return

        if self.n8n_worker and self.n8n_worker.isRunning():
            self.showMessage("n8n sync in progress", "Please wait for the n8n backup to finish first.", QSystemTrayIcon.Information, 2500)
            return

        if self.clickup_worker and self.clickup_worker.isRunning():
            self.showMessage("ClickUp sync in progress", "Please wait for the ClickUp refresh to finish first.", QSystemTrayIcon.Information, 2500)
            return

        if self.maintenance_worker and self.maintenance_worker.isRunning():
            self.showMessage("Maintenance in progress", "A maintenance run is already in progress.", QSystemTrayIcon.Information, 2500)
            return

        if self.connection_resync_worker and self.connection_resync_worker.isRunning():
            self.showMessage("Resync in progress", "Please wait for the connection resync to finish first.", QSystemTrayIcon.Information, 2500)
            return

        if not self.db or (not self.db.conn and not self.db.connect()):
            self.showMessage("Setup needed", "Please open Setup and finish the database connection first.", QSystemTrayIcon.Critical, 2500)
            return

        status_text = self._describe_manual_maintenance(job_name)
        self.showMessage("Maintenance", status_text, QSystemTrayIcon.Information, 2200)
        self.set_status_light("syncing", status_text)
        if self.dashboard:
            self.dashboard.set_status(status_text)
            self.dashboard.set_busy(True)

        self.maintenance_worker = MaintenanceWorker(mode="manual", job_name=job_name)
        self.maintenance_worker.finished.connect(self.on_maintenance_finished)
        self.maintenance_worker.start()

    def on_maintenance_finished(self, success: bool, summary: str, details: dict):
        if self.db and hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()

        ran_any = bool((details or {}).get("ran_any"))
        if ran_any:
            self.last_summary = summary or self.last_summary
            self._record_job_result("Maintenance", success, self.last_summary)
            if self.dashboard:
                self.dashboard.set_busy(False)
                self.dashboard.set_status(self.last_summary)
                self.dashboard.refresh_status()
            self.showMessage(
                "Maintenance complete" if success else "Maintenance issue",
                self.last_summary,
                QSystemTrayIcon.Information if success else QSystemTrayIcon.Warning,
                3500,
            )
        elif not (self.worker and self.worker.isRunning()):
            if self.dashboard:
                self.dashboard.set_busy(False)
            self.set_status_light("ready", self.last_summary or "Ready")

        self.refresh_schedule()
        self.maintenance_worker = None

    def run_sync(self, mode: str = "sync"):
        if self.worker and self.worker.isRunning():
            self.showMessage("Sync in progress", "A sync is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.n8n_worker and self.n8n_worker.isRunning():
            self.showMessage("n8n sync in progress", "The n8n backup is still running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.clickup_worker and self.clickup_worker.isRunning():
            self.showMessage("ClickUp sync in progress", "The ClickUp refresh is still running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.maintenance_worker and self.maintenance_worker.isRunning():
            self.showMessage("Maintenance in progress", "The local clean-up is still running. Please try again in a moment.", QSystemTrayIcon.Information, 2500)
            return

        if self.connection_resync_worker and self.connection_resync_worker.isRunning():
            self.showMessage("Resync in progress", "The connection resync is still running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if not self.db.conn and not self.db.connect():
            self.showMessage("Setup needed", "Please open Setup and finish the database connection first.", QSystemTrayIcon.Critical, 2500)
            return

        if not self._confirm_one_way_sync(mode):
            return

        action_text = {
            "sync": "Running two-way sync...",
            "pull": "Importing updates from Notion...",
            "push": "Pushing pending local changes to Notion...",
        }.get(mode, "Running sync...")

        self._set_sync_actions_enabled(False)
        self.showMessage("Working", action_text, QSystemTrayIcon.Information, 2000)
        if self.dashboard:
            self.dashboard.set_status(action_text)
            self.dashboard.set_busy(True)

        self.worker = SyncWorker(mode)
        self.worker.finished.connect(self.on_sync_finished)
        self.worker.start()

    def on_sync_finished(self, mode: str, success: bool, summary: str):
        self._set_sync_actions_enabled(True)
        if self.db and hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = summary or self.last_summary
        action_name = {
            "sync": "smart sync",
            "pull": "import from Notion",
            "push": "push to Notion",
        }.get(mode, mode)

        if success:
            self.last_sync_name = action_name
            self.last_sync_at = datetime.now()
            self.last_sync_action.setText(
                self.build_last_sync_status_text(
                    activity_name=self.last_sync_name,
                    activity_at=self.last_sync_at,
                )
            )
            self._record_job_result(action_name.title(), True, self.last_summary)
            self.showMessage("Success", self.last_summary, QSystemTrayIcon.Information, 3500)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary)
        else:
            self._record_job_result(action_name.title(), False, self.last_summary or f"{action_name.capitalize()} failed.")
            self.showMessage("Error", self.last_summary or f"{action_name.capitalize()} failed.", QSystemTrayIcon.Critical, 4000)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary or f"{action_name.capitalize()} failed. Check logs.")

        self.refresh_schedule()
        if self.dashboard:
            self.dashboard.set_busy(False)
            self.dashboard.refresh_status()
        self.worker = None

    def run_connection_resync(self, service: str = "all"):
        target_service = str(service or "all").strip().lower() or "all"
        if target_service not in {"all", "notion", "clickup"}:
            target_service = "all"

        if self.worker and self.worker.isRunning():
            self.showMessage("Sync in progress", "Please wait for the current Notion sync to finish first.", QSystemTrayIcon.Information, 2000)
            return

        if self.n8n_worker and self.n8n_worker.isRunning():
            self.showMessage("n8n sync in progress", "Please wait for the n8n backup to finish first.", QSystemTrayIcon.Information, 2000)
            return

        if self.clickup_worker and self.clickup_worker.isRunning():
            self.showMessage("ClickUp sync in progress", "Please wait for the ClickUp refresh to finish first.", QSystemTrayIcon.Information, 2000)
            return

        if self.maintenance_worker and self.maintenance_worker.isRunning():
            self.showMessage("Maintenance in progress", "Please wait for the local clean-up to finish first.", QSystemTrayIcon.Information, 2500)
            return

        if self.connection_resync_worker and self.connection_resync_worker.isRunning():
            label = {"notion": "Notion resync", "clickup": "ClickUp resync"}.get(self.connection_resync_service, "Resync")
            self.showMessage("Resync in progress", f"The {label.lower()} is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if not self.db.conn and not self.db.connect():
            self.showMessage("Setup needed", "Please open Setup and finish the database connection first.", QSystemTrayIcon.Critical, 2500)
            return

        action_text = {
            "notion": "Refreshing Notion connections and pulling in newly visible databases...",
            "clickup": "Refreshing ClickUp connections and pulling in newly visible lists...",
            "all": "Refreshing all saved connections and pulling in newly visible items...",
        }.get(target_service, "Refreshing saved connections...")
        self.connection_resync_service = target_service
        self._set_sync_actions_enabled(False)
        self.showMessage("Working", action_text, QSystemTrayIcon.Information, 2200)
        if self.dashboard:
            self.dashboard.set_status(action_text)
            self.dashboard.set_busy(True)

        self.connection_resync_worker = ConnectionResyncWorker(target_service)
        self.connection_resync_worker.finished.connect(self.on_connection_resync_finished)
        self.connection_resync_worker.start()

    def on_connection_resync_finished(self, success: bool, summary: str, details: dict):
        self._set_sync_actions_enabled(True)
        if self.db and hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()

        notion_details = (details or {}).get("notion", {})
        if notion_details and notion_details.get("status") != "skipped":
            databases = notion_details.get("databases") or []
            linked_missing = notion_details.get("linked_missing") or []
            report = self.build_notion_access_report(databases, get_env("NOTION_DB_ID", ""), linked_missing)
            self.notion_access_summary = report.get("summary") or self.notion_access_summary
            self.notion_last_databases = list(databases)
            self.notion_last_unlinked = list(linked_missing)

        self.last_summary = summary or self.last_summary
        sync_label = {
            "notion": "Notion resync",
            "clickup": "ClickUp resync",
            "all": "connection resync",
        }.get(self.connection_resync_service, "connection resync")
        self._record_job_result(sync_label.title(), success, self.last_summary)
        if success:
            self.last_sync_name = sync_label
            self.last_sync_at = datetime.now()
            self.last_sync_action.setText(
                self.build_last_sync_status_text(
                    activity_name=self.last_sync_name,
                    activity_at=self.last_sync_at,
                )
            )
            title = "Notion resync complete" if self.connection_resync_service == "notion" else (
                "ClickUp resync complete" if self.connection_resync_service == "clickup" else "Resync complete"
            )
            self.showMessage(title, self.last_summary, QSystemTrayIcon.Information, 4000)
        else:
            title = "Notion resync issue" if self.connection_resync_service == "notion" else (
                "ClickUp resync issue" if self.connection_resync_service == "clickup" else "Resync issue"
            )
            self.showMessage(title, self.last_summary or "The connection resync needs attention.", QSystemTrayIcon.Warning, 4500)

        self.refresh_schedule()
        if self.dashboard:
            self.dashboard.set_busy(False)
            self.dashboard.set_status(self.last_summary)
            self.dashboard.refresh_status()
        self.connection_resync_worker = None
        self.connection_resync_service = "all"

    def run_n8n_sync(self):
        if self.worker and self.worker.isRunning():
            self.showMessage("Sync in progress", "A Notion sync is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.n8n_worker and self.n8n_worker.isRunning():
            self.showMessage("n8n sync in progress", "The n8n backup is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.clickup_worker and self.clickup_worker.isRunning():
            self.showMessage("ClickUp sync in progress", "The ClickUp refresh is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.maintenance_worker and self.maintenance_worker.isRunning():
            self.showMessage("Maintenance in progress", "The local clean-up is still running. Please try again in a moment.", QSystemTrayIcon.Information, 2500)
            return

        if self.connection_resync_worker and self.connection_resync_worker.isRunning():
            self.showMessage("Resync in progress", "Please wait for the connection resync to finish first.", QSystemTrayIcon.Information, 2000)
            return

        if not self.db.conn and not self.db.connect():
            self.showMessage("Setup needed", "Please open Setup and finish the database connection first.", QSystemTrayIcon.Critical, 2500)
            return

        action_text = "Syncing n8n workflows..."
        self._set_sync_actions_enabled(False)
        self.showMessage("Working", action_text, QSystemTrayIcon.Information, 2000)
        if self.dashboard:
            self.dashboard.set_status(action_text)
            self.dashboard.set_busy(True)

        self.n8n_worker = N8nSyncWorker()
        self.n8n_worker.finished.connect(self.on_n8n_sync_finished)
        self.n8n_worker.start()

    def on_n8n_sync_finished(self, success: bool, summary: str):
        self._set_sync_actions_enabled(True)
        if self.db and hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = summary or self.last_summary

        if success:
            self.last_sync_name = "n8n sync"
            self.last_sync_at = datetime.now()
            self.last_sync_action.setText(
                self.build_last_sync_status_text(
                    activity_name=self.last_sync_name,
                    activity_at=self.last_sync_at,
                )
            )
            self._record_job_result("n8n sync", True, self.last_summary)
            self.showMessage("Success", self.last_summary, QSystemTrayIcon.Information, 3500)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary)
        else:
            self._record_job_result("n8n sync", False, self.last_summary or "n8n sync failed.")
            self.showMessage("Error", self.last_summary or "n8n sync failed.", QSystemTrayIcon.Critical, 4000)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary or "n8n sync failed. Check logs.")

        self.refresh_schedule()
        if self.dashboard:
            self.dashboard.set_busy(False)
            self.dashboard.refresh_status()
        self.n8n_worker = None

    def run_clickup_sync(self):
        if self.worker and self.worker.isRunning():
            self.showMessage("Sync in progress", "A Notion sync is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.n8n_worker and self.n8n_worker.isRunning():
            self.showMessage("n8n sync in progress", "Please wait for the n8n backup to finish first.", QSystemTrayIcon.Information, 2000)
            return

        if self.clickup_worker and self.clickup_worker.isRunning():
            self.showMessage("ClickUp sync in progress", "The ClickUp refresh is already running in the background.", QSystemTrayIcon.Information, 2000)
            return

        if self.maintenance_worker and self.maintenance_worker.isRunning():
            self.showMessage("Maintenance in progress", "The local clean-up is still running. Please try again in a moment.", QSystemTrayIcon.Information, 2500)
            return

        if self.connection_resync_worker and self.connection_resync_worker.isRunning():
            self.showMessage("Resync in progress", "Please wait for the connection resync to finish first.", QSystemTrayIcon.Information, 2000)
            return

        if not self.db.conn and not self.db.connect():
            self.showMessage("Setup needed", "Please open Setup and finish the database connection first.", QSystemTrayIcon.Critical, 2500)
            return

        self.ensure_clickup_webhook_listener(show_message=False)
        action_text = "Syncing ClickUp tasks and comments..."
        self._set_sync_actions_enabled(False)
        self.showMessage("Working", action_text, QSystemTrayIcon.Information, 2000)
        if self.dashboard:
            self.dashboard.set_status(action_text)
            self.dashboard.set_busy(True)

        self.clickup_worker = ClickUpSyncWorker()
        self.clickup_worker.finished.connect(self.on_clickup_sync_finished)
        self.clickup_worker.start()

    def on_clickup_sync_finished(self, success: bool, summary: str):
        self._set_sync_actions_enabled(True)
        if self.db and hasattr(self.db, "invalidate_runtime_caches"):
            self.db.invalidate_runtime_caches()
        self.last_summary = summary or self.last_summary

        if success:
            self.last_sync_name = "ClickUp sync"
            self.last_sync_at = datetime.now()
            self.last_sync_action.setText(
                self.build_last_sync_status_text(
                    activity_name=self.last_sync_name,
                    activity_at=self.last_sync_at,
                )
            )
            self._record_job_result("ClickUp sync", True, self.last_summary)
            self.showMessage("Success", self.last_summary, QSystemTrayIcon.Information, 3500)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary)
        else:
            self._record_job_result("ClickUp sync", False, self.last_summary or "ClickUp sync failed.")
            self.showMessage("Error", self.last_summary or "ClickUp sync failed.", QSystemTrayIcon.Critical, 4000)
            if self.dashboard:
                self.dashboard.set_status(self.last_summary or "ClickUp sync failed. Check logs.")

        self.refresh_schedule()
        if self.dashboard:
            self.dashboard.set_busy(False)
            self.dashboard.refresh_status()
        self.clickup_worker = None

    def quit_app(self):
        logger.info("Application shutting down.")
        self.auto_timer.stop()
        self.maintenance_timer.stop()
        self.status_flash_timer.stop()
        if self.clickup_webhook_server:
            self.clickup_webhook_server.stop()
        self.hide()
        app = QApplication.instance()
        if app:
            app.quit()
