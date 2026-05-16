from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

from src.core.config import get_app_display_name


class DatabaseBrowserWindow(QWidget):
    def __init__(self, tray_app, embedded: bool = False):
        super().__init__()
        self.tray_app = tray_app
        self.embedded = embedded
        self.entries: list[dict] = []
        self.current_entry: dict = {}
        self.current_context: dict = {}

        if not embedded:
            self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle(f"{get_app_display_name()} — Database Browser")
        self.resize(1180, 760)
        self.setMinimumSize(980, 640)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #f4f7fb;
                color: #17202a;
                font-size: 13px;
            }
            QLabel {
                background: transparent;
            }
            QFrame#summaryCard {
                background-color: #ffffff;
                border: 1px solid #d7e0ea;
                border-radius: 12px;
            }
            QLineEdit, QListWidget, QTextBrowser {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px;
            }
            QPushButton {
                background-color: #ffffff;
                border: 1px solid #c9d4e1;
                border-radius: 8px;
                padding: 9px 12px;
            }
            QPushButton:hover {
                border-color: #7c93b4;
                background-color: #f8fbff;
            }
            QPushButton#primaryAction {
                background-color: #2563eb;
                color: white;
                border: none;
                font-weight: 700;
            }
            QPushButton#primaryAction:hover {
                background-color: #1d4ed8;
            }
            """
        )

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        header = QLabel(
            "<h2 style='margin-bottom:4px;'>Database Browser</h2>"
            "<p style='margin-top:0;'>Search your mirrored Notion pages locally, open details fast, and spot anything waiting to push.</p>"
        )
        header.setWordWrap(True)
        root_layout.addWidget(header)

        summary_card = QFrame()
        summary_card.setObjectName("summaryCard")
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(12, 10, 12, 10)
        summary_layout.setSpacing(4)

        self.summary_label = QLabel("Loading your local mirror summary…")
        self.summary_label.setWordWrap(True)
        self.result_label = QLabel("Use the search box to narrow the list when you need to.")
        self.result_label.setWordWrap(True)
        self.result_label.setStyleSheet("color: #475569;")
        summary_layout.addWidget(self.summary_label)
        summary_layout.addWidget(self.result_label)
        root_layout.addWidget(summary_card)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by title or summary…")
        self.search_input.returnPressed.connect(self.refresh_entries)
        search_row.addWidget(self.search_input, 1)

        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All pages", "all")
        self.filter_combo.addItem("Waiting to push", "pending")
        self.filter_combo.addItem("With media", "media")
        self.filter_combo.addItem("Updated this week", "recent")
        self.filter_combo.currentIndexChanged.connect(lambda *_: self.refresh_entries())
        search_row.addWidget(self.filter_combo)

        self.limit_combo = QComboBox()
        for value in (50, 100, 200, 500):
            self.limit_combo.addItem(f"Show {value}", value)
        self.limit_combo.setCurrentIndex(2)
        self.limit_combo.currentIndexChanged.connect(lambda *_: self.refresh_entries())
        search_row.addWidget(self.limit_combo)

        self.search_btn = QPushButton("Search")
        self.search_btn.setObjectName("primaryAction")
        self.search_btn.clicked.connect(self.refresh_entries)
        search_row.addWidget(self.search_btn)

        self.refresh_btn = QPushButton("Refresh list")
        self.refresh_btn.clicked.connect(self.refresh_entries)
        search_row.addWidget(self.refresh_btn)
        root_layout.addLayout(search_row)

        splitter = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        left_title = QLabel("Pages in your local mirror")
        left_title.setStyleSheet("font-weight: 700;")
        left_layout.addWidget(left_title)

        self.page_list = QListWidget()
        self.page_list.currentItemChanged.connect(self._load_selected_entry)
        self.page_list.itemDoubleClicked.connect(lambda *_: self.open_selected_notion())
        left_layout.addWidget(self.page_list, 1)

        splitter.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        right_title = QLabel("Selected page details")
        right_title.setStyleSheet("font-weight: 700;")
        right_layout.addWidget(right_title)

        self.detail_view = QTextBrowser()
        self.detail_view.setOpenExternalLinks(True)
        self.detail_view.setHtml(self._empty_state_html("Pick a page on the left to preview it here."))
        right_layout.addWidget(self.detail_view, 1)

        action_row = QHBoxLayout()
        self.open_btn = QPushButton("Open in Notion")
        self.open_btn.clicked.connect(self.open_selected_notion)
        action_row.addWidget(self.open_btn)

        self.quick_edit_btn = QPushButton("Quick local edit")
        self.quick_edit_btn.clicked.connect(self.quick_edit_selected)
        action_row.addWidget(self.quick_edit_btn)

        self.bulk_replace_btn = QPushButton("Bulk search & replace")
        self.bulk_replace_btn.clicked.connect(self.open_bulk_replace)
        action_row.addWidget(self.bulk_replace_btn)

        self.copy_id_btn = QPushButton("Copy page ID")
        self.copy_id_btn.clicked.connect(self.copy_selected_page_id)
        action_row.addWidget(self.copy_id_btn)

        action_row.addStretch(1)
        right_layout.addLayout(action_row)

        splitter.addWidget(right_panel)
        splitter.setSizes([380, 760])
        root_layout.addWidget(splitter, 1)

        self.refresh_entries()

    def ensure_connected(self) -> bool:
        db = getattr(self.tray_app, "db", None)
        if db and getattr(db, "conn", None):
            return True
        if db and hasattr(db, "connect") and db.connect():
            return True

        message = "Please finish the PostgreSQL setup first, then open the Database Browser again."
        if self.embedded:
            self.summary_label.setText("Local mirror not ready yet")
            self.result_label.setText(message)
            self.current_entry = {}
            self.current_context = {}
            self.page_list.clear()
            self.detail_view.setHtml(self._empty_state_html(message))
        else:
            QMessageBox.warning(self, "Database not ready", message)
        return False

    def refresh(self):
        self.refresh_entries()

    @staticmethod
    def _empty_state_html(message: str) -> str:
        return (
            "<div style='padding:16px; background:#ffffff; border-radius:8px;'>"
            f"<p style='margin:0; color:#475569;'>{html.escape(message)}</p>"
            "</div>"
        )

    @staticmethod
    def _display_value(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (list, tuple)):
            text = ", ".join(DatabaseBrowserWindow._display_value(item) for item in value)
            return text or "—"
        if isinstance(value, dict):
            text = ", ".join(f"{key}: {DatabaseBrowserWindow._display_value(item)}" for key, item in value.items())
            return text or "—"
        text = " ".join(str(value).split())
        return text or "—"

    @staticmethod
    def format_timestamp(value: Any) -> str:
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value) if value else "Never"

    @staticmethod
    def format_entry_label(entry: dict) -> str:
        title = (entry.get("title") or "Untitled").strip() or "Untitled"
        notion_id = entry.get("notion_id", "")
        notes: list[str] = []

        if entry.get("needs_push"):
            notes.append("waiting to push")

        media_count = int(entry.get("media_count", 0) or 0)
        if media_count > 0 or entry.get("has_media"):
            notes.append(f"{media_count or 1} media")

        linked_count = int(entry.get("linked_page_count", 0) or 0)
        if linked_count > 0:
            notes.append(f"{linked_count} link(s)")

        label = f"{title} ({notion_id})" if notion_id else title
        if notes:
            label += " — " + ", ".join(notes)
        return label

    @staticmethod
    def build_results_text(
        stats: dict,
        shown_count: int,
        search_text: str,
        filter_label: str = "All pages",
        result_limit: int = 200,
    ) -> tuple[str, str]:
        active_pages = int(stats.get("active_pages", 0) or 0)
        pending_push = int(stats.get("pending_push_pages", 0) or 0)
        pages_with_media = int(stats.get("pages_with_media", 0) or 0)
        latest_update = DatabaseBrowserWindow.format_timestamp(stats.get("latest_local_update"))

        summary = (
            f"Local mirror: <b>{active_pages}</b> page(s) • "
            f"<b>{pending_push}</b> waiting to push • "
            f"<b>{pages_with_media}</b> with media • "
            f"Latest local update: <b>{html.escape(latest_update)}</b>"
        )

        if search_text:
            results = f"Showing <b>{shown_count}</b> result(s) for <b>{html.escape(search_text)}</b>."
        else:
            results = f"Showing up to <b>{int(result_limit or 0)}</b> page(s) from your local mirror."

        if filter_label and filter_label != "All pages":
            results += f" Filter: <b>{html.escape(filter_label)}</b>."
        return summary, results

    @staticmethod
    def build_page_details_html(entry: dict, context: dict) -> str:
        source = dict(entry or {})
        source.update(context or {})

        title = html.escape((source.get("title") or "Untitled").strip() or "Untitled")
        notion_id = html.escape(source.get("notion_id", ""))
        summary = html.escape(source.get("ai_summary") or source.get("summary_preview") or "No summary saved yet.").replace("\n", "<br>")
        updated_at = html.escape(DatabaseBrowserWindow.format_timestamp(source.get("updated_at")))
        source_updated_at = html.escape(DatabaseBrowserWindow.format_timestamp(source.get("source_updated_at")))
        pending_push = "Yes" if source.get("needs_push") else "No"
        media_count = int(source.get("media_count", 0) or 0)
        notion_url = source.get("user_notion_url") or ""
        internal_resource_uri = source.get("internal_resource_uri") or ""

        property_preview = source.get("property_preview") or {}
        if isinstance(property_preview, dict) and property_preview:
            property_items = "".join(
                f"<li><b>{html.escape(str(key))}:</b> {html.escape(DatabaseBrowserWindow._display_value(value))}</li>"
                for key, value in property_preview.items()
            )
        else:
            property_items = "<li>No property preview has been saved for this page yet.</li>"

        internal_links = source.get("internal_links") or []
        if internal_links:
            link_items = []
            for item in internal_links:
                link_title = html.escape((item or {}).get("title") or (item or {}).get("notion_id") or "Linked page")
                user_url = html.escape((item or {}).get("user_notion_url") or "")
                internal_uri = html.escape((item or {}).get("internal_resource_uri") or "")
                link_html = f"<li><b>{link_title}</b>"
                if user_url:
                    link_html += f" — <a href='{user_url}'>Open in Notion</a>"
                if internal_uri:
                    link_html += f"<br><code>{internal_uri}</code>"
                link_html += "</li>"
                link_items.append(link_html)
            links_html = "".join(link_items)
        else:
            links_html = "<li>No linked pages were found for this item.</li>"

        notion_button = (
            f"<p><a href='{html.escape(notion_url)}' style='display:inline-block; padding:8px 12px; background:#2563eb; color:#ffffff; text-decoration:none; border-radius:6px;'>Open in Notion</a></p>"
            if notion_url
            else "<p><i>No Notion link is saved for this page yet.</i></p>"
        )

        resource_block = f"<p><b>Local resource URI:</b> <code>{html.escape(internal_resource_uri or 'Not available')}</code></p>"

        return f"""
        <div style='font-family:Segoe UI, sans-serif; color:#17202a;'>
            <h2 style='margin-bottom:6px;'>{title}</h2>
            <p style='margin-top:0;'>
                <b>Page ID:</b> <code>{notion_id or 'Unknown'}</code><br>
                <b>Waiting to push:</b> {pending_push}<br>
                <b>Saved media:</b> {media_count}<br>
                <b>Updated locally:</b> {updated_at}<br>
                <b>Latest Notion update seen:</b> {source_updated_at}
            </p>
            <h3>Summary</h3>
            <p>{summary}</p>
            {notion_button}
            {resource_block}
            <h3>Property preview</h3>
            <ul>{property_items}</ul>
            <h3>Linked pages</h3>
            <ul>{links_html}</ul>
        </div>
        """

    def refresh_entries(self, target_notion_id: str | None = None):
        if not self.ensure_connected():
            return

        if not isinstance(target_notion_id, str):
            target_notion_id = None

        search_text = self.search_input.text().strip()
        filter_mode = self.filter_combo.currentData() or "all"
        filter_label = self.filter_combo.currentText() or "All pages"
        result_limit = int(self.limit_combo.currentData() or 200)
        db = getattr(self.tray_app, "db", None)
        if hasattr(self.tray_app, "get_catalog_overview"):
            stats = self.tray_app.get_catalog_overview()
        elif db and hasattr(db, "get_catalog_stats"):
            stats = db.get_catalog_stats() or {}
        else:
            stats = {}
        if hasattr(self.tray_app, "has_background_activity") and self.tray_app.has_background_activity():
            summary_html, results_html = self.build_results_text(stats, len(self.entries), search_text, filter_label, result_limit)
            self.summary_label.setText(summary_html)
            self.result_label.setText(f"{results_html} Background work is still running, so the list will refresh when it finishes.")
            if not self.entries:
                self.current_entry = {}
                self.current_context = {}
                self.detail_view.setHtml(self._empty_state_html("Background sync or maintenance is running. Please try the list again in a moment."))
            return

        if db and hasattr(db, "list_catalog_entries"):
            self.entries = db.list_catalog_entries(
                search_text=search_text,
                limit=result_limit,
                filter_mode=filter_mode,
            )
        else:
            self.entries = []

        summary_html, results_html = self.build_results_text(stats, len(self.entries), search_text, filter_label, result_limit)
        self.summary_label.setText(summary_html)
        self.result_label.setText(results_html)

        self.page_list.blockSignals(True)
        self.page_list.clear()

        for entry in self.entries:
            item = QListWidgetItem(self.format_entry_label(entry))
            item.setData(Qt.UserRole, entry)
            self.page_list.addItem(item)

        self.page_list.blockSignals(False)

        if not self.entries:
            if search_text:
                message = "No matching pages were found. Try a shorter or broader search."
            elif filter_mode != "all":
                message = "No pages match that filter yet. Try switching back to All pages."
            else:
                message = "No pages are mirrored yet. Run an import from Notion first."
            self.current_entry = {}
            self.current_context = {}
            self.detail_view.setHtml(self._empty_state_html(message))
            return

        row_to_select = 0
        if target_notion_id:
            for index, entry in enumerate(self.entries):
                if entry.get("notion_id") == target_notion_id:
                    row_to_select = index
                    break

        self.page_list.setCurrentRow(row_to_select)

    def _load_selected_entry(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None):
        entry = current.data(Qt.UserRole) if current else {}
        self.current_entry = dict(entry or {})

        notion_id = self.current_entry.get("notion_id", "")
        try:
            get_context = getattr(self.tray_app.db, "get_page_context", None)
            self.current_context = get_context(notion_id) if notion_id and callable(get_context) else {}
        except Exception as exc:
            self.current_context = {}
            self.detail_view.setHtml(self._empty_state_html(f"Could not load the saved page preview right now: {exc}"))
            return

        self.detail_view.setHtml(self.build_page_details_html(self.current_entry, self.current_context))

    def open_bulk_replace(self):
        dashboard = getattr(self.tray_app, "dashboard", None)
        if dashboard and hasattr(dashboard, "open_bulk_replace"):
            dashboard.open_bulk_replace()
            return

        from src.ui.dashboard import BulkReplaceDialog

        dialog = BulkReplaceDialog(self.tray_app, self)
        dialog.exec()
        self.refresh_entries()

    def _selected_notion_url(self) -> str:
        return (
            self.current_context.get("user_notion_url")
            or self.current_entry.get("user_notion_url")
            or ""
        )

    def open_selected_notion(self):
        url = self._selected_notion_url()
        if not url:
            message = "This page does not have a saved Notion URL yet."
            if self.embedded:
                self.result_label.setText(message)
            else:
                QMessageBox.information(self, "Notion link not available", message)
            return
        QDesktopServices.openUrl(QUrl(url))

    def copy_selected_page_id(self):
        notion_id = self.current_entry.get("notion_id", "")
        if not notion_id:
            message = "Pick a page first, then try copying its page ID again."
            if self.embedded:
                self.result_label.setText(message)
            else:
                QMessageBox.information(self, "Nothing selected", message)
            return

        QApplication.clipboard().setText(notion_id)
        message = "The page ID was copied to your clipboard."
        if self.embedded:
            self.result_label.setText(message)
        else:
            QMessageBox.information(self, "Copied", message)

    def quick_edit_selected(self):
        notion_id = self.current_entry.get("notion_id", "")
        if not notion_id:
            message = "Pick a page first, then try the quick local edit again."
            if self.embedded:
                self.result_label.setText(message)
            else:
                QMessageBox.information(self, "Nothing selected", message)
            return

        dashboard = getattr(self.tray_app, "dashboard", None)
        if dashboard and hasattr(dashboard, "queue_local_edit"):
            dashboard.queue_local_edit(notion_id=notion_id)
            return

        current_title = self.current_context.get("title") or self.current_entry.get("title") or ""
        current_summary = self.current_context.get("ai_summary") or self.current_entry.get("summary_preview") or ""

        new_title, ok = QInputDialog.getText(
            self,
            "Quick local edit",
            "Update the local title for this page:",
            text=current_title,
        )
        if not ok:
            return

        new_summary, ok = QInputDialog.getMultiLineText(
            self,
            "Quick local edit",
            "Update the local summary for this page:",
            text=current_summary,
        )
        if not ok:
            return

        success = self.tray_app.queue_local_edit(notion_id, new_title, new_summary)
        if success:
            self.refresh_entries(target_notion_id=notion_id)
