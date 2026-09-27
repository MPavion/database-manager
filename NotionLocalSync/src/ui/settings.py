"""
Settings dialog — accessible via tray right-click → Settings.

Sections
  Notion      : Integration Token, Database IDs
  PostgreSQL  : host / port / user / password / database name
  Sync        : interval, Windows auto-start
  Claude MCP  : server name + "Auto-configure Claude Desktop" button

Each section has a "Test" button.  Changes are saved to .env on OK.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.config import (
    get_env,
    get_secret,
    is_windows_startup_enabled,
    logger,
    save_env_var,
    save_secret,
    sync_windows_startup,
)


# ── Background test workers ───────────────────────────────────────────────────

class _NotionTestWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def run(self):
        try:
            from notion_client import Client
            c    = Client(auth=self.token)
            resp = c.users.me()
            name = resp.get("name") or resp.get("id") or "unknown"
            self.done.emit(True, f"Connected as {name}")
        except Exception as exc:
            self.done.emit(False, str(exc))


class _DbTestWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, host: str, port: str, user: str,
                  password: str, dbname: str):
        super().__init__()
        self.host = host; self.port = port; self.user = user
        self.password = password; self.dbname = dbname

    def run(self):
        try:
            import psycopg2
            conn = psycopg2.connect(
                host=self.host, port=self.port, user=self.user,
                password=self.password, dbname=self.dbname,
                connect_timeout=5,
            )
            conn.close()
            self.done.emit(True, "Connection successful.")
        except Exception as exc:
            self.done.emit(False, str(exc))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_group(title: str) -> tuple[QGroupBox, QFormLayout]:
    box    = QGroupBox(title)
    layout = QFormLayout(box)
    layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
    return box, layout


def _status_label(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    return lbl


def _set_status(lbl: QLabel, ok: bool, text: str):
    colour = "#22c55e" if ok else "#ef4444"
    lbl.setStyleSheet(f"color: {colour}; font-size: 11px;")
    lbl.setText(text)


# ── Main dialog ───────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Notion Local Sync — Settings")
        self.setMinimumWidth(520)
        self.setModal(True)

        self._workers: list[QThread] = []
        self._build_ui()
        self._load_values()

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ── Notion ────────────────────────────────────────────────────────────
        notion_box, notion_form = _make_group("Notion")

        self._notion_token = QLineEdit()
        self._notion_token.setEchoMode(QLineEdit.Password)
        self._notion_token.setPlaceholderText("secret_…")
        notion_form.addRow("Integration Token:", self._notion_token)

        self._notion_db_ids = QLineEdit()
        self._notion_db_ids.setPlaceholderText("ALL  or  id1,id2,…")
        notion_form.addRow("Database IDs:", self._notion_db_ids)

        self._notion_status = _status_label()
        notion_form.addRow("", self._notion_status)

        notion_test_btn = QPushButton("Test Notion Connection")
        notion_test_btn.clicked.connect(self._test_notion)
        notion_form.addRow("", notion_test_btn)

        root.addWidget(notion_box)

        # ── PostgreSQL ────────────────────────────────────────────────────────
        pg_box, pg_form = _make_group("PostgreSQL")

        self._pg_host   = QLineEdit(); pg_form.addRow("Host:", self._pg_host)
        self._pg_port   = QLineEdit(); pg_form.addRow("Port:", self._pg_port)
        self._pg_user   = QLineEdit(); pg_form.addRow("User:", self._pg_user)
        self._pg_pass   = QLineEdit()
        self._pg_pass.setEchoMode(QLineEdit.Password)
        pg_form.addRow("Password:", self._pg_pass)
        self._pg_dbname = QLineEdit(); pg_form.addRow("Database:", self._pg_dbname)

        self._pg_status = _status_label()
        pg_form.addRow("", self._pg_status)

        pg_test_btn = QPushButton("Test DB Connection")
        pg_test_btn.clicked.connect(self._test_db)
        pg_form.addRow("", pg_test_btn)

        root.addWidget(pg_box)

        # ── Sync ──────────────────────────────────────────────────────────────
        sync_box, sync_form = _make_group("Sync")

        self._sync_interval = QSpinBox()
        self._sync_interval.setRange(1, 1440)
        self._sync_interval.setSuffix(" minutes")
        sync_form.addRow("Sync every:", self._sync_interval)

        self._startup_cb = QCheckBox("Launch automatically when Windows starts")
        sync_form.addRow("", self._startup_cb)

        root.addWidget(sync_box)

        # ── Claude MCP ────────────────────────────────────────────────────────
        mcp_box, mcp_form = _make_group("Claude Desktop MCP")

        self._mcp_name = QLineEdit()
        self._mcp_name.setPlaceholderText("Notion Local DB")
        mcp_form.addRow("Server name:", self._mcp_name)

        self._mcp_status = _status_label()
        mcp_form.addRow("", self._mcp_status)

        mcp_btn = QPushButton("Auto-configure Claude Desktop")
        mcp_btn.clicked.connect(self._configure_mcp)
        mcp_form.addRow("", mcp_btn)

        mcp_note = QLabel(
            "Writes the MCP entry to Claude Desktop's config and removes any old entries."
        )
        mcp_note.setWordWrap(True)
        mcp_note.setStyleSheet("color: #888; font-size: 11px;")
        mcp_form.addRow("", mcp_note)

        root.addWidget(mcp_box)

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save_and_accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ── Load current values ───────────────────────────────────────────────────
    def _load_values(self):
        self._notion_token.setText(
            get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        )
        self._notion_db_ids.setText(get_env("NOTION_DB_ID", "ALL"))

        self._pg_host.setText(get_env("PG_HOST", "localhost"))
        self._pg_port.setText(get_env("PG_PORT", "5432"))
        self._pg_user.setText(get_env("PG_USER", "postgres"))
        self._pg_pass.setText(get_secret("PG_PASSWORD"))
        self._pg_dbname.setText(get_env("PG_DBNAME", "notion_mirror"))

        try:
            self._sync_interval.setValue(int(get_env("SYNC_INTERVAL_MINUTES", "5")))
        except ValueError:
            self._sync_interval.setValue(5)

        self._startup_cb.setChecked(is_windows_startup_enabled())
        self._mcp_name.setText(get_env("CLAUDE_MCP_NAME", "Notion Local DB"))

    # ── Tests ─────────────────────────────────────────────────────────────────
    def _test_notion(self):
        token = self._notion_token.text().strip()
        if not token:
            _set_status(self._notion_status, False, "Enter a token first.")
            return
        self._notion_status.setText("Testing…")
        w = _NotionTestWorker(token)
        w.done.connect(lambda ok, msg: _set_status(self._notion_status, ok, msg))
        w.done.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _test_db(self):
        self._pg_status.setText("Testing…")
        w = _DbTestWorker(
            host     = self._pg_host.text().strip() or "localhost",
            port     = self._pg_port.text().strip() or "5432",
            user     = self._pg_user.text().strip() or "postgres",
            password = self._pg_pass.text(),
            dbname   = self._pg_dbname.text().strip() or "notion_mirror",
        )
        w.done.connect(lambda ok, msg: _set_status(self._pg_status, ok, msg))
        w.done.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _configure_mcp(self):
        self._mcp_status.setText("Configuring…")
        # Save the name first so the configurator picks it up
        name = self._mcp_name.text().strip() or "Notion Local DB"
        save_env_var("CLAUDE_MCP_NAME", name)

        try:
            from src.mcp.configurator import configure_claude_mcp
            ok = configure_claude_mcp()
            _set_status(
                self._mcp_status, ok,
                f"Done — Claude Desktop entry '{name}' written." if ok
                else "Failed — check the log for details.",
            )
        except Exception as exc:
            _set_status(self._mcp_status, False, str(exc))

    # ── Save ──────────────────────────────────────────────────────────────────
    def _save_and_accept(self):
        try:
            save_secret("NOTION_TOKEN", self._notion_token.text().strip(),
                         encrypted_key="NOTION_TOKEN_ENCRYPTED")
            save_env_var("NOTION_DB_ID",
                          self._notion_db_ids.text().strip() or "ALL")

            save_env_var("PG_HOST",   self._pg_host.text().strip() or "localhost")
            save_env_var("PG_PORT",   self._pg_port.text().strip() or "5432")
            save_env_var("PG_USER",   self._pg_user.text().strip() or "postgres")
            save_secret("PG_PASSWORD", self._pg_pass.text())
            save_env_var("PG_DBNAME", self._pg_dbname.text().strip() or "notion_mirror")

            save_env_var("SYNC_INTERVAL_MINUTES", str(self._sync_interval.value()))

            startup_enabled = self._startup_cb.isChecked()
            save_env_var("WINDOWS_STARTUP_ENABLED", "1" if startup_enabled else "0")
            sync_windows_startup(enabled=startup_enabled)

            mcp_name = self._mcp_name.text().strip() or "Notion Local DB"
            save_env_var("CLAUDE_MCP_NAME", mcp_name)

            logger.info("Settings saved.")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        self.accept()
