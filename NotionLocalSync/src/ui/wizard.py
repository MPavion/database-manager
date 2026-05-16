import secrets
import string
from typing import Any, Callable, cast

import requests

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from notion_client import Client

from src.core.config import get_env, get_secret, logger, save_env_var, save_secret
from src.core.http import build_retry_session
from src.mcp.configurator import configure_claude_mcp
from src.sync.engine import ClickUpSyncEngine, N8nSyncEngine

# (key, display name, description)
_SERVICES = [
    ("notion",    "Notion",         "Connect your Notion workspace and choose which databases to mirror."),
    ("database",  "Local database", "Set up the PostgreSQL database and choose sync timing."),
    ("backups",   "Backups",        "Keep backups local, or also copy them to Amazon S3."),
    ("clickup",   "ClickUp",        "Optional — task management and sync support."),
    ("wordpress", "WordPress",      "Optional — site maintenance and content management."),
    ("n8n",       "n8n",            "Optional — connect your n8n automation server."),
    ("mautic",    "Mautic",         "Optional — connect your Mautic marketing workspace."),
]


def _link_label(html: str) -> QLabel:
    """A label that renders HTML and opens links in the system browser."""
    lbl = QLabel(html)
    lbl.setWordWrap(True)
    lbl.setOpenExternalLinks(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    return lbl


class SetupWizard(QWidget):
    """
    Per-service setup wizard.

    Opens on a service-picker landing page. Each service has its own
    focused page with step-by-step instructions (including clickable links
    to the relevant web pages) and a form to enter credentials.
    """

    def __init__(
        self,
        db_manager: Any,
        on_complete_callback: Callable[[], None],
        tray_app: Any | None = None,
        service: str | None = None,
        embedded: bool = False,
    ):
        super().__init__()
        self.db = db_manager
        self.on_complete = on_complete_callback
        self.tray_app = tray_app

        if not embedded:
            self.setWindowFlag(Qt.WindowType.Window, True)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowTitle("Setup")
        self.resize(900, 700)
        self.setMinimumSize(780, 580)

        self._init_fields()

        # ── Layout ────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        # Sidebar — service list
        sidebar = QFrame()
        sidebar.setObjectName("card")
        sidebar.setFixedWidth(210)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 14, 12, 14)
        sidebar_layout.setSpacing(6)

        sidebar_layout.addWidget(QLabel("<b>Services</b>"))
        hint = QLabel("Pick a service to connect.\nAdd others at any time.")
        hint.setWordWrap(True)
        sidebar_layout.addWidget(hint)

        self.service_list = QListWidget()
        for _key, name, _desc in _SERVICES:
            self.service_list.addItem(f"  {name}")
        self.service_list.currentRowChanged.connect(self._on_sidebar_row_changed)
        sidebar_layout.addWidget(self.service_list, 1)
        outer.addWidget(sidebar)

        # Main content — stacked pages
        content = QFrame()
        content.setObjectName("card")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_picker_page())     # 0 — landing / picker
        self.stack.addWidget(self._build_notion_page())     # 1
        self.stack.addWidget(self._build_database_page())   # 2
        self.stack.addWidget(self._build_backups_page())    # 3
        self.stack.addWidget(self._build_clickup_page())    # 4
        self.stack.addWidget(self._build_wordpress_page())  # 5
        self.stack.addWidget(self._build_n8n_page())        # 6
        self.stack.addWidget(self._build_mautic_page())     # 7
        content_layout.addWidget(self.stack, 1)
        outer.addWidget(content, 1)

        self._service_index: dict[str, int] = {
            "notion": 1, "database": 2, "backups": 3,
            "clickup": 4, "wordpress": 5, "n8n": 6, "mautic": 7,
        }

        # Open a specific service page if requested, otherwise show the picker
        if service and service in self._service_index:
            idx = self._service_index[service]
            self.stack.setCurrentIndex(idx)
            self.service_list.setCurrentRow(list(self._service_index).index(service))
        else:
            self.stack.setCurrentIndex(0)
            self.service_list.clearSelection()

        self._refresh_sidebar_labels()

    # ── Field initialisation ──────────────────────────────────────────────

    def _init_fields(self):
        """Create all form-field widgets once so each page builder can use them."""
        saved_token = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        self.token_input = QLineEdit(saved_token)
        self.token_input.setPlaceholderText(
            "Already saved — leave as-is, or paste a new ntn_\u2026 token"
            if saved_token
            else "ntn_\u2026"
        )
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.db_input = QLineEdit(get_env("NOTION_DB_ID"))
        self.db_input.setPlaceholderText("Click \u2018Find databases\u2019 after entering your token")
        self.find_dbs_btn = QPushButton("Find databases")
        self.find_dbs_btn.clicked.connect(self.discover_databases)

        self.pg_host   = QLineEdit(get_env("PG_HOST",   "localhost"))
        self.pg_port   = QLineEdit(get_env("PG_PORT",   "5432"))
        self.pg_dbname = QLineEdit(get_env("PG_DBNAME", "notion_mirror"))
        self.pg_user   = QLineEdit(get_env("PG_USER",   "postgres"))

        self.pg_pass = QLineEdit(get_secret("PG_PASSWORD"))
        self.pg_pass.setPlaceholderText("PostgreSQL password")
        self.pg_pass.setEchoMode(QLineEdit.EchoMode.Password)

        self.generate_pg_pass_btn = QPushButton("Generate")
        self.generate_pg_pass_btn.clicked.connect(self.generate_pg_password)
        self.toggle_pg_pass_btn = QPushButton("Show")
        self.toggle_pg_pass_btn.setCheckable(True)
        self.toggle_pg_pass_btn.toggled.connect(self.toggle_pg_password_visibility)

        self.proxy_port = QLineEdit(get_env("MEDIA_PROXY_PORT", "8080"))
        backup_minutes_text = str(get_env("BACKUP_INTERVAL_MINUTES", "1440") or "1440").strip()
        try:
            backup_hours_value = max(1, round(int(backup_minutes_text) / 60))
        except ValueError:
            backup_hours_value = 24
        self.backup_interval_hours = QLineEdit(str(backup_hours_value))
        self.backup_interval_hours.setPlaceholderText("24")
        self.maintenance_time = QLineEdit(get_env("MAINTENANCE_START_TIME", "02:30"))
        self.maintenance_time.setPlaceholderText("02:30")
        self.maintenance_run_on_wake = QCheckBox(
            "Run the missed clean-up if the computer was asleep"
        )
        self.maintenance_run_on_wake.setChecked(
            str(get_env("MAINTENANCE_RUN_ON_WAKE", "0")).strip().lower()
            not in {"0", "false", "off", "no"}
        )

        self.s3_bucket     = QLineEdit(get_env("S3_BUCKET"))
        self.s3_region     = QLineEdit(get_env("S3_REGION",        "us-east-1"))
        self.s3_access_key = QLineEdit(get_env("S3_ACCESS_KEY_ID"))
        self.s3_secret_key = QLineEdit(get_secret("S3_SECRET_ACCESS_KEY"))
        self.s3_secret_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.s3_prefix = QLineEdit(get_env("S3_PREFIX", "notion-local-sync"))

        self.clickup_token = QLineEdit(get_secret("CLICKUP_TOKEN"))
        self.clickup_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.clickup_token.setPlaceholderText("pk_\u2026")
        self.clickup_team_id = str(get_env("CLICKUP_TEAM_ID", "") or "").strip()
        self.clickup_lists = QLineEdit(get_env("CLICKUP_LIST_IDS"))
        self.clickup_lists.setPlaceholderText("Click ‘Find lists’ to load them automatically, or enter IDs manually")
        self.find_clickup_lists_btn = QPushButton("Find lists")
        self.find_clickup_lists_btn.clicked.connect(self.discover_clickup_lists)

        self.wordpress_url = QLineEdit(get_env("WORDPRESS_URL"))
        self.wordpress_url.setPlaceholderText("https://example.com")
        self.wordpress_username     = QLineEdit(get_env("WORDPRESS_USERNAME"))
        self.wordpress_app_password = QLineEdit(get_secret("WORDPRESS_APP_PASSWORD"))
        self.wordpress_app_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.wordpress_app_password.setPlaceholderText("xxxx xxxx xxxx xxxx xxxx xxxx")

        self.n8n_api_url = QLineEdit(get_env("N8N_API_URL"))
        self.n8n_api_url.setPlaceholderText("https://your-n8n.example.com")
        self.n8n_api_key = QLineEdit(get_secret("N8N_API_KEY"))
        self.n8n_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.n8n_api_key.setPlaceholderText("Your n8n API key")
        self.n8n_verify_ssl = QCheckBox("Verify SSL certificates")
        self.n8n_verify_ssl.setChecked(
            str(get_env("N8N_VERIFY_SSL", "1")).strip().lower()
            not in {"0", "false", "off", "no"}
        )

        self.mautic_base_url = QLineEdit(get_env("MAUTIC_BASE_URL"))
        self.mautic_base_url.setPlaceholderText("https://your-mautic.example.com")
        self.mautic_access_token = QLineEdit(get_secret("MAUTIC_ACCESS_TOKEN"))
        self.mautic_access_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.mautic_access_token.setPlaceholderText("Already-issued bearer token (optional)")
        self.mautic_client_id = QLineEdit(get_env("MAUTIC_CLIENT_ID"))
        self.mautic_client_id.setPlaceholderText("Client ID / token key")
        self.mautic_client_secret = QLineEdit(get_secret("MAUTIC_CLIENT_SECRET"))
        self.mautic_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.mautic_client_secret.setPlaceholderText("Client secret")
        self.mautic_username = QLineEdit(get_env("MAUTIC_USERNAME"))
        self.mautic_password = QLineEdit(get_secret("MAUTIC_PASSWORD"))
        self.mautic_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.mautic_password.setPlaceholderText("Mautic password")
        self.mautic_verify_ssl = QCheckBox("Verify SSL certificates")
        self.mautic_verify_ssl.setChecked(
            str(get_env("MAUTIC_VERIFY_SSL", "1")).strip().lower()
            not in {"0", "false", "off", "no"}
        )

    # ── Sidebar helpers ───────────────────────────────────────────────────

    def _is_configured(self, key: str) -> bool:
        checks: dict[str, Callable[[], bool]] = {
            "notion":    lambda: bool(
                get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
                and get_env("NOTION_DB_ID")
            ),
            "database":  lambda: bool(get_env("PG_HOST") and get_env("PG_DBNAME")),
            "backups":   lambda: True,   # local backups are always available
            "clickup":   lambda: bool(get_secret("CLICKUP_TOKEN")),
            "wordpress": lambda: bool(get_env("WORDPRESS_URL") and get_env("WORDPRESS_USERNAME")),
            "n8n":       lambda: bool(get_env("N8N_API_URL") and get_secret("N8N_API_KEY")),
            "mautic":    lambda: bool(
                get_env("MAUTIC_BASE_URL") and (
                    get_secret("MAUTIC_ACCESS_TOKEN")
                    or (get_env("MAUTIC_CLIENT_ID") and get_secret("MAUTIC_CLIENT_SECRET"))
                    or (get_env("MAUTIC_USERNAME") and get_secret("MAUTIC_PASSWORD"))
                    or get_env("MAUTIC_SSH_HOST")
                )
            ),
        }
        try:
            return checks.get(key, lambda: False)()
        except Exception:
            return False

    def _refresh_sidebar_labels(self):
        for i, (key, name, _) in enumerate(_SERVICES):
            mark = "\u2713" if self._is_configured(key) else "\u25cb"
            self.service_list.item(i).setText(f"  {mark}  {name}")
        self._update_progress_summary()

    def _on_sidebar_row_changed(self, row: int):
        if row < 0:
            return
        key = _SERVICES[row][0]
        idx = self._service_index.get(key, 0)
        self.stack.setCurrentIndex(idx)

    def _go_to_picker(self):
        self._refresh_sidebar_labels()
        self.stack.setCurrentIndex(0)
        self.service_list.clearSelection()

    def _next_recommended_service(self) -> tuple[str | None, str]:
        ordered = ["database", "notion", "backups", "clickup", "wordpress", "n8n", "mautic"]
        labels = {key: name for key, name, _desc in _SERVICES}
        for key in ordered:
            if not self._is_configured(key):
                return key, labels.get(key, key.title())
        return None, ""

    def _update_progress_summary(self):
        if not hasattr(self, "stack"):
            return

        completed = sum(1 for key, _name, _desc in _SERVICES if self._is_configured(key))
        total = len(_SERVICES)
        next_key, next_name = self._next_recommended_service()

        if hasattr(self, "setup_progress_label"):
            self.setup_progress_label.setText(f"{completed} of {total} service areas are ready.")
        if hasattr(self, "setup_detail_label"):
            if next_key:
                self.setup_detail_label.setText(
                    f"Recommended next step: open <b>{next_name}</b> and finish that setup first."
                )
            else:
                self.setup_detail_label.setText(
                    "Everything essential looks saved. You can revisit any service at any time."
                )
        if hasattr(self, "setup_continue_btn"):
            if next_key:
                self.setup_continue_btn.setText(f"Continue with {next_name}")
                self.setup_continue_btn.setEnabled(True)
            else:
                self.setup_continue_btn.setText("Everything looks ready")
                self.setup_continue_btn.setEnabled(False)

    def _open_next_recommended_service(self):
        key, _name = self._next_recommended_service()
        if key:
            self._open_service(key)

    # ── Shared page-building helpers ──────────────────────────────────────

    def _card(self, heading: str, body_html: str | None = None) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        h = QLabel(f"<b>{heading}</b>")
        h.setObjectName("sectionTitle")
        layout.addWidget(h)
        if body_html:
            layout.addWidget(_link_label(body_html))
        return card, layout

    def _back_button(self) -> QPushButton:
        btn = QPushButton("\u2190 Back to services")
        btn.setFlat(True)
        btn.clicked.connect(self._go_to_picker)
        return btn

    def _primary_button(self, label: str, slot: Callable[..., Any]) -> QPushButton:
        btn = QPushButton(label)
        btn.setObjectName("primaryAction")
        btn.clicked.connect(slot)
        return btn

    def _wrap(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def _header(self, title: str, subtitle: str = "") -> QWidget:
        bar = QWidget()
        bar_layout = QVBoxLayout(bar)
        bar_layout.setContentsMargins(18, 14, 18, 6)
        bar_layout.setSpacing(4)
        back = self._back_button()
        back.setFixedWidth(160)
        bar_layout.addWidget(back)
        bar_layout.addWidget(QLabel(f"<h3 style='margin:0'>{title}</h3>"))
        if subtitle:
            sub = _link_label(f"<span style='color:#999'>{subtitle}</span>")
            bar_layout.addWidget(sub)
        return bar

    # ── Picker (landing) page ─────────────────────────────────────────────

    def _build_picker_page(self) -> QScrollArea:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)

        outer.addWidget(QLabel("<h2 style='margin:0'>Setup</h2>"))
        outer.addWidget(_link_label(
            "Choose a service to connect. Each one has step-by-step instructions "
            "and links to the pages where you can find the keys or passwords you need."
        ))

        progress_card = QFrame()
        progress_card.setObjectName("card")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(14, 12, 14, 12)
        progress_layout.setSpacing(6)
        progress_layout.addWidget(QLabel("<b>Recommended next step</b>"))
        self.setup_progress_label = QLabel("Checking your saved setup…")
        self.setup_progress_label.setWordWrap(True)
        progress_layout.addWidget(self.setup_progress_label)
        self.setup_detail_label = _link_label("Review the list below to continue where you left off.")
        progress_layout.addWidget(self.setup_detail_label)
        self.setup_continue_btn = self._primary_button("Continue setup", self._open_next_recommended_service)
        progress_layout.addWidget(self.setup_continue_btn, alignment=Qt.AlignLeft)
        outer.addWidget(progress_card)

        services_layout = QVBoxLayout()
        services_layout.setSpacing(8)

        for key, name, desc in _SERVICES:
            row_frame = QFrame()
            row_frame.setObjectName("card")
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(14, 10, 14, 10)
            row.setSpacing(12)

            name_lbl = QLabel(f"<b>{name}</b>")
            name_lbl.setMinimumWidth(150)
            row.addWidget(name_lbl)

            desc_lbl = _link_label(desc)
            desc_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            row.addWidget(desc_lbl, 1)

            configured = self._is_configured(key)
            state_lbl = QLabel("Ready" if configured else "Not set up")
            state_lbl.setStyleSheet("color:#55aa55;" if configured else "color:#dc2626;")
            state_lbl.setMinimumWidth(100)
            row.addWidget(state_lbl)

            btn = QPushButton("Edit \u2192" if configured else "Set up \u2192")
            if not configured:
                btn.setObjectName("primaryAction")
            btn.setFixedWidth(90)
            btn.clicked.connect(lambda _checked, k=key: self._open_service(k))
            row.addWidget(btn)

            services_layout.addWidget(row_frame)

        outer.addLayout(services_layout)
        outer.addStretch(1)
        return self._wrap(page)

    def _open_service(self, key: str):
        idx = self._service_index.get(key)
        if idx is not None:
            self.stack.setCurrentIndex(idx)
            row = list(self._service_index).index(key)
            self.service_list.setCurrentRow(row)

    # ── Notion page ───────────────────────────────────────────────────────

    def _build_notion_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "Notion",
            "Mirror your Notion databases to a local PostgreSQL database.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        how_card, how_layout = self._card("Step 1 \u2014 Create a Notion integration")
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Go to <a href='https://www.notion.so/my-integrations'>"
            "notion.so/my-integrations</a> and click <b>New integration</b>.</li>"
            "<li>Give it a name (e.g. <i>Local Sync</i>), choose your workspace, "
            "and click <b>Save</b>.</li>"
            "<li>Copy the <b>Internal Integration Token</b> \u2014 it starts with "
            "<code>ntn_</code>.</li>"
            "</ol>"
        ))
        body_layout.addWidget(how_card)

        share_card, share_layout = self._card(
            "Step 2 \u2014 Share your databases with the integration"
        )
        share_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Open each Notion database you want to sync.</li>"
            "<li>Click the <b>\u2026</b> menu in the top-right corner, then "
            "<b>Add connections</b>.</li>"
            "<li>Search for the integration you just created and select it.</li>"
            "</ol>"
            "<br><i>Tip: if \u2018Find databases\u2019 returns nothing, the integration "
            "hasn\u2019t been added to any database yet \u2014 complete this step first.</i>"
        ))
        body_layout.addWidget(share_card)

        form_card, form_layout = self._card(
            "Step 3 \u2014 Paste your token and find databases"
        )
        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        form.addRow("Integration token", self.token_input)
        db_row = QHBoxLayout()
        db_row.setSpacing(6)
        db_row.addWidget(self.db_input)
        db_row.addWidget(self.find_dbs_btn)
        form.addRow("Database to sync", db_row)
        form_layout.addLayout(form)
        body_layout.addWidget(form_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save Notion settings", self._save_notion))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── Local database page ───────────────────────────────────────────────

    def _build_database_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "Local database",
            "Connect the PostgreSQL database that stores your mirrored Notion data.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        how_card, how_layout = self._card(
            "Step 1 \u2014 Install PostgreSQL (if not already installed)"
        )
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Download and install PostgreSQL from "
            "<a href='https://www.postgresql.org/download/'>postgresql.org/download</a>.</li>"
            "<li>During setup, make a note of the <b>password</b> you set for the "
            "<code>postgres</code> user.</li>"
            "<li>The default host is <code>localhost</code> and port is "
            "<code>5432</code> \u2014 leave these as they are unless you changed them.</li>"
            "</ol>"
        ))
        body_layout.addWidget(how_card)

        conn_card, conn_layout = self._card("Step 2 \u2014 Connection details")
        conn_form = QFormLayout()
        conn_form.setHorizontalSpacing(10)
        conn_form.setVerticalSpacing(10)
        conn_form.addRow("Host",          self.pg_host)
        conn_form.addRow("Port",          self.pg_port)
        conn_form.addRow("Database name", self.pg_dbname)
        conn_form.addRow("Username",      self.pg_user)
        pg_pass_row = QHBoxLayout()
        pg_pass_row.setSpacing(6)
        pg_pass_row.addWidget(self.pg_pass)
        pg_pass_row.addWidget(self.generate_pg_pass_btn)
        pg_pass_row.addWidget(self.toggle_pg_pass_btn)
        conn_form.addRow("Password", pg_pass_row)
        conn_layout.addLayout(conn_form)
        body_layout.addWidget(conn_card)

        timing_card, timing_layout = self._card("Timing settings")
        timing_form = QFormLayout()
        timing_form.setHorizontalSpacing(10)
        timing_form.setVerticalSpacing(10)
        timing_form.addRow("Night clean-up time", self.maintenance_time)
        timing_form.addRow("Back up every (hours)", self.backup_interval_hours)
        timing_form.addRow("Media proxy port", self.proxy_port)
        timing_layout.addLayout(timing_form)
        timing_layout.addWidget(self.maintenance_run_on_wake)
        timing_layout.addWidget(_link_label(
            "<i>Example: set the clean-up time to <b>02:30</b> so heavier processing "
            "waits for overnight or the next wake-up.</i>"
        ))
        body_layout.addWidget(timing_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save & test connection", self._save_database))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── Backups page ──────────────────────────────────────────────────────

    def _build_backups_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "Backups",
            "Backups are always kept locally. Optionally mirror them to Amazon S3.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        local_card, local_layout = self._card("Local backups \u2014 always on")
        local_layout.addWidget(_link_label(
            "No setup needed. Backups are saved automatically to the local "
            "<code>data/backups/</code> folder. Leave all the S3 fields below blank "
            "if you only want local backups."
        ))
        body_layout.addWidget(local_card)

        how_card, how_layout = self._card(
            "Optional \u2014 mirror backups to Amazon S3"
        )
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Sign in to the <a href='https://console.aws.amazon.com/s3/'>AWS S3 console</a> "
            "and create a bucket (e.g. <code>my-notion-backups</code>). "
            "Note the region you choose.</li>"
            "<li>Open <a href='https://console.aws.amazon.com/iam/'>AWS IAM</a>, "
            "create a new user, and attach the <b>AmazonS3FullAccess</b> policy "
            "(or a custom policy scoped to your bucket).</li>"
            "<li>Under the user\u2019s <b>Security credentials</b> tab, click "
            "<b>Create access key</b> and copy the <b>Access key ID</b> and "
            "<b>Secret access key</b>.</li>"
            "</ol>"
        ))
        body_layout.addWidget(how_card)

        s3_card, s3_layout = self._card(
            "Amazon S3 details (leave blank for local-only backups)"
        )
        s3_form = QFormLayout()
        s3_form.setHorizontalSpacing(10)
        s3_form.setVerticalSpacing(10)
        s3_form.addRow("Bucket name",   self.s3_bucket)
        s3_form.addRow("Region",        self.s3_region)
        s3_form.addRow("Access key ID", self.s3_access_key)
        s3_form.addRow("Secret key",    self.s3_secret_key)
        s3_form.addRow("Folder prefix", self.s3_prefix)
        s3_layout.addLayout(s3_form)
        body_layout.addWidget(s3_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save backup settings", self._save_backups))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── ClickUp page ──────────────────────────────────────────────────────

    def _build_clickup_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "ClickUp",
            "Optional \u2014 connect ClickUp for task management and sync support.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        how_card, how_layout = self._card(
            "Step 1 \u2014 Find your ClickUp personal API token"
        )
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Open <a href='https://app.clickup.com/settings/apps'>"
            "ClickUp \u2192 Settings \u2192 Apps</a>.</li>"
            "<li>Under <b>API Token</b>, click <b>Generate</b> if you don\u2019t have one, "
            "or copy your existing token.</li>"
            "</ol>"
        ))
        body_layout.addWidget(how_card)

        lists_card, lists_layout = self._card("Step 2 \u2014 Find your ClickUp lists")
        lists_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Paste your ClickUp token below.</li>"
            "<li>Click <b>Find lists</b> to load every list your token can access.</li>"
            "<li>Choose one list or <b>All accessible lists</b> \u2014 just like the Notion database picker.</li>"
            "</ol>"
        ))
        body_layout.addWidget(lists_card)

        form_card, form_layout = self._card("Step 3 \u2014 Enter your ClickUp details")
        cu_form = QFormLayout()
        cu_form.setHorizontalSpacing(10)
        cu_form.setVerticalSpacing(10)
        cu_form.addRow("Personal API token", self.clickup_token)

        list_row = QWidget()
        list_row_layout = QHBoxLayout(list_row)
        list_row_layout.setContentsMargins(0, 0, 0, 0)
        list_row_layout.setSpacing(8)
        list_row_layout.addWidget(self.clickup_lists, 1)
        list_row_layout.addWidget(self.find_clickup_lists_btn)
        cu_form.addRow("Lists to sync", list_row)

        form_layout.addLayout(cu_form)
        body_layout.addWidget(form_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save ClickUp settings", self._save_clickup))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── WordPress page ────────────────────────────────────────────────────

    def _build_wordpress_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "WordPress",
            "Optional \u2014 connect your site for maintenance and content management.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        how_card, how_layout = self._card(
            "Step 1 \u2014 Create a WordPress application password"
        )
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Log in to your WordPress admin panel "
            "(<b>your-site.com/wp-admin</b>).</li>"
            "<li>Go to <b>Users \u2192 Profile</b> (or <b>Users \u2192 Edit User</b> "
            "for a different account).</li>"
            "<li>Scroll down to the <b>Application Passwords</b> section.</li>"
            "<li>Type a name for this connection (e.g. <i>Local Sync</i>) and click "
            "<b>Add New Application Password</b>.</li>"
            "<li>Copy the generated password \u2014 it looks like "
            "<code>xxxx xxxx xxxx xxxx xxxx xxxx</code>. "
            "You won\u2019t be able to see it again after closing that screen.</li>"
            "</ol>"
            "<br><i>Note: application passwords require WordPress 5.6 or later. "
            "Some security plugins may disable the feature \u2014 check your "
            "plugin settings if the section is missing.</i>"
        ))
        body_layout.addWidget(how_card)

        form_card, form_layout = self._card("Step 2 \u2014 Enter your site details")
        wp_form = QFormLayout()
        wp_form.setHorizontalSpacing(10)
        wp_form.setVerticalSpacing(10)
        wp_form.addRow("Site URL",             self.wordpress_url)
        wp_form.addRow("Username",             self.wordpress_username)
        wp_form.addRow("Application password", self.wordpress_app_password)
        form_layout.addLayout(wp_form)
        body_layout.addWidget(form_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save WordPress settings", self._save_wordpress))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── n8n page ──────────────────────────────────────────────────────────

    def _build_n8n_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "n8n",
            "Optional \u2014 connect your n8n automation server.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        how_card, how_layout = self._card("Step 1 \u2014 Create an n8n API key")
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Open your n8n instance and click your <b>user icon</b> in "
            "the bottom-left corner.</li>"
            "<li>Go to <b>Settings \u2192 API</b> and click <b>Create an API key</b>.</li>"
            "<li>Give it a label, copy the key, and save it somewhere safe \u2014 "
            "you can only see it once.</li>"
            "<li>See the "
            "<a href='https://docs.n8n.io/api/authentication/'>"
            "n8n API authentication docs</a> if you get stuck.</li>"
            "</ol>"
        ))
        body_layout.addWidget(how_card)

        url_card, url_layout = self._card("Step 2 \u2014 Find your API URL")
        url_layout.addWidget(_link_label(
            "Paste your normal n8n address here. If <code>/api/v1</code> is missing, the app adds it for you.<br>"
            "Example: <code>https://n8n.example.com</code>"
        ))
        body_layout.addWidget(url_card)

        form_card, form_layout = self._card("Step 3 \u2014 Enter your n8n details")
        n8n_form = QFormLayout()
        n8n_form.setHorizontalSpacing(10)
        n8n_form.setVerticalSpacing(10)
        n8n_form.addRow("API URL", self.n8n_api_url)
        n8n_form.addRow("API key", self.n8n_api_key)
        form_layout.addLayout(n8n_form)
        form_layout.addWidget(self.n8n_verify_ssl)
        body_layout.addWidget(form_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save n8n settings", self._save_n8n))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── Mautic page ───────────────────────────────────────────────────────

    def _build_mautic_page(self) -> QScrollArea:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        page_layout.addWidget(self._header(
            "Mautic",
            "Optional — connect your Mautic marketing workspace.",
        ))

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        how_card, how_layout = self._card("Step 1 — Find your Mautic API details")
        how_layout.addWidget(_link_label(
            "<ol style='margin:4px 0 0 0; padding-left:20px; line-height:1.7'>"
            "<li>Open your Mautic admin area and sign in.</li>"
            "<li>If Mautic shows a <b>Client ID / token</b> and <b>Client Secret</b>, use those two fields below.</li>"
            "<li>If you already have a ready-made <b>Bearer access token</b>, you can paste that instead.</li>"
            "<li>If API Basic Auth is enabled on your site, you can also use your usual <b>username</b> and <b>password</b>.</li>"
            "<li>See the <a href='https://developer.mautic.org/#authentication'>Mautic authentication guide</a> if you need help.</li>"
            "</ol>"
        ))
        body_layout.addWidget(how_card)

        url_card, url_layout = self._card("Step 2 — Find your Mautic URL")
        url_layout.addWidget(_link_label(
            "Use the main address you sign in with, for example <code>https://mautic.example.com</code>."
        ))
        body_layout.addWidget(url_card)

        form_card, form_layout = self._card("Step 3 — Enter your Mautic details")
        mautic_form = QFormLayout()
        mautic_form.setHorizontalSpacing(10)
        mautic_form.setVerticalSpacing(10)
        mautic_form.addRow("Mautic URL", self.mautic_base_url)
        mautic_form.addRow("Bearer access token (optional)", self.mautic_access_token)
        mautic_form.addRow("Client ID / token (optional)", self.mautic_client_id)
        mautic_form.addRow("Client secret (optional)", self.mautic_client_secret)
        mautic_form.addRow("Username (optional)", self.mautic_username)
        mautic_form.addRow("Password (optional)", self.mautic_password)
        form_layout.addLayout(mautic_form)
        form_layout.addWidget(_link_label(
            "Use <b>one</b> of these sign-in methods: <b>Bearer token</b>, <b>Client ID + Client secret</b>, or <b>Username + Password</b>."
        ))
        form_layout.addWidget(_link_label(
            "If Mautic gave you a <b>token + secret</b> pair, put the token in <b>Client ID / token</b> and the secret in <b>Client secret</b>."
        ))
        form_layout.addWidget(_link_label(
            "If you see a <b>401</b> after saving, the auth mode on your Mautic server likely does not match the method you entered."
        ))
        form_layout.addWidget(self.mautic_verify_ssl)
        body_layout.addWidget(form_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self._primary_button("Save Mautic settings", self._save_mautic))
        body_layout.addLayout(btn_row)
        body_layout.addStretch(1)

        page_layout.addWidget(body, 1)
        return self._wrap(page)

    # ── Save handlers ─────────────────────────────────────────────────────

    def _save_notion(self):
        token = self.token_input.text().strip()
        save_secret("NOTION_TOKEN", token, encrypted_key="NOTION_TOKEN_ENCRYPTED")

        db_ids = self.db_input.text().strip()
        if not db_ids:
            QMessageBox.warning(
                self,
                "Database needed",
                "Click \u2018Find databases\u2019 and choose one before saving.",
            )
            return

        save_env_var("NOTION_DB_ID", db_ids)

        mcp_updated = configure_claude_mcp()
        msg = (
            "Notion settings saved. The Claude connection was also refreshed \u2014 "
            "if Claude is already open, fully quit and reopen it."
            if mcp_updated
            else "Notion settings saved."
        )
        QMessageBox.information(self, "Saved", msg)
        self._update_and_return()
        self.on_complete()

    def _save_database(self):
        maintenance_time = self.maintenance_time.text().strip() or "02:30"
        try:
            pg_port = str(int(self.pg_port.text().strip() or "5432"))
            proxy_port = str(int(self.proxy_port.text().strip() or "8080"))
            backup_hours = max(1, int(self.backup_interval_hours.text().strip() or "24"))
            backup_interval = str(backup_hours * 60)
            hour_str, minute_str = maintenance_time.split(":", 1)
            maint_hour = max(0, min(int(hour_str), 23))
            maint_minute = max(0, min(int(minute_str), 59))
            maintenance_time = f"{maint_hour:02d}:{maint_minute:02d}"
        except ValueError:
            QMessageBox.warning(
                self,
                "Check the fields",
                "Ports and backup hours must be whole numbers, and the clean-up "
                "time should look like 02:30.",
            )
            return

        save_env_var("PG_HOST", self.pg_host.text().strip() or "localhost")
        save_env_var("PG_PORT", pg_port)
        save_env_var("PG_DBNAME", self.pg_dbname.text().strip() or "notion_mirror")
        save_env_var("PG_USER", self.pg_user.text().strip() or "postgres")
        save_secret("PG_PASSWORD", self.pg_pass.text().strip())
        save_env_var("MEDIA_PROXY_PORT", proxy_port)
        save_env_var("MAINTENANCE_RUNS_NIGHT_ONLY", "1")
        save_env_var("MAINTENANCE_START_TIME", maintenance_time)
        save_env_var("MAINTENANCE_WINDOW_HOURS", "4")
        save_env_var("MAINTENANCE_WINDOW_START_HOUR", str(maint_hour))
        save_env_var("MAINTENANCE_WINDOW_END_HOUR", str((maint_hour + 4) % 24))
        save_env_var("MAINTENANCE_RUN_ON_WAKE",
                     "1" if self.maintenance_run_on_wake.isChecked() else "0")
        save_env_var("BACKUP_INTERVAL_MINUTES", backup_interval)
        save_env_var("BACKUP_MODE", "full")

        if self.db.connect():
            QMessageBox.information(
                self,
                "Connected",
                "Database settings saved and the connection was tested successfully.",
            )
            self._update_and_return()
            self.on_complete()
        else:
            logger.error("Could not connect to PostgreSQL after saving setup values.")
            error_text = getattr(self.db, "last_error", "")
            if "Connection refused" in error_text or "could not connect to server" in error_text:
                msg = (
                    "PostgreSQL is not running at that host and port. "
                    "For a local install, check that the service is started, "
                    "use localhost and port 5432, and try again."
                )
            elif "password authentication failed" in error_text:
                msg = "PostgreSQL rejected the password. Check the password for that user and try again."
            else:
                msg = (
                    "Could not connect to PostgreSQL. Double-check the host, port, "
                    "username, and password, and make sure the server is running."
                )
            QMessageBox.critical(self, "Connection failed", msg)

    def _save_backups(self):
        save_env_var("BACKUP_MODE",      "full")
        save_env_var("BACKUP_PROVIDER",  "s3" if self.s3_bucket.text().strip() else "local")
        save_env_var("S3_BUCKET",        self.s3_bucket.text().strip())
        save_env_var("S3_REGION",        self.s3_region.text().strip()     or "us-east-1")
        save_env_var("S3_ACCESS_KEY_ID", self.s3_access_key.text().strip())
        save_secret("S3_SECRET_ACCESS_KEY", self.s3_secret_key.text().strip())
        save_env_var("S3_PREFIX",        self.s3_prefix.text().strip()     or "notion-local-sync")
        QMessageBox.information(
            self,
            "Saved",
            "Backup settings saved."
            if self.s3_bucket.text().strip()
            else "Backup settings saved \u2014 using local backups only.",
        )
        self._update_and_return()
        self.on_complete()

    def _save_clickup(self):
        token = self.clickup_token.text().strip()
        list_setting = self.clickup_lists.text().strip()
        auto_note = ""

        if token and not list_setting:
            list_setting = "ALL"
            self.clickup_lists.setText(list_setting)
            auto_note = " All accessible ClickUp lists will be discovered automatically during sync."

        save_secret("CLICKUP_TOKEN", token)
        save_env_var("CLICKUP_LIST_IDS", list_setting)
        save_env_var("CLICKUP_TEAM_ID", self.clickup_team_id)
        QMessageBox.information(self, "Saved", f"ClickUp settings saved.{auto_note}")
        self._update_and_return()
        self.on_complete()

    def _save_wordpress(self):
        save_env_var("WORDPRESS_URL",      self.wordpress_url.text().strip())
        save_env_var("WORDPRESS_USERNAME", self.wordpress_username.text().strip())
        save_secret("WORDPRESS_APP_PASSWORD", self.wordpress_app_password.text().strip())
        QMessageBox.information(self, "Saved", "WordPress settings saved.")
        self._update_and_return()
        self.on_complete()

    def _save_n8n(self):
        api_url = N8nSyncEngine.normalize_base_url(self.n8n_api_url.text().strip())
        api_key = self.n8n_api_key.text().strip()
        verify_ssl = self.n8n_verify_ssl.isChecked()

        if api_url:
            self.n8n_api_url.setText(api_url)

        save_env_var("N8N_API_URL", api_url)
        save_secret("N8N_API_KEY", api_key)
        save_env_var("N8N_VERIFY_SSL", "1" if verify_ssl else "0")

        is_valid, detail = self._validate_n8n_settings(api_url, api_key, verify_ssl)
        self._show_validation_result("n8n", is_valid, detail)
        self._update_and_return()
        self.on_complete()

    def _save_mautic(self):
        base_url = self.mautic_base_url.text().strip()
        access_token = self.mautic_access_token.text().strip()
        client_id = self.mautic_client_id.text().strip()
        client_secret = self.mautic_client_secret.text().strip()
        username = self.mautic_username.text().strip()
        password = self.mautic_password.text().strip()

        if not base_url:
            QMessageBox.warning(self, "Mautic URL needed", "Add your Mautic URL before saving.")
            return

        has_bearer_token = bool(access_token)
        has_client_pair = bool(client_id and client_secret)
        has_login = bool(username and password)

        if not (has_bearer_token or has_client_pair or has_login):
            QMessageBox.warning(
                self,
                "Sign-in details needed",
                "Add a Bearer token, a Client ID + Client secret pair, or your username and password before saving.",
            )
            return

        if (client_id and not client_secret) or (client_secret and not client_id):
            QMessageBox.warning(
                self,
                "Complete the pair",
                "If you use the Mautic token + secret method, fill in both the Client ID / token and Client secret fields.",
            )
            return

        save_env_var("MAUTIC_BASE_URL", base_url)
        save_secret("MAUTIC_ACCESS_TOKEN", access_token)
        save_env_var("MAUTIC_CLIENT_ID", client_id)
        save_secret("MAUTIC_CLIENT_SECRET", client_secret)
        save_env_var("MAUTIC_USERNAME", username)
        save_secret("MAUTIC_PASSWORD", password)
        verify_ssl = self.mautic_verify_ssl.isChecked()
        save_env_var("MAUTIC_VERIFY_SSL", "1" if verify_ssl else "0")

        is_valid, detail = self._validate_mautic_settings(
            base_url=base_url,
            access_token=access_token,
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        )
        self._show_validation_result("Mautic", is_valid, detail)
        self._update_and_return()
        self.on_complete()

    def _show_validation_result(self, service_name: str, is_valid: bool, detail: str):
        base_message = f"{service_name} settings saved"
        detail_text = str(detail or "").strip()
        if is_valid:
            message = f"{base_message} and validated."
            if detail_text:
                message = f"{message}\n\n{detail_text}"
            QMessageBox.information(self, "Saved and valid", message)
            return

        message = f"{base_message}, but validation failed."
        if detail_text:
            message = f"{message}\n\n{detail_text}"
        QMessageBox.warning(self, "Saved but not valid", message)

    def _validate_n8n_settings(self, api_url: str, api_key: str, verify_ssl: bool) -> tuple[bool, str]:
        cleaned_url = N8nSyncEngine.normalize_base_url(api_url)
        cleaned_key = str(api_key or "").strip()
        if not cleaned_url or not cleaned_key:
            return False, "Add both the n8n API URL and API key before validating."

        session = build_retry_session(user_agent="NotionLocalSync-SetupValidation/2.0")
        session.headers.update({"X-N8N-API-KEY": cleaned_key})

        try:
            response = session.get(f"{cleaned_url}/workflows?limit=1", timeout=15, verify=verify_ssl)
            if response.status_code == 200:
                return True, "n8n accepted the API key."
            if response.status_code in {401, 403}:
                return False, "n8n rejected the API key. Check the key and permissions."
            return False, f"n8n returned HTTP {response.status_code}."
        except requests.RequestException as exc:
            return False, f"Could not reach n8n: {exc}"

    def _validate_mautic_settings(
        self,
        base_url: str,
        access_token: str,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        verify_ssl: bool,
    ) -> tuple[bool, str]:
        cleaned_base_url = str(base_url or "").strip().rstrip("/")
        cleaned_token = str(access_token or "").strip()
        cleaned_client_id = str(client_id or "").strip()
        cleaned_client_secret = str(client_secret or "").strip()
        cleaned_username = str(username or "").strip()
        cleaned_password = str(password or "").strip()

        if not cleaned_base_url:
            return False, "Add your Mautic URL before validating."

        if (cleaned_client_id and not cleaned_client_secret) or (cleaned_client_secret and not cleaned_client_id):
            return False, "Add both the Client ID / token and Client secret, or use one of the other sign-in methods."

        if not cleaned_token and not (cleaned_client_id and cleaned_client_secret) and not (cleaned_username and cleaned_password):
            return False, "Add a Bearer token, a Client ID + Client secret pair, or your username and password before validating."

        session = build_retry_session(user_agent="NotionLocalSync-SetupValidation/2.0")
        contacts_url = f"{cleaned_base_url}/api/contacts?limit=1"

        def _check_contacts(*, bearer_token: str | None = None, auth: tuple[str, str] | None = None, success_message: str) -> tuple[bool, str]:
            headers = {"Accept": "application/json"}
            if bearer_token:
                headers["Authorization"] = f"Bearer {bearer_token}"

            response = session.get(
                contacts_url,
                headers=headers,
                auth=auth,
                timeout=15,
                verify=verify_ssl,
            )
            if response.status_code == 200:
                return True, success_message
            if response.status_code in {401, 403}:
                return False, (
                    "Mautic rejected the credentials (401/403). "
                    "The API auth mode on the server likely does not match the method you entered."
                )
            if response.status_code == 404:
                return False, "Mautic could not find the API endpoint. Check the site URL and make sure API access is enabled in Mautic."
            return False, f"Mautic returned HTTP {response.status_code}."

        try:
            if cleaned_token:
                return _check_contacts(
                    bearer_token=cleaned_token,
                    success_message="Mautic accepted the Bearer token.",
                )

            if cleaned_client_id and cleaned_client_secret:
                token_response = session.post(
                    f"{cleaned_base_url}/oauth/v2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": cleaned_client_id,
                        "client_secret": cleaned_client_secret,
                    },
                    headers={"Accept": "application/json"},
                    timeout=15,
                    verify=verify_ssl,
                )
                if token_response.status_code in {401, 403}:
                    return False, "Mautic rejected the Client ID and secret. Double-check both values and confirm the API credentials are enabled."
                if token_response.status_code == 404:
                    return False, "Mautic could not find the OAuth token endpoint. Check the site URL and make sure API access is enabled."
                if token_response.status_code != 200:
                    return False, f"Mautic returned HTTP {token_response.status_code} while requesting an OAuth token."

                try:
                    token_payload = token_response.json() or {}
                except ValueError:
                    token_payload = {}

                issued_access_token = str(token_payload.get("access_token") or "").strip()
                if not issued_access_token:
                    return False, "Mautic accepted the Client ID and secret, but did not return an access token."

                return _check_contacts(
                    bearer_token=issued_access_token,
                    success_message="Mautic accepted the Client ID and secret.",
                )

            return _check_contacts(
                auth=(cleaned_username, cleaned_password),
                success_message="Mautic accepted the sign-in details.",
            )
        except requests.RequestException as exc:
            return False, f"Could not reach Mautic: {exc}"

    def _update_and_return(self):
        """Refresh sidebar status labels and return to the service picker."""
        self._refresh_sidebar_labels()
        self._go_to_picker()

    # ── Utility helpers ───────────────────────────────────────────────────

    def _build_suggested_password(self, length: int = 20) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def generate_pg_password(self):
        self.pg_pass.setText(self._build_suggested_password())
        self.pg_pass.setFocus()
        self.pg_pass.selectAll()

    def toggle_pg_password_visibility(self, checked: bool):
        self.pg_pass.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self.toggle_pg_pass_btn.setText("Hide" if checked else "Show")

    def _discover_clickup_lists_from_token(self, token: str) -> list[dict[str, Any]]:
        engine = ClickUpSyncEngine(db_manager=None)
        engine.api_token = str(token or "").strip()
        engine.team_id = self.clickup_team_id
        engine.http.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": engine.api_token,
        })

        discovered_lists = engine.discover_accessible_lists()
        team_ids = {str(item.get("team_id") or "").strip() for item in discovered_lists if str(item.get("team_id") or "").strip()}
        if len(team_ids) == 1:
            self.clickup_team_id = next(iter(team_ids))
        return discovered_lists

    @staticmethod
    def _format_clickup_list_choice(item: dict[str, Any]) -> str:
        pieces = [
            str(item.get("team_name") or "").strip(),
            str(item.get("space_name") or "").strip(),
            str(item.get("folder_name") or "").strip(),
            str(item.get("name") or "Untitled list").strip() or "Untitled list",
        ]
        path_text = " / ".join(piece for piece in pieces if piece)
        return f"{path_text} ({str(item.get('id') or '').strip()})"

    def discover_clickup_lists(self):
        token = self.clickup_token.text().strip() or get_secret("CLICKUP_TOKEN")
        if not token:
            QMessageBox.information(
                self,
                "Token needed",
                "Paste your ClickUp API token first, then click Find lists.",
            )
            return

        try:
            lists = self._discover_clickup_lists_from_token(token)
            if not lists:
                QMessageBox.information(
                    self,
                    "No lists found",
                    "No accessible ClickUp lists were found for that token yet.",
                )
                return

            if len(lists) == 1:
                list_setting = lists[0]["id"]
            else:
                choices = [f"All accessible lists ({len(lists)})"] + [self._format_clickup_list_choice(item) for item in lists]
                selection, ok = QInputDialog.getItem(
                    self,
                    "Choose a ClickUp list",
                    "Select one list to sync, or choose all accessible lists:",
                    choices,
                    0,
                    False,
                )
                if not ok or not selection:
                    return
                list_setting = (
                    "ALL"
                    if selection.startswith("All accessible lists")
                    else selection.rsplit("(", 1)[-1].rstrip(")")
                )

            self.clickup_lists.setText(list_setting)

        except Exception as exc:
            logger.error(f"Failed to discover ClickUp lists: {exc}")
            QMessageBox.critical(
                self,
                "Search failed",
                f"ClickUp rejected the list lookup: {exc}",
            )

    def discover_databases(self):
        token = self.token_input.text().strip()
        if not token:
            token = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        if not token:
            QMessageBox.information(
                self,
                "Token needed",
                "Paste your Notion integration token first, then click Find databases.",
            )
            return

        try:
            client = Client(auth=token)
            results: list[dict[str, Any]] = []
            next_cursor = None

            while True:
                search_args: dict[str, Any] = {
                    "filter": {"property": "object", "value": "data_source"},
                    "page_size": 100,
                }
                if next_cursor:
                    search_args["start_cursor"] = next_cursor
                response = cast(dict[str, Any], client.search(**search_args))
                results.extend(response.get("results", []))
                if not response.get("has_more"):
                    break
                next_cursor = response.get("next_cursor")

            databases: list[tuple[str, str]] = []
            for item in results:
                title = (
                    "".join(p.get("plain_text", "") for p in item.get("title", [])).strip()
                    or "Untitled database"
                )
                databases.append((title, item["id"]))
            databases.sort(key=lambda x: x[0].lower())

            if not databases:
                QMessageBox.information(
                    self,
                    "No databases found",
                    "No shared databases were found. In Notion, open each database, "
                    "click the \u2026 menu \u2192 Add connections, add your integration, "
                    "and try again.",
                )
                return

            if len(databases) == 1:
                db_ids = databases[0][1]
            else:
                choices = (
                    [f"All accessible databases ({len(databases)})"]
                    + [f"{t} ({i})" for t, i in databases]
                )
                selection, ok = QInputDialog.getItem(
                    self,
                    "Choose a database",
                    "Select one database to sync, or choose all accessible databases:",
                    choices,
                    0,
                    False,
                )
                if not ok or not selection:
                    return
                db_ids = (
                    "ALL"
                    if selection.startswith("All accessible databases")
                    else selection.rsplit("(", 1)[-1].rstrip(")")
                )

            self.db_input.setText(db_ids)

        except Exception as exc:
            logger.error(f"Failed to discover Notion databases: {exc}")
            QMessageBox.critical(
                self,
                "Search failed",
                f"Notion rejected the database search: {exc}",
            )
