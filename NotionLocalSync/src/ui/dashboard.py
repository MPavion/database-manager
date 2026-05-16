import html as _html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.config import get_app_display_name, get_env


class ClickableLabel(QLabel):
    """A label that emits a clicked signal when the user clicks it."""
    clicked = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ActivityLogDialog(QDialog):
    """Shows the full activity log as a scrollable table."""

    def __init__(self, tray_app, parent=None):
        super().__init__(parent)
        self.tray_app = tray_app
        self.setWindowTitle("Activity Log")
        self.resize(920, 520)
        self.setMinimumSize(640, 380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["When", "Activity", "Result", "Summary"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_entries()

    def _load_entries(self):
        entries = (
            self.tray_app.get_activity_log(limit=200)
            if hasattr(self.tray_app, "get_activity_log")
            else []
        )
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            created_at = entry.get("created_at")
            when = (
                created_at.strftime("%Y-%m-%d %H:%M:%S")
                if hasattr(created_at, "strftime")
                else str(created_at or "—")
            )
            action_label = (
                entry.get("action_label")
                or str(entry.get("action") or "Event").replace("_", " ").title()
            )
            status = str(entry.get("status") or "—").capitalize()
            summary = str(entry.get("summary") or "—")

            self.table.setItem(row, 0, QTableWidgetItem(when))
            self.table.setItem(row, 1, QTableWidgetItem(action_label))
            self.table.setItem(row, 2, QTableWidgetItem(status))
            self.table.setItem(row, 3, QTableWidgetItem(summary))


class BulkReplaceDialog(QDialog):
    def __init__(self, tray_app, parent=None):
        super().__init__(parent)
        self.tray_app = tray_app
        self.setWindowTitle("Bulk search & replace")
        self.resize(620, 430)
        self.setMinimumSize(560, 380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        intro = QLabel(
            "Run a large local search-and-replace in the database, then push the reviewed changes to Notion when ready."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)

        form.addWidget(QLabel("Find"), 0, 0)
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("Text to search for")
        form.addWidget(self.find_input, 0, 1)

        form.addWidget(QLabel("Replace with"), 1, 0)
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Replacement text")
        form.addWidget(self.replace_input, 1, 1)

        field_row = QHBoxLayout()
        self.title_check = QCheckBox("Titles")
        self.title_check.setChecked(True)
        self.summary_check = QCheckBox("Notes / summaries")
        self.summary_check.setChecked(True)
        self.case_check = QCheckBox("Match case")
        field_row.addWidget(self.title_check)
        field_row.addWidget(self.summary_check)
        field_row.addWidget(self.case_check)
        field_row.addStretch(1)
        form.addLayout(field_row, 2, 0, 1, 2)

        layout.addLayout(form)

        preview_row = QHBoxLayout()
        self.preview_btn = QPushButton("Preview matches")
        self.preview_btn.clicked.connect(self.run_preview)
        preview_row.addWidget(self.preview_btn)

        self.apply_btn = QPushButton("Apply locally")
        self.apply_btn.setObjectName("primaryAction")
        self.apply_btn.clicked.connect(self.apply_changes)
        preview_row.addWidget(self.apply_btn)
        preview_row.addStretch(1)
        layout.addLayout(preview_row)

        self.results_box = QPlainTextEdit()
        self.results_box.setReadOnly(True)
        self.results_box.setPlaceholderText("Preview results will appear here.")
        layout.addWidget(self.results_box, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _selected_fields(self) -> list[str]:
        fields = []
        if self.title_check.isChecked():
            fields.append("title")
        if self.summary_check.isChecked():
            fields.append("ai_summary")
        return fields or ["title", "ai_summary"]

    def run_preview(self):
        preview = self.tray_app.preview_bulk_replace(
            self.find_input.text(),
            self.replace_input.text(),
            fields=self._selected_fields(),
            case_sensitive=self.case_check.isChecked(),
        )

        if not preview.get("search_text"):
            self.results_box.setPlainText("Type the text you want to find first.")
            return

        lines = [
            f"Affected pages: {preview.get('affected_pages', 0)}",
            f"Total matches: {preview.get('total_matches', 0)}",
            "",
        ]
        for item in preview.get("matches", []):
            changed_fields = ", ".join(item.get("changed_fields", [])) or "unknown fields"
            lines.append(
                f"- {item.get('title', 'Untitled')} ({item.get('notion_id', '')}) — {item.get('match_count', 0)} match(es) in {changed_fields}"
            )

        if not preview.get("matches"):
            lines.append("No matches found in the local mirror.")

        self.results_box.setPlainText("\n".join(lines))

    def apply_changes(self):
        find_text = self.find_input.text().strip()
        if not find_text:
            QMessageBox.information(self, "Find text required", "Type the text you want to replace first.")
            return

        preview = self.tray_app.preview_bulk_replace(
            find_text,
            self.replace_input.text(),
            fields=self._selected_fields(),
            case_sensitive=self.case_check.isChecked(),
        )
        affected_pages = int(preview.get("affected_pages", 0) or 0)
        total_matches = int(preview.get("total_matches", 0) or 0)

        confirm = QMessageBox.question(
            self,
            "Apply bulk replace locally?",
            f"This will queue {total_matches} replacement(s) across {affected_pages} page(s) in the local mirror.\n\nYou can review the result and push later. Continue?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return

        success, message = self.tray_app.apply_bulk_replace(
            find_text,
            self.replace_input.text(),
            fields=self._selected_fields(),
            case_sensitive=self.case_check.isChecked(),
        )
        self.results_box.setPlainText(message)
        if success:
            QMessageBox.information(self, "Bulk replace queued", message)
        else:
            QMessageBox.warning(self, "Bulk replace failed", message)


class AdminDashboard(QWidget):
    def __init__(self, tray_app):
        super().__init__()
        self.tray_app = tray_app

        self.setWindowFlag(Qt.Window, True)
        self.setMinimumSize(760, 460)

        layout = QVBoxLayout()

        self.header_label = QLabel()
        self.header_label.setWordWrap(True)
        layout.addWidget(self.header_label)

        action_group = QGroupBox("Quick actions")
        action_layout = QVBoxLayout()

        top_row = QHBoxLayout()
        self.sync_btn = QPushButton("Run Smart Sync")
        self.sync_btn.clicked.connect(lambda: self.tray_app.run_sync("sync"))
        top_row.addWidget(self.sync_btn)

        self.pull_btn = QPushButton("Import from Notion only")
        self.pull_btn.clicked.connect(lambda: self.tray_app.run_sync("pull"))
        top_row.addWidget(self.pull_btn)

        self.push_btn = QPushButton("Push local changes only")
        self.push_btn.clicked.connect(lambda: self.tray_app.run_sync("push"))
        top_row.addWidget(self.push_btn)
        action_layout.addLayout(top_row)

        middle_row = QHBoxLayout()
        self.browser_btn = QPushButton("Browse local database")
        self.browser_btn.clicked.connect(self.open_database_browser)
        middle_row.addWidget(self.browser_btn)

        self.local_edit_btn = QPushButton("Queue local edit")
        self.local_edit_btn.clicked.connect(self.queue_local_edit)
        middle_row.addWidget(self.local_edit_btn)


        self.mcp_btn = QPushButton("Configure Claude MCP")
        self.mcp_btn.clicked.connect(self.tray_app.configure_mcp)
        middle_row.addWidget(self.mcp_btn)
        action_layout.addLayout(middle_row)

        # Notion tab/section for remote housekeeping
        notion_group = QGroupBox("Notion")
        notion_layout = QVBoxLayout()
        self.notion_housekeeping_btn = QPushButton("Run remote Notion housekeeping")
        self.notion_housekeeping_btn.clicked.connect(self.tray_app.run_notion_housekeeping)
        notion_layout.addWidget(self.notion_housekeeping_btn)
        notion_group.setLayout(notion_layout)
        layout.addWidget(notion_group)


        support_row = QHBoxLayout()
        self.backup_btn = QPushButton("Run backup now")
        self.backup_btn.clicked.connect(lambda: self.tray_app.run_backup_now(silent=False))
        support_row.addWidget(self.backup_btn)

        self.health_btn = QPushButton("Run health check")
        self.health_btn.clicked.connect(self.tray_app.run_health_check)
        support_row.addWidget(self.health_btn)

        self.maintenance_btn = QPushButton("Run clean-up now")
        self.maintenance_btn.clicked.connect(lambda: self.tray_app.run_manual_maintenance())
        support_row.addWidget(self.maintenance_btn)

        self.bulk_replace_btn = QPushButton("Bulk search & replace")
        self.bulk_replace_btn.clicked.connect(self.open_bulk_replace)
        support_row.addWidget(self.bulk_replace_btn)
        action_layout.addLayout(support_row)

        support_help = QLabel(
            "<b>What these do:</b><br>"
            "• <b>Run health check:</b> checks your connections, local database, and backup or clean-up readiness without changing your content.<br>"
            "• <b>Run clean-up now:</b> runs the built-in housekeeping jobs, like deduping and clearing old leftovers based on your settings.<br>"
            "• <b>Run backup now:</b> creates a fresh restore point before bigger changes."
        )
        support_help.setWordWrap(True)
        support_help.setStyleSheet("color: #94a3b8;")
        action_layout.addWidget(support_help)

        # Advanced Tools section
        advanced_group = QGroupBox("Advanced Tools")
        advanced_layout = QVBoxLayout()
        self.vacuum_btn = QPushButton("Run VACUUM ANALYZE (DB speed)")
        self.vacuum_btn.clicked.connect(self.tray_app.run_vacuum_analyze)
        advanced_layout.addWidget(self.vacuum_btn)
        advanced_group.setLayout(advanced_layout)
        layout.addWidget(advanced_group)

        bottom_row = QHBoxLayout()
        self.settings_btn = QPushButton("Setup")
        self.settings_btn.clicked.connect(self.tray_app.show_wizard)
        bottom_row.addWidget(self.settings_btn)

        self.logs_btn = QPushButton("Open logs")
        self.logs_btn.clicked.connect(self.tray_app.open_logs)
        bottom_row.addWidget(self.logs_btn)

        self.access_btn = QPushButton("Check Notion access")
        self.access_btn.clicked.connect(self.tray_app.check_notion_access)
        bottom_row.addWidget(self.access_btn)

        self.statistics_btn = QPushButton("Statistics")
        self.statistics_btn.clicked.connect(self.show_statistics)
        bottom_row.addWidget(self.statistics_btn)

        self.refresh_btn = QPushButton("Refresh status")
        self.refresh_btn.clicked.connect(self.refresh_status)
        bottom_row.addWidget(self.refresh_btn)
        action_layout.addLayout(bottom_row)

        action_group.setLayout(action_layout)
        layout.addWidget(action_group)

        self.connection_label = QLabel()
        self.target_label = QLabel()
        self.records_label = QLabel()
        self.pending_label = QLabel()
        self.last_sync_label = QLabel()
        self.access_label = QLabel()
        self.db_connections_label = QLabel()
        self.db_connections_label.setTextFormat(Qt.RichText)
        self.db_connections_label.setOpenExternalLinks(False)
        self.mcp_label = QLabel()
        from PySide6.QtWidgets import QProgressBar, QFrame
        self.summary_frame = QFrame()
        self.summary_frame.setFrameShape(QFrame.StyledPanel)
        self.summary_layout = QVBoxLayout(self.summary_frame)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_layout.addWidget(self.summary_label)
        # Add score/progress bars for key metrics
        self.sync_bar = QProgressBar()
        self.sync_bar.setFormat('Sync completion: %p%')
        self.backup_bar = QProgressBar()
        self.backup_bar.setFormat('Backup health: %p%')
        self.maintenance_bar = QProgressBar()
        self.maintenance_bar.setFormat('Maintenance: %p%')
        self.summary_layout.addWidget(self.sync_bar)
        self.summary_layout.addWidget(self.backup_bar)
        self.summary_layout.addWidget(self.maintenance_bar)
        self.status_label = ClickableLabel("Status: Ready — click for full log")
        self.status_label.setStyleSheet(
            "font-weight: bold; font-size: 16px; padding: 4px 0;"
            " color: #1e40af; text-decoration: underline;"
        )
        self.status_label.clicked.connect(self._open_activity_log)

        for label in (
            self.connection_label,
            self.target_label,
            self.records_label,
            self.pending_label,
            self.last_sync_label,
            self.access_label,
            self.db_connections_label,
            self.mcp_label,
            self.status_label,
        ):
            label.setWordWrap(True)
            layout.addWidget(label)
        layout.addWidget(self.summary_frame)

        layout.addStretch(1)
        self.setLayout(layout)
        self.apply_branding()
        self.refresh_status()

    def _build_db_connections_html(self) -> str:
        databases: list[dict] = []
        unlinked: list[dict] = []
        if hasattr(self.tray_app, "get_notion_database_cache"):
            databases, unlinked = self.tray_app.get_notion_database_cache()

        if not databases and not unlinked:
            return (
                "<span style='color:#999;'><i>Database list not loaded yet — "
                "click \"Check Notion access\" to see which databases are shared.</i></span>"
            )

        target_raw = get_env("NOTION_DB_ID", "") or ""
        all_flag = target_raw.strip().upper() == "ALL"
        configured_ids: set[str] = set()
        if not all_flag and target_raw.strip():
            configured_ids = {p.strip() for p in target_raw.split(",") if p.strip()}

        parts: list[str] = []

        if databases:
            parts.append("<b>Databases connected to this integration:</b>")
            for db in databases:
                db_id = db.get("id", "")
                title = _html.escape(db.get("title") or "Untitled")
                is_target = all_flag or db_id in configured_ids
                tag = " <span style='color:#22c55e;'>(syncing)</span>" if is_target else ""
                parts.append(f"&nbsp;&nbsp;• {title}{tag}")
        else:
            parts.append("<span style='color:#999;'><i>No databases are currently shared with this integration.</i></span>")

        if unlinked:
            parts.append("")
            parts.append("<b>Referenced but not yet connected:</b>")
            for item in unlinked:
                db_id = _html.escape(item.get("id", ""))
                source = _html.escape(item.get("source_title") or "unknown")
                prop = _html.escape(item.get("property_name") or "unknown")
                parts.append(
                    f"<span style='color:#aaa;'><i>&nbsp;&nbsp;• {db_id} — "
                    f"linked from <b>{source}</b> via &ldquo;{prop}&rdquo; — "
                    f"add to your Notion integration to sync it</i></span>"
                )

        return "<br>".join(parts)

    def apply_branding(self):
        app_name = get_app_display_name()
        self.setWindowTitle(f"{app_name} Dashboard")
        self.header_label.setText(
            f"<h2>{app_name}</h2>"
            "<p>Use these controls to sync, browse your local mirror, run maintenance for Notion, ClickUp, and WordPress, and reopen setup when needed.</p>"
        )

    @staticmethod
    def _format_stat_timestamp(value) -> str:
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value) if value else "Never"

    def _format_timestamp(self, value) -> str:
        return self._format_stat_timestamp(value)

    @staticmethod
    def build_statistics_summary(stats: dict) -> str:
        if not stats or not stats.get("connected"):
            return "The local PostgreSQL mirror is not connected yet."

        lines = [
            f"<b>Database size:</b> {stats.get('database_size_text', 'Not available yet')}",
            f"<b>Active pages:</b> {int(stats.get('active_pages', 0) or 0)}",
            f"<b>Saved versions:</b> {int(stats.get('total_versions', 0) or 0)}",
            f"<b>Pages with media:</b> {int(stats.get('pages_with_media', 0) or 0)}",
            f"<b>Pending push:</b> {int(stats.get('pending_push', 0) or 0)}",
            f"<b>Last pull:</b> {AdminDashboard._format_stat_timestamp(stats.get('last_pull_at'))}",
            f"<b>Last push:</b> {AdminDashboard._format_stat_timestamp(stats.get('last_push_at'))}",
            f"<b>Latest local update:</b> {AdminDashboard._format_stat_timestamp(stats.get('latest_local_update'))}",
            f"<b>Latest Notion update seen:</b> {AdminDashboard._format_stat_timestamp(stats.get('latest_source_update'))}",
        ]
        return "<br>".join(lines)

    def set_busy(self, busy: bool):
        for button in (
            self.sync_btn,
            self.pull_btn,
            self.push_btn,
            self.browser_btn,
            self.local_edit_btn,
            self.mcp_btn,
            self.backup_btn,
            self.health_btn,
            self.maintenance_btn,
            self.bulk_replace_btn,
            self.settings_btn,
            self.logs_btn,
            self.access_btn,
            self.statistics_btn,
        ):
            button.setEnabled(not busy)
        self.refresh_btn.setEnabled(True)

    def show_statistics(self):
        stats = self.tray_app.get_runtime_stats() if hasattr(self.tray_app, "get_runtime_stats") else (self.tray_app.db.get_stats() if self.tray_app.db else {})

        dialog = QMessageBox(self)
        dialog.setWindowTitle("Statistics")
        dialog.setIcon(QMessageBox.Information)
        dialog.setText("Local database statistics")
        dialog.setInformativeText(self.build_statistics_summary(stats))
        dialog.setTextFormat(Qt.RichText)
        dialog.exec()

    def open_database_browser(self):
        if hasattr(self.tray_app, "show_database_browser"):
            self.tray_app.show_database_browser()
        else:
            self.show_statistics()

    def open_bulk_replace(self):
        dialog = BulkReplaceDialog(self.tray_app, self)
        dialog.exec()
        self.refresh_status()

    def queue_local_edit(self):
        if not self.tray_app.db.conn and not self.tray_app.db.connect():
            QMessageBox.warning(self, "Database not ready", "Connect to PostgreSQL first, then try queuing the local edit again.")
            return

        pages = self.tray_app.db.list_editable_pages()
        if not pages:
            QMessageBox.information(self, "Nothing to edit yet", "There are no mirrored pages yet. Run Import from Notion first.")
            return

        options = []
        page_lookup = {}
        for page in pages:
            title = (page.get("title") or "Untitled").strip() or "Untitled"
            notion_id = page.get("notion_id", "")
            suffix = " — pending push" if page.get("needs_push") else ""
            label = f"{title} ({notion_id}){suffix}"
            options.append(label)
            page_lookup[label] = page

        selection, ok = QInputDialog.getItem(
            self,
            "Choose a local page",
            "Pick the mirrored page you want to edit locally:",
            options,
            0,
            False,
        )
        if not ok or not selection:
            return

        selected_page = page_lookup[selection]
        new_title, ok = QInputDialog.getText(
            self,
            "Edit local title",
            "Title to store locally and push back on the next sync:",
            text=selected_page.get("title") or "Untitled",
        )
        if not ok:
            return

        new_summary, ok = QInputDialog.getMultiLineText(
            self,
            "Edit local summary",
            "Optional local notes / summary to queue for the next push:",
            selected_page.get("ai_summary") or "",
        )
        if not ok:
            return

        self.tray_app.queue_local_edit(selected_page.get("notion_id", ""), new_title, new_summary)

    def update_activity_status(self, message: str):
        """Update the status bar with the latest activity (single-line live log)."""
        self.status_label.setText(f"{message} — click for full log")

    def set_status(self, message: str):
        self.update_activity_status(message)

    def _open_activity_log(self):
        dialog = ActivityLogDialog(self.tray_app, self)
        dialog.exec()

    def refresh_status(self):
        host = get_env("PG_HOST", "localhost")
        port = get_env("PG_PORT", "5432")
        dbname = get_env("PG_DBNAME", "notion_mirror")
        interval = get_env("SYNC_INTERVAL_MINUTES", "30")
        target = get_env("NOTION_DB_ID", "Not configured") or "Not configured"
        stats = self.tray_app.get_runtime_stats() if hasattr(self.tray_app, "get_runtime_stats") else (self.tray_app.db.get_stats() if self.tray_app.db else {})

        connection_state = "Connected" if stats.get("connected") else "Not connected yet"
        size_text = stats.get("database_size_text", "Not available yet")
        self.connection_label.setText(
            f"Database: <b>{dbname}</b> on <b>{host}:{port}</b> | Connection: <b>{connection_state}</b> | Size: <b>{size_text}</b>"
        )
        self.target_label.setText(
            f"Notion target: <code>{target}</code> | Auto-sync every <b>{interval}</b> minute(s)"
        )
        self.records_label.setText(
            f"Local mirror: <b>{stats.get('active_pages', 0)}</b> active page(s) across <b>{stats.get('total_versions', 0)}</b> stored version(s)"
        )
        self.pending_label.setText(
            f"Pending push to Notion: <b>{stats.get('pending_push', 0)}</b> | Last pull: <b>{self._format_timestamp(stats.get('last_pull_at'))}</b> | Last push: <b>{self._format_timestamp(stats.get('last_push_at'))}</b>"
        )
        self.last_sync_label.setText(self.tray_app.last_sync_action.text())
        self.access_label.setText(f"Notion access check: {self.tray_app.get_notion_access_summary()}")
        self.db_connections_label.setText(self._build_db_connections_html())
        self.mcp_label.setText(f"Claude MCP: {self.tray_app.get_mcp_status_text()}")
        backup_summary = self.tray_app.get_backup_summary_text() if hasattr(self.tray_app, 'get_backup_summary_text') else 'Backup status not available yet'
        maintenance_manager = getattr(self.tray_app, 'maintenance_manager', None)
        maintenance_summary = maintenance_manager.get_status_text() if maintenance_manager else 'Maintenance status not available yet'
        maintenance_window = maintenance_manager.describe_maintenance_window() if maintenance_manager and hasattr(maintenance_manager, 'describe_maintenance_window') else 'overnight'
        wake_note = ' with wake-up catch-up' if maintenance_manager and hasattr(maintenance_manager, 'should_catch_up_after_wake') and maintenance_manager.should_catch_up_after_wake() else ''
        # Compose a more visual summary with line breaks
        sync_summary = getattr(self.tray_app, 'last_summary', 'Ready')
        self.summary_label.setText(
            f"<b>Latest sync summary:</b> {sync_summary}<br>"
            f"<b>Backup status:</b> {backup_summary}<br>"
            f"<b>Night clean-up:</b> {maintenance_window}{wake_note}<br>"
            f"<b>Maintenance status:</b> {maintenance_summary}"
        )
        # Example: set progress bar values (replace with real metrics if available)
        self.sync_bar.setValue(100 if 'success' in sync_summary.lower() else 50)
        self.backup_bar.setValue(100 if 'ok' in backup_summary.lower() else 50)
        self.maintenance_bar.setValue(100 if 'ok' in maintenance_summary.lower() else 50)
