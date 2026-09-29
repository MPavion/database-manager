import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QLockFile, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from src.core.config import BASE_DIR, get_app_display_name, load_config, logger
from src.core.proxy import MediaProxyServer
from src.db.database import DatabaseManager
from src.ui.tray import TrayApp


class _DbConnectWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, db: DatabaseManager):
        super().__init__()
        self.db = db

    def run(self):
        try:
            ok = self.db.connect()
            self.done.emit(ok, self.db.last_error or "")
        except Exception as exc:
            self.done.emit(False, str(exc))


def main():
    # ── MCP server mode (launched by Claude Desktop) ─────────────────────────
    if "--mcp-server" in sys.argv:
        load_config()
        from src.mcp.server import run_mcp_server
        run_mcp_server()
        return

    # ── GUI / tray mode ───────────────────────────────────────────────────────
    load_config()

    app = QApplication(sys.argv)
    app.setApplicationName(get_app_display_name())
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)

    # Instance lock — prevents duplicate tray icons
    lock_path = str(BASE_DIR / "notion_claude_optimizer.lock")
    lock      = QLockFile(lock_path)
    try:
        stale_ms = max(1, int(str(
            __import__("os").getenv("INSTANCE_LOCK_STALE_MINUTES", "10") or "10"
        ).strip())) * 60 * 1000
    except ValueError:
        stale_ms = 10 * 60 * 1000
    lock.setStaleLockTime(stale_ms)

    if not lock.tryLock(100):
        if not (lock.removeStaleLockFile() and lock.tryLock(1500)):
            logger.warning("Another instance is already running.")
            return

    app.instance_lock = lock

    # Auto-configure Claude Desktop MCP entry
    try:
        from src.mcp.configurator import configure_claude_mcp
        configure_claude_mcp()
    except Exception as exc:
        logger.warning(f"MCP auto-configuration failed (non-fatal): {exc}")

    # Start media proxy
    proxy = MediaProxyServer()
    proxy.start()

    # Build tray (shown before DB connects so user sees the icon immediately)
    db   = DatabaseManager()
    tray = TrayApp(db)
    tray.show()

    # Connect to DB in background after the event loop starts
    db_worker = _DbConnectWorker(db)
    db_worker.done.connect(
        lambda ok, err: tray.on_db_ready(ok, err)
    )
    QTimer.singleShot(500, db_worker.start)

    exit_code = app.exec()

    proxy.stop()
    db.close()
    lock.unlock()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
