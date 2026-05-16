import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QLockFile, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle, QSystemTrayIcon

from src.core.config import get_app_display_name, get_env, get_tray_icon_path, load_config, logger
from src.core.proxy import MediaProxyServer
from src.db.database import DatabaseManager
from src.ui.main_window import build_dark_stylesheet
from src.ui.tray import TrayApp
from src.ui.wizard import SetupWizard


class DBConnectWorker(QThread):
    """Connects to the database on a background thread so the UI never freezes."""
    finished = Signal(bool, str)

    def __init__(self, db_manager, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager

    def run(self):
        try:
            logger.info("[Startup] Connecting to database in background...")
            connected = self.db_manager.connect()
            logger.info(f"[Startup] Database connection finished. Connected: {connected}")
            self.finished.emit(connected, self.db_manager.last_error or "")
        except Exception as e:
            logger.exception(f"[Startup] Database connection error: {e}")
            self.finished.emit(False, str(e))


def main():
    if "--mcp-server" in sys.argv:
        load_config()
        from src.mcp.business_brain_mcp import run_business_brain_mcp

        run_business_brain_mcp()
        return

    logger.info("Starting Unified Local Data Vault & AI Command Center...")
    load_config()

    startup_launch = "--startup" in sys.argv

    app = QApplication(sys.argv)
    app.setApplicationName(get_app_display_name())
    app.setStyle("Fusion")
    app.setStyleSheet(build_dark_stylesheet())
    app.setQuitOnLastWindowClosed(False)

    lock_path = str(Path(__file__).resolve().parent.parent / "notion_local_sync.lock")
    instance_lock = QLockFile(lock_path)
    try:
        stale_minutes = max(1, int(get_env("INSTANCE_LOCK_STALE_MINUTES", "10") or "10"))
    except ValueError:
        stale_minutes = 10
    instance_lock.setStaleLockTime(stale_minutes * 60 * 1000)

    if not instance_lock.tryLock(100):
        recovered_stale_lock = instance_lock.removeStaleLockFile() and instance_lock.tryLock(1500)
        if recovered_stale_lock:
            logger.warning("Recovered a stale app lock file and continued startup.")
        else:
            logger.warning("Another instance is already running. Exiting duplicate launch.")
            QMessageBox.information(
                None,
                "Already running",
                "The app is already open in the tray near the clock. If it looks stuck, close the older copy first and then launch it again.",
            )
            return

    app.instance_lock = instance_lock
    db_manager = DatabaseManager()
    proxy = MediaProxyServer()
    exit_code = 0

    try:
        proxy.start()

        tray_icon_path = get_tray_icon_path()
        icon = QIcon(str(tray_icon_path)) if tray_icon_path else app.style().standardIcon(QStyle.SP_ComputerIcon)
        if not icon.isNull():
            app.setWindowIcon(icon)
        tray = TrayApp(icon, db_manager, SetupWizard)
        tray.show()
        tray.setContextMenu(tray.menu)

        def after_db_connect(connected, error):
            try:
                if connected:
                    logger.info("[Startup] Database connected successfully.")
                else:
                    logger.info(f"[Startup] Database not connected. Error: {error}")
                tray.on_db_ready(connected, error)
            except Exception as exc:
                logger.exception(f"[Startup] after_db_connect error: {exc}")

        # Default to a quiet tray-first launch so startup stays minimized.
        # Use `OPEN_DASHBOARD_ON_LAUNCH=1` or `--show-dashboard` to open it right away.
        open_dashboard_now = ("--show-dashboard" in sys.argv)
        if not open_dashboard_now:
            open_dashboard_pref = str(get_env("OPEN_DASHBOARD_ON_LAUNCH", "0") or "0").strip().lower()
            open_dashboard_now = (not startup_launch) and open_dashboard_pref not in {"0", "false", "off", "no"}

        if open_dashboard_now:
            QTimer.singleShot(100, tray.show_dashboard)

        # --- DB re-enabled: connect in background after dashboard is visible ---
        db_thread = DBConnectWorker(db_manager, parent=app)
        db_thread.finished.connect(after_db_connect)
        QTimer.singleShot(500, db_thread.start)

        exit_code = app.exec()
    except Exception as exc:
        logger.exception(f"Fatal startup error: {exc}")
        QMessageBox.critical(None, "Startup failed", f"The app hit an unexpected error while starting:\n\n{exc}")
        exit_code = 1
    finally:
        proxy.stop()
        db_manager.close()
        if hasattr(app, "instance_lock"):
            app.instance_lock.unlock()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
