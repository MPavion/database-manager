"""
System-tray application with a 3-state traffic-light icon.

  🟢 Green  – connected, idle (last sync succeeded)
  🟡 Amber  – sync in progress
  🔴 Red    – DB unreachable or sync error → tooltip + balloon notification

Right-click menu:
  [App name]          (disabled header)
  ─────────────────
  Last sync: …        (disabled, informational)
  Force Sync Now
  Settings…
  ─────────────────
  Quit
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src.core.config import get_app_display_name, get_backup_hour, get_env, logger
from src.db.database import DatabaseManager


# ── Traffic-light colours ─────────────────────────────────────────────────────
_PALETTE: dict[str, QColor] = {
    "green": QColor("#22c55e"),
    "amber": QColor("#f59e0b"),
    "red":   QColor("#ef4444"),
}
_BORDER = QColor(0, 0, 0, 50)


def _make_traffic_icon(colour: str) -> QIcon:
    size = 64
    pm   = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(_PALETTE.get(colour, _PALETTE["green"])))
    p.setPen(QPen(_BORDER, 2))
    p.drawEllipse(4, 4, size - 8, size - 8)
    p.end()
    return QIcon(pm)


def _brand_icon_with_dot(brand_path: str, colour: str) -> QIcon:
    """Load the brand image and overlay a small status dot in the bottom-right."""
    base = QPixmap(brand_path).scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    if base.isNull():
        return _make_traffic_icon(colour)

    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, base)

    # Small dot: 18×18 at bottom-right with a thin white ring for visibility
    dot_size   = 18
    dot_margin = 2
    x = 64 - dot_size - dot_margin
    y = 64 - dot_size - dot_margin
    p.setPen(QPen(QColor(255, 255, 255, 200), 2))
    p.setBrush(QBrush(_PALETTE.get(colour, _PALETTE["green"])))
    p.drawEllipse(x, y, dot_size, dot_size)
    p.end()
    return QIcon(pm)


# ── Background backup worker ─────────────────────────────────────────────────
class _BackupWorker(QThread):
    done = Signal(bool, str)

    def run(self):
        try:
            from src.db.backup import BackupManager
            ok, msg = BackupManager().run_backup()
            self.done.emit(ok, msg)
        except Exception as exc:
            logger.exception(f"Backup worker error: {exc}")
            self.done.emit(False, str(exc))


# ── Background sync worker ────────────────────────────────────────────────────
class _SyncWorker(QThread):
    done = Signal(bool, str)   # (success, summary_message)

    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db

    def run(self):
        try:
            from src.sync.engine import SyncEngine
            from src.db.indexer import rebuild_index

            engine = SyncEngine(self.db)
            ok     = engine.perform_sync()

            if ok:
                rebuild_index(self.db)

            now = datetime.now(tz=timezone.utc).strftime("%H:%M")
            self.done.emit(ok, engine.last_summary or f"Synced at {now}")
        except Exception as exc:
            logger.exception(f"Sync worker error: {exc}")
            self.done.emit(False, str(exc))


# ── Tray application ──────────────────────────────────────────────────────────
class TrayApp(QSystemTrayIcon):
    IDLE    = "green"
    SYNCING = "amber"
    ERROR   = "red"

    def __init__(self, db: DatabaseManager, parent=None):
        super().__init__(parent)
        self.db             = db
        self._state         = self.IDLE
        self._sync_worker:   _SyncWorker   | None = None
        self._backup_worker: _BackupWorker | None = None

        self._app_name = get_app_display_name()
        self.setToolTip(self._app_name)
        self._apply_icon(self.IDLE)

        self._build_menu()
        self._start_timer()
        self._start_backup_timer()

    # ── Icon ──────────────────────────────────────────────────────────────────
    def _apply_icon(self, colour: str):
        from src.core.config import get_tray_icon_path
        brand_path = get_tray_icon_path()
        if brand_path:
            self.setIcon(_brand_icon_with_dot(str(brand_path), colour))
        else:
            self.setIcon(_make_traffic_icon(colour))

    # ── Menu ──────────────────────────────────────────────────────────────────
    def _build_menu(self):
        menu = QMenu()

        header = menu.addAction(self._app_name)
        header.setEnabled(False)

        menu.addSeparator()

        self._status_action = menu.addAction("Status: starting…")
        self._status_action.setEnabled(False)

        menu.addSeparator()

        menu.addAction("Force Sync Now", self._force_sync)
        menu.addAction("Settings…",      self._show_settings)

        menu.addSeparator()

        menu.addAction("Quit", self._quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activate)

    def _on_activate(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_settings()

    # ── Timer ─────────────────────────────────────────────────────────────────
    def _start_timer(self):
        try:
            minutes = max(1, int(get_env("SYNC_INTERVAL_MINUTES", "5")))
        except ValueError:
            minutes = 5
        self._timer = QTimer(self)
        self._timer.setInterval(minutes * 60 * 1000)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()
        logger.info(f"Sync timer: every {minutes} minute(s).")

    def _on_timer(self):
        self._run_sync()

    # ── Backup timer ──────────────────────────────────────────────────────────
    def _start_backup_timer(self):
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(60 * 60 * 1000)  # check every hour
        self._backup_timer.timeout.connect(self._check_backup)
        self._backup_timer.start()

    def _check_backup(self):
        from src.db.backup import BackupManager
        bm = BackupManager()
        if not bm.is_configured():
            return
        if datetime.now().hour < get_backup_hour():
            return
        if bm.today_backup_exists():
            return
        self._run_backup()

    def _run_backup(self):
        if self._backup_worker and self._backup_worker.isRunning():
            return
        logger.info("Starting scheduled backup…")
        worker = _BackupWorker()
        worker.done.connect(self._on_backup_done)
        worker.finished.connect(lambda: setattr(self, "_backup_worker", None))
        self._backup_worker = worker
        worker.start()

    def _on_backup_done(self, success: bool, message: str):
        if success:
            logger.info(f"Backup complete: {message}")
        else:
            logger.warning(f"Backup failed: {message}")
            self.showMessage(
                "Notion Sync — Backup Failed",
                message,
                QSystemTrayIcon.Warning,
                8000,
            )

    # ── DB ready callback ─────────────────────────────────────────────────────
    def on_db_ready(self, connected: bool, error: str = ""):
        if connected:
            self._set_state(self.IDLE, "Connected — syncing…")
            self._run_sync()
        else:
            self._set_state(self.ERROR, f"DB connection failed: {error}")

    # ── State ─────────────────────────────────────────────────────────────────
    def _set_state(self, state: str, message: str = ""):
        self._state = state
        self._apply_icon(state)

        display = message or state
        self.setToolTip(f"{self._app_name} — {display}")
        self._status_action.setText(f"Status: {display}")

        if state == self.ERROR and message:
            self.showMessage(
                "Notion Sync — Error",
                message,
                QSystemTrayIcon.Critical,
                8000,
            )

    # ── Sync ──────────────────────────────────────────────────────────────────
    def _force_sync(self):
        self._run_sync()

    def _run_sync(self):
        if self._state == self.SYNCING:
            return
        if not self.db.is_connected():
            self._set_state(self.ERROR, "Database not connected — check Settings.")
            return

        self._set_state(self.SYNCING, "Syncing…")
        worker = _SyncWorker(self.db)
        worker.done.connect(self._on_sync_done)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._sync_worker = worker
        worker.start()

    def _on_sync_done(self, success: bool, message: str):
        if success:
            self._set_state(self.IDLE, message)
        else:
            self._set_state(self.ERROR, message)

    def _cleanup_worker(self, worker: _SyncWorker):
        if self._sync_worker is worker:
            self._sync_worker = None

    # ── Settings ──────────────────────────────────────────────────────────────
    def _show_settings(self):
        from src.ui.settings import SettingsDialog
        dlg = SettingsDialog()
        if dlg.exec():
            self._timer.stop()
            self._start_timer()
            self._check_backup()   # re-check immediately in case backup dir just set

    # ── Quit ──────────────────────────────────────────────────────────────────
    def _quit(self):
        if self._sync_worker and self._sync_worker.isRunning():
            self._sync_worker.quit()
            self._sync_worker.wait(3000)
        if self._backup_worker and self._backup_worker.isRunning():
            self._backup_worker.wait(10000)
        QApplication.quit()
