from __future__ import annotations

import html
import textwrap
from datetime import datetime
from difflib import HtmlDiff

from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtWidgets import (
    QCalendarWidget,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTimeEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


from src.core.config import get_app_display_name


class TimeMachineWindow(QWidget):
    def __init__(self, tray_app, embedded: bool = False):
        super().__init__()
        self.tray_app = tray_app
        self.loaded_snapshot_at: datetime | None = None
        self._syncing_checks = False
        self._syncing_activity = False

        if not embedded:
            self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle(f"{get_app_display_name()} — Time Machine Recovery")
        self.setMinimumSize(1120, 720)

        root_layout = QVBoxLayout(self)

        header = QLabel(
            "<h2>Time Machine Recovery</h2>"
            "<p>Pick a past date and time, compare what changed, then restore only the exact items you want back.</p>"
        )
        header.setWordWrap(True)
        root_layout.addWidget(header)

        picker_group = QGroupBox("1) Choose the recovery moment")
        picker_layout = QVBoxLayout()

        helper = QLabel(
            "Use the calendar and time picker like scheduling an appointment. "
            "When you click <b>Load Snapshot</b>, the app looks up the historical version stored in PostgreSQL. "
            "You can also use the recent activity log below to jump straight to a backup, sync, or restore time."
        )
        helper.setWordWrap(True)
        picker_layout.addWidget(helper)

        picker_row = QHBoxLayout()
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        picker_row.addWidget(self.calendar, 2)

        side_panel = QVBoxLayout()
        time_label = QLabel("Time")
        side_panel.addWidget(time_label)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("hh:mm AP")
        self.time_edit.setTime(QTime.currentTime())
        self.time_edit.setKeyboardTracking(False)
        side_panel.addWidget(self.time_edit)

        self.load_btn = QPushButton("Load Snapshot")
        self.load_btn.clicked.connect(self.load_snapshot)
        side_panel.addWidget(self.load_btn)

        self.use_log_btn = QPushButton("Use Checked Log Entry")
        self.use_log_btn.clicked.connect(self.use_checked_activity_timestamp)
        side_panel.addWidget(self.use_log_btn)

        self.select_all_btn = QPushButton("Select All Changes")
        self.select_all_btn.clicked.connect(self.select_all_changes)
        side_panel.addWidget(self.select_all_btn)

        self.clear_btn = QPushButton("Clear Selection")
        self.clear_btn.clicked.connect(self.clear_selected_changes)
        side_panel.addWidget(self.clear_btn)

        side_panel.addStretch(1)
        picker_row.addLayout(side_panel, 1)
        picker_layout.addLayout(picker_row)

        activity_label = QLabel("Recent activity log")
        activity_label.setWordWrap(True)
        picker_layout.addWidget(activity_label)

        self.activity_tree = QTreeWidget()
        self.activity_tree.setHeaderLabels(["Keep", "When", "Action", "Summary"])
        self.activity_tree.setAlternatingRowColors(True)
        self.activity_tree.setRootIsDecorated(False)
        self.activity_tree.setWordWrap(True)
        self.activity_tree.setUniformRowHeights(False)
        self.activity_tree.setTextElideMode(Qt.ElideNone)
        self.activity_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        activity_header = self.activity_tree.header()
        activity_header.setStretchLastSection(True)
        activity_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        activity_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        activity_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        activity_header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.activity_tree.itemChanged.connect(self.on_activity_item_changed)
        picker_layout.addWidget(self.activity_tree)

        picker_group.setLayout(picker_layout)
        root_layout.addWidget(picker_group)

        self.summary_label = QLabel("No snapshot loaded yet.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-weight: bold;")
        root_layout.addWidget(self.summary_label)

        self.change_tree = QTreeWidget()
        self.change_tree.setHeaderLabels(["Restore", "Changed item", "State", "Push to Notion"])
        self.change_tree.setAlternatingRowColors(True)
        self.change_tree.itemSelectionChanged.connect(self.update_preview)
        self.change_tree.itemChanged.connect(self.on_item_changed)

        self.current_view = QTextBrowser()
        self.snapshot_view = QTextBrowser()
        self.diff_view = QTextBrowser()

        self.current_view.setHtml(self._render_panel("Live now", "Choose a changed item to preview it.", "#fff4f4"))
        self.snapshot_view.setHtml(self._render_panel("Historical snapshot", "Choose a changed item to preview it.", "#f3fff3"))
        self.diff_view.setHtml("<p><i>The side-by-side diff will appear here.</i></p>")

        preview_splitter = QSplitter(Qt.Vertical)

        top_preview = QWidget()
        top_preview_layout = QHBoxLayout(top_preview)
        top_preview_layout.setContentsMargins(0, 0, 0, 0)
        top_preview_layout.addWidget(self.current_view)
        top_preview_layout.addWidget(self.snapshot_view)

        preview_splitter.addWidget(top_preview)
        preview_splitter.addWidget(self.diff_view)
        preview_splitter.setSizes([320, 240])

        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(self.change_tree)
        main_splitter.addWidget(preview_splitter)
        main_splitter.setSizes([420, 680])
        root_layout.addWidget(main_splitter, 1)

        action_row = QHBoxLayout()
        self.restore_local_btn = QPushButton("Restore to Local Database Only")
        self.restore_local_btn.clicked.connect(lambda: self.run_restore(push_to_notion=False))
        action_row.addWidget(self.restore_local_btn)

        self.push_restore_btn = QPushButton("Push Restore to Notion")
        self.push_restore_btn.clicked.connect(lambda: self.run_restore(push_to_notion=True))
        action_row.addWidget(self.push_restore_btn)

        action_row.addStretch(1)
        root_layout.addLayout(action_row)

    def _selected_local_datetime(self) -> datetime:
        selected_date = self.calendar.selectedDate().toPython()
        selected_time = self.time_edit.time().toPython()
        local_tz = datetime.now().astimezone().tzinfo
        return datetime.combine(selected_date, selected_time).replace(tzinfo=local_tz)

    def _render_panel(self, title: str, body: str, background: str) -> str:
        safe_body = html.escape(body or "(empty)")
        return (
            f"<div style='background:{background}; padding:10px; border-radius:8px;'>"
            f"<h3 style='margin-top:0;'>{html.escape(title)}</h3>"
            f"<pre style='white-space:pre-wrap; font-family:Consolas, monospace; margin:0;'>{safe_body}</pre>"
            "</div>"
        )

    def _render_diff(self, current_text: str, snapshot_text: str) -> str:
        diff_table = HtmlDiff(wrapcolumn=60).make_table(
            (current_text or "").splitlines(),
            (snapshot_text or "").splitlines(),
            fromdesc="Live now",
            todesc="Historical snapshot",
            context=True,
            numlines=2,
        )
        return (
            "<style>"
            "table.diff {font-family:Consolas, monospace; border-collapse:collapse; width:100%;}"
            "table.diff td, table.diff th {padding:4px; border:1px solid #ddd; vertical-align:top;}"
            "</style>"
            + diff_table
        )

    @staticmethod
    def format_activity_summary(summary: str, width: int = 72) -> str:
        compact_text = " ".join(str(summary or "").split())
        if not compact_text:
            return "No summary recorded."

        return textwrap.fill(
            compact_text,
            width=max(24, int(width or 72)),
            break_long_words=True,
            break_on_hyphens=True,
        )

    def refresh_activity_log(self):
        log_reader = getattr(self.tray_app, "get_activity_log", None)
        if callable(log_reader):
            entries = log_reader(limit=120)
        elif getattr(self.tray_app, "db", None) and hasattr(self.tray_app.db, "get_activity_log"):
            entries = self.tray_app.db.get_activity_log(limit=120)
        else:
            entries = []

        self._syncing_activity = True
        try:
            self.activity_tree.clear()
            for entry in entries:
                timestamp_text = str(entry.get("snapshot_at") or entry.get("created_at") or "")
                full_summary = str(entry.get("summary") or "").strip() or "No summary recorded."
                item = QTreeWidgetItem(
                    [
                        "",
                        timestamp_text.replace("T", " ")[:16] if timestamp_text else "Unknown",
                        entry.get("action_label") or str(entry.get("action") or "Activity").replace("_", " ").title(),
                        self.format_activity_summary(full_summary),
                    ]
                )
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                item.setCheckState(0, Qt.Checked if entry.get("is_checked") else Qt.Unchecked)
                item.setData(0, Qt.UserRole, entry)
                item.setToolTip(3, full_summary)
                self.activity_tree.addTopLevelItem(item)
        finally:
            self._syncing_activity = False

    def on_activity_item_changed(self, item: QTreeWidgetItem, _column: int):
        if self._syncing_activity:
            return

        entry = item.data(0, Qt.UserRole) or {}
        entry_id = entry.get("id")
        update_check_state = getattr(self.tray_app, "set_activity_checked", None)
        if entry_id and callable(update_check_state):
            update_check_state(entry_id, item.checkState(0) == Qt.Checked)

    def use_checked_activity_timestamp(self):
        target_entry = None
        for index in range(self.activity_tree.topLevelItemCount()):
            item = self.activity_tree.topLevelItem(index)
            if item.checkState(0) == Qt.Checked:
                target_entry = item.data(0, Qt.UserRole) or {}
                break

        if not target_entry:
            current_item = self.activity_tree.currentItem()
            target_entry = (current_item.data(0, Qt.UserRole) or {}) if current_item else None

        timestamp_text = str((target_entry or {}).get("snapshot_at") or (target_entry or {}).get("created_at") or "").strip()
        if not timestamp_text:
            QMessageBox.information(self, "Pick a log entry", "Tick or select an activity log entry first.")
            return

        try:
            snapshot_at = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00")).astimezone()
        except ValueError:
            QMessageBox.warning(self, "Time not available", "That log entry did not include a usable timestamp.")
            return

        self.calendar.setSelectedDate(QDate(snapshot_at.year, snapshot_at.month, snapshot_at.day))
        self.time_edit.setTime(QTime(snapshot_at.hour, snapshot_at.minute))
        self.load_snapshot()

    def load_snapshot(self):
        db = getattr(self.tray_app, "db", None)
        if not db:
            QMessageBox.warning(self, "Database not ready", "Please finish the PostgreSQL setup first.")
            return

        if not getattr(db, "conn", None) and not db.connect():
            QMessageBox.warning(self, "Database not ready", "Please finish the PostgreSQL setup first.")
            return

        if not hasattr(db, "get_time_machine_comparison"):
            QMessageBox.warning(self, "Snapshot could not load", "Time Machine comparison is not available in this build yet.")
            return

        snapshot_at = self._selected_local_datetime()
        self.load_btn.setEnabled(False)
        try:
            changes = db.get_time_machine_comparison(snapshot_at)
        except Exception as exc:
            self.loaded_snapshot_at = None
            self.change_tree.blockSignals(True)
            self.change_tree.clear()
            self.change_tree.blockSignals(False)
            self.summary_label.setText(
                f"Could not load the snapshot for {snapshot_at.strftime('%Y-%m-%d %I:%M %p')}."
            )
            self.current_view.setHtml(self._render_panel("Live now", "The snapshot could not be loaded right now.", "#fff4f4"))
            self.snapshot_view.setHtml(self._render_panel("Historical snapshot", "Please try again in a moment or choose a different time.", "#f3fff3"))
            self.diff_view.setHtml("<p><i>The side-by-side diff could not be generated for this moment.</i></p>")
            QMessageBox.warning(
                self,
                "Snapshot could not load",
                f"The app could not load that snapshot right now.\n\n{exc}",
            )
            return
        finally:
            self.load_btn.setEnabled(True)

        self.loaded_snapshot_at = snapshot_at
        self.refresh_activity_log()

        self.change_tree.blockSignals(True)
        self.change_tree.clear()
        self.change_tree.blockSignals(False)

        if not changes:
            self.summary_label.setText(
                f"No recoverable differences were found for {snapshot_at.strftime('%Y-%m-%d %I:%M %p')}."
            )
            self.current_view.setHtml(self._render_panel("Live now", "No differences were found.", "#fff4f4"))
            self.snapshot_view.setHtml(self._render_panel("Historical snapshot", "Try a different time if you need an older version.", "#f3fff3"))
            self.diff_view.setHtml("<p><i>No side-by-side differences to show for this moment.</i></p>")
            return

        self._populate_tree(changes)
        page_count = len(changes)
        field_count = sum(len(page.get("changed_fields", [])) for page in changes)
        self.summary_label.setText(
            f"Loaded {page_count} page(s) with {field_count} changed item(s) from {snapshot_at.strftime('%Y-%m-%d %I:%M %p')}."
        )

    def _populate_tree(self, changes: list[dict]):
        self._syncing_checks = True
        try:
            self.change_tree.clear()
            for page in changes:
                page_item = QTreeWidgetItem(["", f"{page['title']} ({page['notion_id']})", page["status"], f"{len(page['changed_fields'])} item(s)"])
                page_item.setFlags(page_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                page_item.setCheckState(0, Qt.Unchecked)
                page_item.setData(0, Qt.UserRole, {"kind": "page", "page": page})
                self.change_tree.addTopLevelItem(page_item)

                for change in page.get("changed_fields", []):
                    child = QTreeWidgetItem(["", change["label"], change["status"], "Yes" if change.get("push_supported") else "Local only"])
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                    child.setCheckState(0, Qt.Unchecked)
                    child.setData(0, Qt.UserRole, {"kind": "field", "page": page, "change": change})
                    child.setToolTip(1, change.get("label", ""))
                    child.setToolTip(2, change.get("status", ""))
                    child.setToolTip(3, "Can be patched to Notion now." if change.get("push_supported") else "This item restores locally only.")
                    page_item.addChild(child)

                page_item.setExpanded(True)

            if self.change_tree.topLevelItemCount():
                first_item = self.change_tree.topLevelItem(0)
                target = first_item.child(0) if first_item and first_item.childCount() else first_item
                if target:
                    self.change_tree.setCurrentItem(target)
        finally:
            self._syncing_checks = False

    def on_item_changed(self, item: QTreeWidgetItem, _column: int):
        if self._syncing_checks:
            return

        self._syncing_checks = True
        try:
            payload = item.data(0, Qt.UserRole) or {}
            if payload.get("kind") == "page":
                for index in range(item.childCount()):
                    item.child(index).setCheckState(0, item.checkState(0))
            else:
                parent = item.parent()
                if not parent:
                    return
                states = [parent.child(i).checkState(0) for i in range(parent.childCount())]
                if all(state == Qt.Checked for state in states):
                    parent.setCheckState(0, Qt.Checked)
                elif any(state == Qt.Checked for state in states):
                    parent.setCheckState(0, Qt.PartiallyChecked)
                else:
                    parent.setCheckState(0, Qt.Unchecked)
        finally:
            self._syncing_checks = False

    def update_preview(self):
        item = self.change_tree.currentItem()
        if not item:
            return

        payload = item.data(0, Qt.UserRole) or {}
        if payload.get("kind") == "field":
            change = payload.get("change") or {}
            current_text = change.get("current_value") or "(empty)"
            snapshot_text = change.get("snapshot_value") or "(empty)"
            title = change.get("label") or "Selected change"
        else:
            page = payload.get("page") or {}
            changed_names = "\n".join(f"• {entry.get('label', 'Change')}" for entry in page.get("changed_fields", [])) or "No changed items listed."
            current_text = f"Page: {page.get('title', 'Untitled')}\nState: {page.get('status', 'Unknown')}\n\nChanged items:\n{changed_names}"
            snapshot_text = current_text
            title = page.get("title") or "Selected page"

        self.current_view.setHtml(self._render_panel(f"Live now — {title}", current_text, "#fff4f4"))
        self.snapshot_view.setHtml(self._render_panel(f"Historical snapshot — {title}", snapshot_text, "#f3fff3"))
        self.diff_view.setHtml(self._render_diff(current_text, snapshot_text))

    def select_all_changes(self):
        self._syncing_checks = True
        try:
            for index in range(self.change_tree.topLevelItemCount()):
                item = self.change_tree.topLevelItem(index)
                item.setCheckState(0, Qt.Checked)
                for child_index in range(item.childCount()):
                    item.child(child_index).setCheckState(0, Qt.Checked)
        finally:
            self._syncing_checks = False

    def clear_selected_changes(self):
        self._syncing_checks = True
        try:
            for index in range(self.change_tree.topLevelItemCount()):
                item = self.change_tree.topLevelItem(index)
                item.setCheckState(0, Qt.Unchecked)
                for child_index in range(item.childCount()):
                    item.child(child_index).setCheckState(0, Qt.Unchecked)
        finally:
            self._syncing_checks = False

    def _collect_restore_requests(self) -> list[dict]:
        if not self.loaded_snapshot_at:
            return []

        grouped: dict[str, dict] = {}
        for index in range(self.change_tree.topLevelItemCount()):
            page_item = self.change_tree.topLevelItem(index)
            payload = page_item.data(0, Qt.UserRole) or {}
            page = payload.get("page") or {}
            notion_id = page.get("notion_id")
            if not notion_id:
                continue

            selected_fields = []
            for child_index in range(page_item.childCount()):
                child = page_item.child(child_index)
                if child.checkState(0) != Qt.Checked:
                    continue
                child_payload = child.data(0, Qt.UserRole) or {}
                change = child_payload.get("change") or {}
                field_key = change.get("field_key")
                if field_key:
                    selected_fields.append(field_key)

            if selected_fields:
                grouped[notion_id] = {
                    "notion_id": notion_id,
                    "snapshot_at": self.loaded_snapshot_at.isoformat(),
                    "selected_fields": selected_fields,
                }

        return list(grouped.values())

    def run_restore(self, push_to_notion: bool):
        if not self.loaded_snapshot_at:
            QMessageBox.information(self, "Pick a time first", "Choose a historical date and time and load its snapshot first.")
            return

        requests = self._collect_restore_requests()
        if not requests:
            QMessageBox.information(self, "Nothing selected", "Tick the exact items you want to recover first.")
            return

        page_count = len(requests)
        item_count = sum(len(entry.get("selected_fields", [])) for entry in requests)
        action_name = "restore locally" if not push_to_notion else "restore and push to Notion"
        answer = QMessageBox.question(
            self,
            "Confirm recovery",
            f"Recover {item_count} selected item(s) across {page_count} page(s) and {action_name}?",
        )
        if answer != QMessageBox.Yes:
            return

        restore_callback = getattr(self.tray_app, "restore_from_time_machine", None)
        if not callable(restore_callback):
            QMessageBox.warning(self, "Recovery unavailable", "This build does not expose Time Machine recovery yet.")
            return

        try:
            success, message = restore_callback(requests, push_to_notion=push_to_notion)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Recovery finished with issues",
                f"The restore could not finish right now.\n\n{exc}",
            )
            return

        if success:
            QMessageBox.information(self, "Recovery finished", message)
            self.refresh_activity_log()
            self.load_snapshot()
        else:
            self.refresh_activity_log()
            QMessageBox.warning(self, "Recovery finished with issues", message)
