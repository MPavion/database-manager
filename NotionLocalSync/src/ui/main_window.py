from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import (
    get_app_display_name,
    get_app_tagline,
    get_app_version,
    get_claude_mcp_name,
    get_env,
    is_windows_startup_enabled,
    save_env_var,
    sync_windows_startup,
)
from src.core.plugins import PluginRegistry, ServicePlugin, ServiceStatus, build_default_registry
from src.ui.database_browser import DatabaseBrowserWindow
from src.ui.time_machine import TimeMachineWindow
from src.ui.wizard import SetupWizard


def build_dark_stylesheet() -> str:
    return """
    QWidget {
        background-color: #111827;
        color: #e5e7eb;
        font-family: 'Segoe UI';
        font-size: 10pt;
    }
    QMainWindow {
        background-color: #0b1220;
    }
    QFrame#sidebar {
        background-color: #0f172a;
        border-right: 1px solid #1f2937;
    }
    QListWidget {
        background-color: transparent;
        border: none;
        outline: 0;
        padding: 8px;
    }
    QListWidget::item {
        border-radius: 10px;
        padding: 12px 10px;
        margin: 3px 0;
    }
    QListWidget::item:hover {
        background-color: #1e293b;
    }
    QListWidget::item:selected {
        background-color: #2563eb;
        color: white;
    }
    QFrame#card, QFrame#metricCard {
        background-color: #172033;
        border: 1px solid #24324a;
        border-radius: 14px;
    }
    QFrame#serviceCard {
        background-color: #172033;
        border: 1px solid #24324a;
        border-radius: 14px;
    }
    QFrame#serviceCard:hover {
        border-color: #3b82f6;
        background-color: #1a2540;
    }
    QFrame#warningCard {
        background-color: #2a1d12;
        border: 1px solid #b45309;
        border-radius: 14px;
    }
    QLabel#eyebrow {
        color: #93c5fd;
        font-size: 9pt;
        font-weight: 600;
        text-transform: uppercase;
    }
    QLabel#pageTitle {
        font-size: 18pt;
        font-weight: 700;
    }
    QLabel#sectionTitle {
        font-size: 11pt;
        font-weight: 700;
        color: #cbd5e1;
    }
    QLabel#serviceName {
        font-size: 12pt;
        font-weight: 700;
    }
    QLabel#metricValue {
        font-size: 16pt;
        font-weight: 700;
    }
    QLabel#activityHeadline {
        font-size: 10.5pt;
        font-weight: 600;
        color: #e2e8f0;
    }
    QLabel#statusBadge {
        background-color: #1d4ed8;
        color: white;
        border-radius: 10px;
        padding: 4px 10px;
        font-weight: 700;
        font-size: 9pt;
    }
    QLabel#statusBadgeGreen {
        background-color: #15803d;
        color: white;
        border-radius: 10px;
        padding: 4px 10px;
        font-weight: 700;
        font-size: 9pt;
    }
    QLabel#statusBadgeOrange {
        background-color: #b45309;
        color: white;
        border-radius: 10px;
        padding: 4px 10px;
        font-weight: 700;
        font-size: 9pt;
    }
    QLabel#statusBadgeRed {
        background-color: #b91c1c;
        color: white;
        border-radius: 10px;
        padding: 4px 10px;
        font-weight: 700;
        font-size: 9pt;
    }
    QLabel#statusBadgeGrey {
        background-color: #374151;
        color: #9ca3af;
        border-radius: 10px;
        padding: 4px 10px;
        font-weight: 700;
        font-size: 9pt;
    }
    QLabel#warningBadge {
        background-color: #b45309;
        color: white;
        border-radius: 10px;
        padding: 6px 10px;
        font-weight: 700;
    }
    QLabel#cardMeta {
        color: #64748b;
        font-size: 9pt;
    }
    QLabel#cardSummary {
        color: #94a3b8;
        font-size: 9pt;
    }
    QLabel#navSectionLabel {
        color: #4b5563;
        font-size: 8pt;
        font-weight: 700;
        text-transform: uppercase;
        padding: 6px 10px 2px 10px;
    }
    QPushButton {
        background-color: #1f2937;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 10px 14px;
    }
    QPushButton:hover {
        background-color: #2b3a55;
        border-color: #60a5fa;
        color: white;
    }
    QPushButton:pressed {
        background-color: #172554;
        border-color: #93c5fd;
        padding: 11px 14px 9px 14px;
    }
    QPushButton:disabled {
        background-color: #111827;
        border-color: #1f2937;
        color: #6b7280;
    }
    QPushButton#primaryAction {
        background-color: #2563eb;
        border-color: #2563eb;
        color: white;
        font-weight: 700;
    }
    QPushButton#primaryAction:hover {
        background-color: #1d4ed8;
        border-color: #93c5fd;
    }
    QPushButton#primaryAction:pressed {
        background-color: #1e40af;
        border-color: #bfdbfe;
    }
    QPushButton#warningAction {
        background-color: #7c2d12;
        border-color: #c2410c;
        color: white;
        font-weight: 700;
    }
    QPushButton#warningAction:hover {
        background-color: #9a3412;
        border-color: #fdba74;
    }
    QPushButton#warningAction:pressed {
        background-color: #7c2d12;
        border-color: #ffedd5;
    }
    QLineEdit, QPlainTextEdit {
        background-color: #0f172a;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 8px 10px;
    }
    QLineEdit:focus, QPlainTextEdit:focus {
        border-color: #60a5fa;
    }
    QScrollArea {
        border: none;
    }
    """


class MetricCard(QFrame):
    def __init__(self, title: str, value: str, detail: str, parent=None):
        super().__init__(parent)
        self.setObjectName("metricCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setObjectName("eyebrow")
        layout.addWidget(title_label)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)

        self.detail_label = QLabel(detail)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

    def update_content(self, value: str, detail: str):
        self.value_label.setText(value)
        self.detail_label.setText(detail)


# ── Dashboard service summary cards ──────────────────────────────────────────

_STATE_BADGE_STYLES = {
    "ready": "statusBadgeGreen",
    "not set up": "statusBadgeRed",
    "needs setup": "statusBadgeRed",
    "setup needed": "statusBadgeRed",
    "not connected": "statusBadgeRed",
    "connected": "statusBadgeOrange",
    "turned off": "statusBadgeGrey",
}


def _badge_style_for_state(state: str) -> str:
    low = state.lower()
    for key, obj_name in _STATE_BADGE_STYLES.items():
        if key in low:
            return obj_name
    return "statusBadge"


def _normalize_inline_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _truncate_text(value: str, limit: int = 96) -> str:
    text = _normalize_inline_text(value)
    if len(text) <= limit:
        return text

    clipped = text[: max(0, limit - 3)].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    if not clipped:
        clipped = text[: max(0, limit - 3)]
    return f"{clipped}..."


def _compact_last_sync_status(sync_status_text: str) -> str:
    text = _normalize_inline_text(sync_status_text)
    if not text:
        return ""
    if text.lower() == "last sync: never":
        return "Sync not run yet"

    match = re.match(r"Last sync:\s*(.*?)\s*@\s*.*?\((.*?)\)\s*$", text, re.IGNORECASE)
    if match:
        label = _normalize_inline_text(match.group(1))
        elapsed = _normalize_inline_text(match.group(2))
        if label.lower() == "notion sync":
            return f"Synced {elapsed}"
        return f"{label} · {elapsed}"

    if text.lower().startswith("last sync:"):
        text = text.split(":", 1)[1].strip()
    return _truncate_text(text, limit=56)


def _compact_maintenance_status(maintenance_state: dict | None) -> str:
    if not isinstance(maintenance_state, dict) or not maintenance_state:
        return ""

    statuses = [
        str((entry or {}).get("last_status") or "").strip().lower()
        for entry in maintenance_state.values()
        if isinstance(entry, dict)
    ]
    if not statuses:
        return ""

    attention_count = sum(status in {"error", "failed", "warning"} for status in statuses)
    pending_count = sum(status in {"never", "skipped"} for status in statuses)

    if attention_count:
        return f"Clean-up: {attention_count} need attention"
    if pending_count == len(statuses):
        return "Clean-up: not run yet"
    if pending_count:
        return f"Clean-up: {pending_count} pending"
    return "Clean-up: up to date"


def build_compact_activity_text(
    last_summary: str,
    sync_status_text: str = "",
    maintenance_state: dict | None = None,
) -> tuple[str, str]:
    headline = _truncate_text(last_summary, limit=108)
    if not headline or headline.lower() in {"ready", "working"}:
        headline = "Waiting for the first sync…"

    meta_parts = [
        part
        for part in (
            _compact_last_sync_status(sync_status_text),
            _compact_maintenance_status(maintenance_state),
        )
        if part
    ]
    return headline, "  ·  ".join(meta_parts)


def _describe_action(label: str) -> str:
    text = _normalize_inline_text(label).lower()
    if not text:
        return ""

    rules: list[tuple[tuple[str, ...], str]] = [
        (
            ("run smart sync", "sync all now", "sync now", "sync clickup now", "sync n8n now"),
            "Safest everyday option. It pulls in new updates and sends your queued local changes back out.",
        ),
        (
            ("import from notion only", "import only"),
            "Pulls the latest remote content into your local database without changing anything online.",
        ),
        (
            ("push local changes only", "push local changes"),
            "Sends only the edits waiting in your local queue. It will not pull newer remote changes first.",
        ),
        (
            ("resync databases", "resync connections", "one-click resync", "resync notion"),
            "Refreshes the current service's saved access and pulls in anything newly visible to that service.",
        ),
        (
            ("check access", "check notion access"),
            "Verifies your connection and shows whether the app can reach the Notion areas you expect.",
        ),
        (
            ("run health check",),
            "Checks connections, the local database, and backup or clean-up readiness. It reports problems without editing your content.",
        ),
        (
            ("run clean-up now", "run maintenance", "run wordpress clean-up", "run clickup tidy-up"),
            "Runs the housekeeping rules for this area, such as deduping items and clearing old leftovers based on your settings.",
        ),
        (
            ("back up now", "run backup now"),
            "Creates a fresh backup of your local database so you have a restore point before bigger changes.",
        ),
        (
            ("configure claude mcp", "configure mcp"),
            "Updates the Claude Desktop link so your AI tools can talk directly to this app.",
        ),
        (
            ("speed up database", "run vacuum analyze"),
            "Optimizes the local database after lots of sync or clean-up activity to help keep searches fast.",
        ),
        (
            ("browse local database", "browse saved pages"),
            "Opens your local copy so you can review records without touching the live service.",
        ),
        (
            ("queue local edit", "edit a page locally"),
            "Lets you prepare a local change first, then review and push it later when you are ready.",
        ),
        (
            ("bulk search & replace",),
            "Finds repeated text in your local copy so you can update it in bulk before pushing reviewed changes.",
        ),
        (
            ("copy webhook details",),
            "Copies the webhook setup details you need when connecting ClickUp back to this app.",
        ),
        (
            ("setup", "quick-start setup", "open setup", "open setup guide"),
            "Opens the guided setup so you can connect or update this service step by step.",
        ),
        (
            ("open logs",),
            "Shows the recent activity log so you can see what the app has been doing.",
        ),
    ]

    for keywords, description in rules:
        if any(keyword in text for keyword in keywords):
            return description
    return ""


def _format_action_help(labels: list[str], intro: str = "What these buttons do") -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for label in labels:
        clean_label = _normalize_inline_text(label)
        if not clean_label or clean_label in seen:
            continue
        seen.add(clean_label)
        description = _describe_action(clean_label)
        if description:
            lines.append(f"• <b>{clean_label}</b>: {description}")

    if not lines:
        return ""
    return f"<b>{intro}</b><br>" + "<br>".join(lines)


class ServiceSummaryCard(QFrame):
    """A compact card shown on the Dashboard for one service."""

    open_requested: Signal = Signal()

    def __init__(self, plugin: ServicePlugin, accent_color: str = "#3b82f6", parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self._accent = accent_color
        self.setObjectName("serviceCard")
        self.setMinimumHeight(170)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Colored accent strip at top
        accent_strip = QFrame()
        accent_strip.setFixedHeight(4)
        accent_strip.setStyleSheet(f"background-color: {accent_color}; border-radius: 14px 14px 0 0;")
        root.addWidget(accent_strip)

        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(6)

        # Service name + status badge
        top_row = QHBoxLayout()
        self.name_label = QLabel(plugin.display_name)
        self.name_label.setObjectName("serviceName")
        top_row.addWidget(self.name_label)
        top_row.addStretch(1)
        self.last_activity_label = QLabel("")
        self.last_activity_label.setObjectName("cardMeta")
        self.last_activity_label.hide()
        top_row.addWidget(self.last_activity_label)
        self.status_badge = QLabel("Checking…")
        self.status_badge.setObjectName("statusBadgeGrey")
        top_row.addWidget(self.status_badge)
        body.addLayout(top_row)

        # Summary / key metric
        self.summary_label = QLabel("Loading…")
        self.summary_label.setObjectName("cardSummary")
        self.summary_label.setWordWrap(True)
        body.addWidget(self.summary_label)

        # Detail line (first entry from status.details)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("cardMeta")
        self.detail_label.setWordWrap(True)
        body.addWidget(self.detail_label)

        body.addStretch(1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.action_btn = QPushButton("…")
        self.action_btn.setObjectName("primaryAction")
        self.action_btn.setEnabled(False)
        btn_row.addWidget(self.action_btn)

        self.open_btn = QPushButton("Open →")
        self.open_btn.clicked.connect(self.open_requested)
        btn_row.addWidget(self.open_btn)
        btn_row.addStretch(1)
        body.addLayout(btn_row)

        root.addLayout(body)

    def refresh(self):
        try:
            status = self.plugin.get_status()
        except Exception:
            status = ServiceStatus(state="Checking…", summary="Waiting for the database.", details=[])

        # Status badge
        badge_style = _badge_style_for_state(status.state)
        self.status_badge.setObjectName(badge_style)
        self.status_badge.setText(status.state)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

        self.summary_label.setText(status.summary)
        meta_text = _normalize_inline_text(getattr(status, "meta", ""))
        self.last_activity_label.setVisible(bool(meta_text))
        self.last_activity_label.setText(meta_text)
        self.detail_label.setText(status.details[0] if status.details else "")

        # Primary quick action
        try:
            actions = self.plugin.get_quick_actions()
        except Exception:
            actions = []
        if actions:
            first = actions[0]
            self.action_btn.setText(first.label)
            self.action_btn.setEnabled(True)
            try:
                self.action_btn.clicked.disconnect()
            except RuntimeError:
                pass
            if callable(first.callback):
                self.action_btn.clicked.connect(first.callback)
        else:
            self.action_btn.setText("View details")
            self.action_btn.setEnabled(False)


# ── Dashboard overview page ───────────────────────────────────────────────────

# The primary services shown on the dashboard, in display order.
_DASHBOARD_SERVICES = ["notion", "clickup", "wordpress"]


class OverviewPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        # Populated lazily after plugins are ready
        self._service_cards: dict[str, ServiceSummaryCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root.addWidget(self.scroll_area)

        content = QWidget()
        self.scroll_area.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(16)

        # ── Header ────────────────────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("card")
        hdr_layout = QVBoxLayout(header)
        hdr_layout.setContentsMargins(20, 18, 20, 18)
        hdr_layout.setSpacing(4)

        eyebrow = QLabel("Your connected services, at a glance")
        eyebrow.setObjectName("eyebrow")
        hdr_layout.addWidget(eyebrow)

        title = QLabel(get_app_display_name())
        title.setObjectName("pageTitle")
        hdr_layout.addWidget(title)

        version_label = QLabel(f"{get_app_version()} · {get_app_tagline()}")
        version_label.setObjectName("cardMeta")
        version_label.setWordWrap(True)
        hdr_layout.addWidget(version_label)

        intro = QLabel(
            "See which services are connected, when they last synced, and the best next step for each one. "
            "Click <b>Open →</b> to jump straight to that service."
        )
        intro.setWordWrap(True)
        hdr_layout.addWidget(intro)

        # Global actions in the header
        global_row = QHBoxLayout()
        tray = main_window.tray_app
        sync_all_btn = QPushButton("Sync all now")
        sync_all_btn.setObjectName("primaryAction")
        sync_all_btn.setToolTip("Run a two-way sync across all connected services.")
        if callable(getattr(tray, "run_sync", None)):
            sync_all_btn.clicked.connect(lambda: tray.run_sync("sync"))
        else:
            sync_all_btn.setEnabled(False)
        global_row.addWidget(sync_all_btn)

        health_btn = QPushButton("Run health check")
        health_btn.setToolTip("Check that all connections and database tables are healthy.")
        if callable(getattr(tray, "run_health_check", None)):
            health_btn.clicked.connect(lambda: tray.run_health_check(show_message=False))
        else:
            health_btn.setEnabled(False)
        global_row.addWidget(health_btn)

        history_btn = QPushButton("Health & history")
        history_btn.setToolTip("Open the app-wide health, progress, and recent activity view.")
        history_btn.clicked.connect(lambda: self.main_window.navigate_to_page("Health & history"))
        global_row.addWidget(history_btn)

        global_row.addStretch(1)
        hdr_layout.addLayout(global_row)

        global_help = QLabel(
            _format_action_help(
                ["Sync all now", "Run health check"],
                intro="Quick actions on this page",
            )
        )
        global_help.setObjectName("cardSummary")
        global_help.setWordWrap(True)
        hdr_layout.addWidget(global_help)
        layout.addWidget(header)

        # ── Service cards grid ─────────────────────────────────────────────────
        section_lbl = QLabel("Connected services")
        section_lbl.setObjectName("sectionTitle")
        layout.addWidget(section_lbl)

        # Placeholder grid — cards are inserted once plugins load
        self._cards_grid = QGridLayout()
        self._cards_grid.setHorizontalSpacing(14)
        self._cards_grid.setVerticalSpacing(14)
        layout.addLayout(self._cards_grid)

        # Placeholder label shown before plugins load
        self._loading_label = QLabel("Loading service status…")
        self._loading_label.setObjectName("cardSummary")
        layout.addWidget(self._loading_label)

        # ── Quick-start progress ──────────────────────────────────────────────
        self.setup_card = QFrame()
        self.setup_card.setObjectName("card")
        setup_layout = QVBoxLayout(self.setup_card)
        setup_layout.setContentsMargins(16, 12, 16, 12)
        setup_layout.setSpacing(6)
        setup_lbl = QLabel("Quick-start progress")
        setup_lbl.setObjectName("sectionTitle")
        setup_layout.addWidget(setup_lbl)
        self.setup_summary_label = QLabel("Checking your saved setup…")
        self.setup_summary_label.setWordWrap(True)
        setup_layout.addWidget(self.setup_summary_label)
        self.setup_next_step_label = QLabel("")
        self.setup_next_step_label.setObjectName("cardMeta")
        self.setup_next_step_label.setWordWrap(True)
        setup_layout.addWidget(self.setup_next_step_label)

        setup_actions = QHBoxLayout()
        continue_setup_btn = QPushButton("Continue setup")
        continue_setup_btn.setObjectName("primaryAction")
        continue_setup_btn.clicked.connect(lambda: self.main_window.show_setup_page())
        setup_actions.addWidget(continue_setup_btn)
        open_history_btn = QPushButton("Open health & history")
        open_history_btn.clicked.connect(lambda: self.main_window.navigate_to_page("Health & history"))
        setup_actions.addWidget(open_history_btn)
        setup_actions.addStretch(1)
        setup_layout.addLayout(setup_actions)
        layout.addWidget(self.setup_card)

        # ── Last-activity summary ─────────────────────────────────────────────
        self.activity_card = QFrame()
        self.activity_card.setObjectName("card")
        act_layout = QVBoxLayout(self.activity_card)
        act_layout.setContentsMargins(16, 12, 16, 12)
        act_layout.setSpacing(4)
        act_lbl = QLabel("Latest activity")
        act_lbl.setObjectName("sectionTitle")
        act_layout.addWidget(act_lbl)
        self.activity_label = QLabel("Waiting for the first sync…")
        self.activity_label.setObjectName("activityHeadline")
        self.activity_label.setWordWrap(True)
        act_layout.addWidget(self.activity_label)
        self.activity_meta_label = QLabel("")
        self.activity_meta_label.setObjectName("cardMeta")
        self.activity_meta_label.hide()
        act_layout.addWidget(self.activity_meta_label)
        layout.addWidget(self.activity_card)

        layout.addStretch(1)
        self._layout_ref = layout

    def build_service_cards(self):
        """Called once after plugins are registered. Inserts cards into the grid."""
        if self._service_cards:
            return  # Already built

        registry = self.main_window.registry
        plugin_map = {p.plugin_id: p for p in registry.all()}

        # Build cards for the five primary dashboard services, in order.
        # Any extra services from the registry are shown after.
        ordered_ids = [pid for pid in _DASHBOARD_SERVICES if pid in plugin_map]
        remaining = [p for p in registry.all() if p.plugin_id not in _DASHBOARD_SERVICES]

        all_to_show = [plugin_map[pid] for pid in ordered_ids] + remaining

        for i, plugin in enumerate(all_to_show):
            card = ServiceSummaryCard(plugin, accent_color=getattr(plugin, "accent_color", "#3b82f6"))
            # When "Open →" is clicked, navigate sidebar to the matching page
            page_label = plugin.nav_label
            card.open_requested.connect(lambda label=page_label: self._navigate_to(label))
            self._service_cards[plugin.plugin_id] = card
            row, col = divmod(i, 2)
            self._cards_grid.addWidget(card, row, col)

        self._cards_grid.setColumnStretch(0, 1)
        self._cards_grid.setColumnStretch(1, 1)

        if all_to_show:
            self._loading_label.hide()

    def _navigate_to(self, nav_label: str):
        nav = self.main_window.nav_list
        for i in range(nav.count()):
            if nav.item(i).text() == nav_label:
                nav.setCurrentRow(i)
                return

    def refresh(self):
        tray = getattr(self.main_window, "tray_app", None)

        # Build cards if not done yet (plugins may have loaded after __init__)
        if not self._service_cards and self.main_window._pages_loaded:
            self.build_service_cards()

        # Refresh each card
        for card in self._service_cards.values():
            try:
                card.refresh()
            except Exception:
                pass

        try:
            progress = tray.get_setup_progress() if tray and hasattr(tray, "get_setup_progress") else {}
            self.setup_summary_label.setText(progress.get("summary") or "Setup progress will appear here.")
            next_step = progress.get("next_step") or ""
            self.setup_next_step_label.setText(f"Next step: {next_step}" if next_step else "")
        except Exception:
            pass

        # Activity summary
        try:
            sync_status = tray.get_last_sync_status_text() if tray and hasattr(tray, "get_last_sync_status_text") else ""
            maintenance_state = None
            maintenance_manager = getattr(tray, "maintenance_manager", None)
            if maintenance_manager and hasattr(maintenance_manager, "get_status_snapshot"):
                maintenance_state = maintenance_manager.get_status_snapshot()

            headline, meta = build_compact_activity_text(
                getattr(tray, "last_summary", "") if tray else "",
                sync_status,
                maintenance_state,
            )
            self.activity_label.setText(headline)
            self.activity_meta_label.setVisible(bool(meta))
            self.activity_meta_label.setText(meta)
        except Exception:
            pass



class NotionModulePage(QWidget):
    def __init__(self, main_window, plugin: ServicePlugin):
        super().__init__()
        self.main_window = main_window
        self.plugin = plugin
        self.metric_cards: dict[str, MetricCard] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 18, 18, 18)
        header_layout.setSpacing(8)

        eyebrow = QLabel("Notion")
        eyebrow.setObjectName("eyebrow")
        header_layout.addWidget(eyebrow)

        title = QLabel("Notion sync")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)

        intro = QLabel(
            "Two-way sync between Notion and your local database. "
            "Browse saved pages, make local edits, and keep your AI command-center link up to date."
        )
        intro.setWordWrap(True)
        header_layout.addWidget(intro)

        self.status_badge = QLabel()
        self.status_badge.setObjectName("statusBadgeGrey")
        header_layout.addWidget(self.status_badge, alignment=Qt.AlignLeft)
        layout.addWidget(header)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        metrics.setVerticalSpacing(12)
        self.metric_cards["connection"] = MetricCard("Connection", "Checking...", "Database and Notion readiness")
        self.metric_cards["vault"] = MetricCard("Local Vault", "Checking...", "Mirrored pages and storage")
        self.metric_cards["queue"] = MetricCard("Push Queue", "Checking...", "Changes waiting to send")
        self.metric_cards["mcp"] = MetricCard("Claude MCP", "Checking...", "Desktop command center link")
        metrics.addWidget(self.metric_cards["connection"], 0, 0)
        metrics.addWidget(self.metric_cards["vault"], 0, 1)
        metrics.addWidget(self.metric_cards["queue"], 1, 0)
        metrics.addWidget(self.metric_cards["mcp"], 1, 1)
        layout.addLayout(metrics)

        sync_card = QFrame()
        sync_card.setObjectName("card")
        sync_layout = QVBoxLayout(sync_card)
        sync_layout.setContentsMargins(18, 18, 18, 18)
        sync_layout.setSpacing(10)
        sync_layout.addWidget(QLabel("<b>Sync controls</b>"))

        sync_row = QHBoxLayout()
        sync_row.addWidget(self._make_button("Sync now", lambda: self.main_window.tray_app.run_sync("sync"), primary=True))
        sync_row.addWidget(self._make_button("Resync Notion", self.resync_notion_databases))
        sync_row.addStretch(1)
        sync_layout.addLayout(sync_row)

        advanced_hint = QLabel(
            "One-way import and push tools are in <b>Advanced tools</b> so everyday sync stays safely front and centre."
        )
        advanced_hint.setWordWrap(True)
        sync_layout.addWidget(advanced_hint)

        tools_row = QHBoxLayout()
        tools_row.addWidget(self._make_button("Browse saved pages", self.main_window.open_database_browser_page))
        tools_row.addWidget(self._make_button("Edit a page locally", self.main_window.queue_local_edit))
        tools_row.addWidget(self._make_button("Bulk search & replace", self.main_window.open_bulk_replace))
        tools_row.addStretch(1)
        sync_layout.addLayout(tools_row)
        layout.addWidget(sync_card)

        support_card = QFrame()
        support_card.setObjectName("card")
        support_layout = QVBoxLayout(support_card)
        support_layout.setContentsMargins(18, 18, 18, 18)
        support_layout.setSpacing(10)
        support_lbl = QLabel("Health checks")
        support_lbl.setObjectName("sectionTitle")
        support_layout.addWidget(support_lbl)

        support_hint = QLabel(
            _format_action_help(
                ["Check access", "Run health check", "Back up now", "Configure Claude MCP"],
                intro="What these buttons do",
            )
        )
        support_hint.setObjectName("cardSummary")
        support_hint.setWordWrap(True)
        support_layout.addWidget(support_hint)

        support_row = QHBoxLayout()
        support_row.addWidget(self._make_button("Check access", lambda: self.main_window.tray_app.check_notion_access(show_message=False)))
        support_row.addWidget(self._make_button("Run health check", lambda: self.main_window.tray_app.run_health_check(show_message=False)))
        support_row.addWidget(self._make_button("Back up now", lambda: self.main_window.tray_app.run_backup_now(silent=True)))
        support_row.addWidget(self._make_button("Configure Claude MCP", self.main_window.tray_app.configure_mcp))
        support_row.addStretch(1)
        support_layout.addLayout(support_row)
        layout.addWidget(support_card)

        config_card = QFrame()
        config_card.setObjectName("card")
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 18, 18, 18)
        config_layout.setSpacing(10)
        config_lbl = QLabel("Notion configuration")
        config_lbl.setObjectName("sectionTitle")
        config_layout.addWidget(config_lbl)
        config_hint = QLabel("Change your Notion token, choose which databases to sync, or update timing settings.")
        config_hint.setWordWrap(True)
        config_layout.addWidget(config_hint)
        config_layout.addWidget(self._make_button("Configure Notion", lambda: self.main_window.show_setup_page("notion"), primary=True))
        layout.addWidget(config_card)

        status_card = QFrame()
        status_card.setObjectName("card")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(18, 18, 18, 18)
        status_layout.setSpacing(8)
        status_lbl = QLabel("Current status")
        status_lbl.setObjectName("sectionTitle")
        status_layout.addWidget(status_lbl)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        status_layout.addWidget(self.summary_label)

        self.access_label = QLabel()
        self.access_label.setWordWrap(True)
        status_layout.addWidget(self.access_label)

        self.backup_label = QLabel()
        self.backup_label.setWordWrap(True)
        status_layout.addWidget(self.backup_label)

        self.activity_label = QLabel()
        self.activity_label.setWordWrap(True)
        status_layout.addWidget(self.activity_label)
        layout.addWidget(status_card)

        layout.addStretch(1)

    def resync_notion_databases(self):
        tray = getattr(self.main_window, "tray_app", None)
        if tray and callable(getattr(tray, "run_connection_resync", None)):
            tray.run_connection_resync("notion")
            return

        QMessageBox.information(
            self,
            "Resync Notion",
            "This build cannot run the Notion resync yet. Please use Setup to refresh your saved Notion access.",
        )

    @staticmethod
    def _make_button(label: str, callback, primary: bool = False) -> QPushButton:
        button = QPushButton(label)
        if primary:
            button.setObjectName("primaryAction")
        if callable(callback):
            button.clicked.connect(callback)
        else:
            button.setEnabled(False)
        return button

    def refresh(self):
        tray = self.main_window.tray_app
        db = self.main_window.db
        try:
            stats = tray.get_runtime_stats() if hasattr(tray, "get_runtime_stats") else (db.get_stats() if db else {})
        except Exception:
            stats = {}
        status = self.plugin.get_status()
        badge_style = _badge_style_for_state(status.state)
        self.status_badge.setObjectName(badge_style)
        self.status_badge.setText(status.state)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

        connected = bool(stats.get("connected"))
        target = get_env("NOTION_DB_ID", "Not configured") or "Not configured"
        self.metric_cards["connection"].update_content(
            "Ready" if connected else "Not set up",
            f"Target: {target}",
        )
        self.metric_cards["vault"].update_content(
            f"{int(stats.get('active_pages', 0) or 0)} mirrored page(s)",
            stats.get("database_size_text", "Database size not available yet"),
        )
        self.metric_cards["queue"].update_content(
            str(int(stats.get("pending_push", 0) or 0)),
            "Local change(s) waiting to push back to Notion",
        )
        self.metric_cards["mcp"].update_content(
            get_claude_mcp_name(),
            tray.get_mcp_status_text() if hasattr(tray, "get_mcp_status_text") else "MCP status not available",
        )

        self.summary_label.setText(
            f"Summary: <b>{status.summary}</b><br>"
            f"Latest app status: {getattr(tray, 'last_summary', 'Ready')}"
        )
        self.access_label.setText(f"Notion access: {tray.get_notion_access_summary()}")
        self.backup_label.setText(f"Backup: {tray.get_backup_summary_text()}")
        activity_text = tray.get_last_sync_status_text() if hasattr(tray, "get_last_sync_status_text") else tray.last_sync_action.text()
        self.activity_label.setText(activity_text)


class AdvancedToolsPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 18, 18, 18)
        header_layout.setSpacing(8)

        eyebrow = QLabel("Use with care")
        eyebrow.setObjectName("eyebrow")
        header_layout.addWidget(eyebrow)

        title = QLabel("Advanced tools")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)

        intro = QLabel(
            "These tools let you do a one-way import or push instead of a full two-way sync. "
            "For everyday use, <b>Sync now</b> on the Notion page is the right choice."
        )
        intro.setWordWrap(True)
        header_layout.addWidget(intro)
        layout.addWidget(header)

        warning_card = QFrame()
        warning_card.setObjectName("warningCard")
        warning_layout = QVBoxLayout(warning_card)
        warning_layout.setContentsMargins(18, 18, 18, 18)
        warning_layout.setSpacing(10)

        badge = QLabel("One-way only")
        badge.setObjectName("warningBadge")
        warning_layout.addWidget(badge, alignment=Qt.AlignLeft)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        warning_layout.addWidget(self.summary_label)

        warning_help = QLabel(
            _format_action_help(
                ["Import from Notion only", "Push local changes only"],
                intro="What these one-way tools do",
            )
        )
        warning_help.setObjectName("cardSummary")
        warning_help.setWordWrap(True)
        warning_layout.addWidget(warning_help)

        button_row = QHBoxLayout()
        button_row.addWidget(self._make_button("Import from Notion only", lambda: self.main_window.tray_app.run_sync("pull"), warning=True))
        button_row.addWidget(self._make_button("Push local changes only", lambda: self.main_window.tray_app.run_sync("push"), warning=True))
        button_row.addStretch(1)
        warning_layout.addLayout(button_row)
        layout.addWidget(warning_card)

        support_card = QFrame()
        support_card.setObjectName("card")
        support_layout = QVBoxLayout(support_card)
        support_layout.setContentsMargins(18, 18, 18, 18)
        support_layout.setSpacing(10)
        support_lbl = QLabel("Health checks")
        support_lbl.setObjectName("sectionTitle")
        support_layout.addWidget(support_lbl)

        support_hint = QLabel(
            _format_action_help(
                ["Run health check", "Check Notion access"],
                intro="What these checks do",
            )
        )
        support_hint.setObjectName("cardSummary")
        support_hint.setWordWrap(True)
        support_layout.addWidget(support_hint)

        support_row = QHBoxLayout()
        support_row.addWidget(self._make_button("Run health check", lambda: self.main_window.tray_app.run_health_check(show_message=False)))
        support_row.addWidget(self._make_button("Check Notion access", lambda: self.main_window.tray_app.check_notion_access(show_message=False)))
        support_row.addStretch(1)
        support_layout.addLayout(support_row)
        layout.addWidget(support_card)

        db_card = QFrame()
        db_card.setObjectName("card")
        db_layout = QVBoxLayout(db_card)
        db_layout.setContentsMargins(18, 18, 18, 18)
        db_layout.setSpacing(10)
        db_lbl = QLabel("Database maintenance")
        db_lbl.setObjectName("sectionTitle")
        db_layout.addWidget(db_lbl)
        db_hint = QLabel("Run a VACUUM ANALYZE to speed up database queries after lots of changes.")
        db_hint.setWordWrap(True)
        db_layout.addWidget(db_hint)
        db_row = QHBoxLayout()
        db_row.addWidget(self._make_button("Speed up database", self.main_window.tray_app.run_vacuum_analyze if callable(getattr(self.main_window.tray_app, "run_vacuum_analyze", None)) else None))
        db_row.addStretch(1)
        db_layout.addLayout(db_row)
        layout.addWidget(db_card)

        layout.addStretch(1)

    @staticmethod
    def _make_button(label: str, callback, primary: bool = False, warning: bool = False) -> QPushButton:
        button = QPushButton(label)
        if warning:
            button.setObjectName("warningAction")
        elif primary:
            button.setObjectName("primaryAction")
        if callable(callback):
            button.clicked.connect(callback)
        else:
            button.setEnabled(False)
        return button

    def refresh(self):
        tray = getattr(self.main_window, "tray_app", None)
        try:
            stats = tray.get_runtime_stats() if tray and hasattr(tray, "get_runtime_stats") else (self.main_window.db.get_stats() if self.main_window.db else {})
        except Exception:
            stats = {}
        target = get_env("NOTION_DB_ID", "Not configured") or "Not configured"
        pending_push = int(stats.get("pending_push", 0) or 0)
        self.summary_label.setText(
            f"Current target: <code>{target}</code><br>"
            f"Queued local changes waiting to push: <b>{pending_push}</b><br>"
            "Use these only when you deliberately want a one-way action instead of the safer two-way smart sync."
        )


class ServiceModulePage(QWidget):
    def __init__(self, plugin: ServicePlugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 18, 18, 18)
        header_layout.setSpacing(8)

        eyebrow = QLabel(self.plugin.display_name)
        eyebrow.setObjectName("eyebrow")
        header_layout.addWidget(eyebrow)

        title = QLabel(self.plugin.display_name)
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)

        description = QLabel(self.plugin.description)
        description.setWordWrap(True)
        header_layout.addWidget(description)

        self.status_badge = QLabel()
        self.status_badge.setObjectName("statusBadgeGrey")
        header_layout.addWidget(self.status_badge, alignment=Qt.AlignLeft)
        layout.addWidget(header)

        details_card = QFrame()
        details_card.setObjectName("card")
        details_layout = QVBoxLayout(details_card)
        details_layout.setContentsMargins(18, 18, 18, 18)
        details_layout.setSpacing(8)
        details_lbl = QLabel("Status")
        details_lbl.setObjectName("sectionTitle")
        details_layout.addWidget(details_lbl)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        details_layout.addWidget(self.summary_label)

        self.details_label = QLabel()
        self.details_label.setWordWrap(True)
        details_layout.addWidget(self.details_label)
        layout.addWidget(details_card)

        actions_card = QFrame()
        actions_card.setObjectName("card")
        actions_layout = QVBoxLayout(actions_card)
        actions_layout.setContentsMargins(18, 18, 18, 18)
        actions_layout.setSpacing(8)
        actions_lbl = QLabel("What you can do")
        actions_lbl.setObjectName("sectionTitle")
        actions_layout.addWidget(actions_lbl)

        self.button_row = QHBoxLayout()
        actions_layout.addLayout(self.button_row)

        self.actions_help_label = QLabel("")
        self.actions_help_label.setObjectName("cardSummary")
        self.actions_help_label.setWordWrap(True)
        self.actions_help_label.hide()
        actions_layout.addWidget(self.actions_help_label)
        layout.addWidget(actions_card)

        config_card = QFrame()
        config_card.setObjectName("card")
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 18, 18, 18)
        config_layout.setSpacing(8)
        config_lbl = QLabel("Configuration")
        config_lbl.setObjectName("sectionTitle")
        config_layout.addWidget(config_lbl)
        config_hint = QLabel(f"Set up credentials and connection details for {self.plugin.display_name}.")
        config_hint.setWordWrap(True)
        config_layout.addWidget(config_hint)
        self._config_btn_row = QHBoxLayout()
        config_layout.addLayout(self._config_btn_row)
        layout.addWidget(config_card)

        layout.addStretch(1)

    def refresh(self):
        try:
            status = self.plugin.get_status()
        except Exception:
            status = ServiceStatus(state="Connecting…", summary="Waiting for the database to connect.", details=[])

        badge_style = _badge_style_for_state(status.state)
        self.status_badge.setObjectName(badge_style)
        self.status_badge.setText(status.state)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

        self.summary_label.setText(status.summary)
        self.details_label.setText("<br>".join(f"• {item}" for item in status.details))

        while self.button_row.count():
            item = self.button_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        actions = self.plugin.get_quick_actions()
        main_actions = []
        config_action = None
        for action in actions:
            action_label = action.label.lower()
            if config_action is None and ("setup" in action_label or "config" in action_label):
                config_action = action
            else:
                main_actions.append(action)

        for action in main_actions:
            button = QPushButton(action.label)
            if action.primary:
                button.setObjectName("primaryAction")
            if callable(action.callback):
                button.clicked.connect(action.callback)
            else:
                button.setEnabled(False)
            self.button_row.addWidget(button)
        self.button_row.addStretch(1)

        action_help = _format_action_help([action.label for action in main_actions])
        self.actions_help_label.setVisible(bool(action_help))
        self.actions_help_label.setText(action_help)

        while self._config_btn_row.count():
            item = self._config_btn_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if config_action:
            btn_label = "Setup" if "setup" in config_action.label.lower() else config_action.label
            btn = QPushButton(btn_label)
            btn.setObjectName("primaryAction")
            if callable(config_action.callback):
                btn.clicked.connect(config_action.callback)
            else:
                btn.setEnabled(False)
            self._config_btn_row.addWidget(btn)
        self._config_btn_row.addStretch(1)


class LocalEditPage(QWidget):
    """Queue a local Notion page edit without opening popup dialogs."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self._pages: list[dict] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 18, 18, 18)
        header_layout.setSpacing(8)
        eyebrow = QLabel("Notion")
        eyebrow.setObjectName("eyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel("Queue a local edit")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)
        intro = QLabel(
            "Choose a mirrored page, update the local title or notes here, then push the reviewed change later when you are ready."
        )
        intro.setWordWrap(True)
        header_layout.addWidget(intro)
        layout.addWidget(header)

        form_card = QFrame()
        form_card.setObjectName("card")
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(18, 18, 18, 18)
        form_layout.setSpacing(10)

        form_layout.addWidget(QLabel("<b>Page to edit</b>"))
        self.page_combo = QComboBox()
        self.page_combo.currentIndexChanged.connect(self._populate_selected_page)
        form_layout.addWidget(self.page_combo)

        form_layout.addWidget(QLabel("<b>Local title</b>"))
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Title to store locally")
        form_layout.addWidget(self.title_input)

        form_layout.addWidget(QLabel("<b>Local notes / summary</b>"))
        self.summary_input = QPlainTextEdit()
        self.summary_input.setPlaceholderText("Optional notes to queue for the next push")
        self.summary_input.setMinimumHeight(180)
        form_layout.addWidget(self.summary_input)

        action_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh list")
        self.refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(self.refresh_btn)

        self.save_btn = QPushButton("Queue local edit")
        self.save_btn.setObjectName("primaryAction")
        self.save_btn.clicked.connect(self.save_edit)
        action_row.addWidget(self.save_btn)
        action_row.addStretch(1)
        form_layout.addLayout(action_row)

        self.status_label = QLabel("Pick a page to start editing.")
        self.status_label.setObjectName("cardSummary")
        self.status_label.setWordWrap(True)
        form_layout.addWidget(self.status_label)
        layout.addWidget(form_card)
        layout.addStretch(1)

    def _selected_page(self) -> dict:
        data = self.page_combo.currentData()
        return data if isinstance(data, dict) else {}

    def _populate_selected_page(self, _index: int = 0):
        page = self._selected_page()
        self.title_input.setText((page.get("title") or "Untitled") if page else "")
        self.summary_input.setPlainText(page.get("ai_summary") or "" if page else "")
        if page:
            self.status_label.setText(
                f"Editing <b>{page.get('title') or 'Untitled'}</b>. This change stays local until you run a push or smart sync."
            )

    def refresh(self):
        db = getattr(self.main_window, "db", None)
        current_page = self._selected_page()
        current_notion_id = current_page.get("notion_id") if current_page else None

        if not db or (not getattr(db, "conn", None) and not db.connect()):
            self._pages = []
            self.page_combo.blockSignals(True)
            self.page_combo.clear()
            self.page_combo.addItem("Database setup still needed", None)
            self.page_combo.blockSignals(False)
            self.page_combo.setEnabled(False)
            self.save_btn.setEnabled(False)
            self.title_input.clear()
            self.summary_input.clear()
            self.status_label.setText("Please finish the database setup first, then come back here to queue a local edit.")
            return

        pages = db.list_editable_pages() if hasattr(db, "list_editable_pages") else []
        self._pages = list(pages or [])
        self.page_combo.blockSignals(True)
        self.page_combo.clear()

        if not self._pages:
            self.page_combo.addItem("No mirrored pages yet", None)
            self.page_combo.setEnabled(False)
            self.save_btn.setEnabled(False)
            self.title_input.clear()
            self.summary_input.clear()
            self.status_label.setText("There are no mirrored pages yet. Run an import from Notion first.")
            self.page_combo.blockSignals(False)
            return

        selected_index = 0
        for index, page in enumerate(self._pages):
            title = (page.get("title") or "Untitled").strip() or "Untitled"
            notion_id = page.get("notion_id", "")
            suffix = " — pending push" if page.get("needs_push") else ""
            label = f"{title} ({notion_id}){suffix}" if notion_id else f"{title}{suffix}"
            self.page_combo.addItem(label, page)
            if current_notion_id and notion_id == current_notion_id:
                selected_index = index

        self.page_combo.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.page_combo.setCurrentIndex(selected_index)
        self.page_combo.blockSignals(False)
        self._populate_selected_page(selected_index)

    def focus_form(self, notion_id: str | None = None):
        self.refresh()
        if notion_id:
            for index in range(self.page_combo.count()):
                page = self.page_combo.itemData(index)
                if isinstance(page, dict) and page.get("notion_id") == notion_id:
                    self.page_combo.setCurrentIndex(index)
                    break
        self.title_input.setFocus()
        self.title_input.selectAll()

    def save_edit(self):
        page = self._selected_page()
        notion_id = page.get("notion_id", "") if page else ""
        if not notion_id:
            self.status_label.setText("Pick a page first, then queue the local edit.")
            return

        title = self.title_input.text().strip() or (page.get("title") or "Untitled")
        summary = self.summary_input.toPlainText()
        success = bool(self.main_window.tray_app.queue_local_edit(notion_id, title, summary))
        message = getattr(self.main_window.tray_app, "last_summary", "") or (
            "Local edit queued." if success else "Local edit could not be queued."
        )
        self.status_label.setText(message)
        if success:
            self.focus_form(notion_id=notion_id)
            self.main_window.refresh_view()


class BulkReplacePage(QWidget):
    """Run bulk text replacements without opening modal dialogs."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(18, 18, 18, 18)
        header_layout.setSpacing(8)
        eyebrow = QLabel("Notion")
        eyebrow.setObjectName("eyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel("Bulk search & replace")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)
        intro = QLabel(
            "Preview the matches in your local mirror first, then apply the reviewed change when it looks right."
        )
        intro.setWordWrap(True)
        header_layout.addWidget(intro)
        layout.addWidget(header)

        form_card = QFrame()
        form_card.setObjectName("card")
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(18, 18, 18, 18)
        form_layout.setSpacing(10)

        form_layout.addWidget(QLabel("<b>Find</b>"))
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("Text to search for")
        form_layout.addWidget(self.find_input)

        form_layout.addWidget(QLabel("<b>Replace with</b>"))
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Replacement text")
        form_layout.addWidget(self.replace_input)

        options_row = QHBoxLayout()
        self.title_check = QCheckBox("Titles")
        self.title_check.setChecked(True)
        options_row.addWidget(self.title_check)
        self.summary_check = QCheckBox("Notes / summaries")
        self.summary_check.setChecked(True)
        options_row.addWidget(self.summary_check)
        self.case_check = QCheckBox("Match case")
        options_row.addWidget(self.case_check)
        options_row.addStretch(1)
        form_layout.addLayout(options_row)

        self.review_check = QCheckBox("I reviewed the preview and want to queue this local change")
        form_layout.addWidget(self.review_check)

        button_row = QHBoxLayout()
        self.preview_btn = QPushButton("Preview matches")
        self.preview_btn.clicked.connect(self.run_preview)
        button_row.addWidget(self.preview_btn)

        self.apply_btn = QPushButton("Apply locally")
        self.apply_btn.setObjectName("primaryAction")
        self.apply_btn.clicked.connect(self.apply_changes)
        button_row.addWidget(self.apply_btn)
        button_row.addStretch(1)
        form_layout.addLayout(button_row)

        self.status_label = QLabel("Preview the change first, then tick the review box to apply it locally.")
        self.status_label.setObjectName("cardSummary")
        self.status_label.setWordWrap(True)
        form_layout.addWidget(self.status_label)

        self.results_box = QPlainTextEdit()
        self.results_box.setReadOnly(True)
        self.results_box.setPlaceholderText("Preview results will appear here.")
        self.results_box.setMinimumHeight(220)
        form_layout.addWidget(self.results_box)
        layout.addWidget(form_card)
        layout.addStretch(1)

    def _selected_fields(self) -> list[str]:
        fields = []
        if self.title_check.isChecked():
            fields.append("title")
        if self.summary_check.isChecked():
            fields.append("ai_summary")
        return fields or ["title", "ai_summary"]

    def refresh(self):
        db = getattr(self.main_window, "db", None)
        ready = bool(db and (getattr(db, "conn", None) or db.connect()))
        self.preview_btn.setEnabled(ready)
        self.apply_btn.setEnabled(ready)
        if not ready:
            self.status_label.setText("Please finish the database setup first, then come back here to run a bulk replace.")

    def focus_form(self):
        self.refresh()
        self.find_input.setFocus()
        self.find_input.selectAll()

    def run_preview(self):
        preview = self.main_window.tray_app.preview_bulk_replace(
            self.find_input.text(),
            self.replace_input.text(),
            fields=self._selected_fields(),
            case_sensitive=self.case_check.isChecked(),
        )

        if not preview.get("search_text"):
            self.status_label.setText("Type the text you want to find first.")
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
        self.status_label.setText(
            f"Preview ready: {preview.get('total_matches', 0)} replacement(s) across {preview.get('affected_pages', 0)} page(s)."
        )

    def apply_changes(self):
        find_text = self.find_input.text().strip()
        if not find_text:
            self.status_label.setText("Type the text you want to replace first.")
            return

        preview = self.main_window.tray_app.preview_bulk_replace(
            find_text,
            self.replace_input.text(),
            fields=self._selected_fields(),
            case_sensitive=self.case_check.isChecked(),
        )
        affected_pages = int(preview.get("affected_pages", 0) or 0)
        total_matches = int(preview.get("total_matches", 0) or 0)

        if total_matches <= 0:
            self.results_box.setPlainText("No matches were found in the local mirror.")
            self.status_label.setText("No matching text was found, so nothing was changed.")
            return

        if not self.review_check.isChecked():
            self.status_label.setText(
                f"Preview found {total_matches} replacement(s) across {affected_pages} page(s). Tick the review box, then click Apply locally again."
            )
            return

        success, message = self.main_window.tray_app.apply_bulk_replace(
            find_text,
            self.replace_input.text(),
            fields=self._selected_fields(),
            case_sensitive=self.case_check.isChecked(),
        )
        self.results_box.setPlainText(message)
        self.status_label.setText(message)
        if success:
            self.review_check.setChecked(False)
            self.main_window.refresh_view()


class HealthHistoryPage(QWidget):
    """Central health, progress, and recent-activity view for the v2 dashboard."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self._next_setup_service: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 18, 20, 18)
        header_layout.setSpacing(4)
        eyebrow = QLabel("Status centre")
        eyebrow.setObjectName("eyebrow")
        header_layout.addWidget(eyebrow)
        title = QLabel("Health & history")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)
        intro = QLabel(
            "A single place to see what is healthy, what still needs setup, and what the app did most recently."
        )
        intro.setWordWrap(True)
        header_layout.addWidget(intro)
        sub = QLabel(f"{get_app_version()} · {get_app_tagline()}")
        sub.setObjectName("cardMeta")
        sub.setWordWrap(True)
        header_layout.addWidget(sub)
        layout.addWidget(header)

        health_card = QFrame()
        health_card.setObjectName("card")
        health_layout = QVBoxLayout(health_card)
        health_layout.setContentsMargins(18, 16, 18, 16)
        health_layout.setSpacing(8)
        health_title = QLabel("Current health")
        health_title.setObjectName("sectionTitle")
        health_layout.addWidget(health_title)
        self.health_headline_label = QLabel("Waiting for the first refresh…")
        self.health_headline_label.setObjectName("activityHeadline")
        self.health_headline_label.setWordWrap(True)
        health_layout.addWidget(self.health_headline_label)
        self.health_summary_label = QLabel("")
        self.health_summary_label.setWordWrap(True)
        health_layout.addWidget(self.health_summary_label)
        self.health_items_label = QLabel("")
        self.health_items_label.setObjectName("cardMeta")
        self.health_items_label.setWordWrap(True)
        self.health_items_label.setTextFormat(Qt.RichText)
        health_layout.addWidget(self.health_items_label)
        self.next_action_label = QLabel("")
        self.next_action_label.setWordWrap(True)
        health_layout.addWidget(self.next_action_label)
        health_actions = QHBoxLayout()
        run_check_btn = QPushButton("Run health check")
        run_check_btn.clicked.connect(lambda: self.main_window.tray_app.run_health_check(show_message=False))
        health_actions.addWidget(run_check_btn)
        sync_now_btn = QPushButton("Sync all now")
        sync_now_btn.setObjectName("primaryAction")
        sync_now_btn.clicked.connect(lambda: self.main_window.tray_app.run_sync("sync"))
        health_actions.addWidget(sync_now_btn)
        open_logs_btn = QPushButton("Open logs")
        open_logs_btn.clicked.connect(self.main_window.tray_app.open_logs)
        health_actions.addWidget(open_logs_btn)
        health_actions.addStretch(1)
        health_layout.addLayout(health_actions)
        layout.addWidget(health_card)

        progress_card = QFrame()
        progress_card.setObjectName("card")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(18, 16, 18, 16)
        progress_layout.setSpacing(8)
        progress_title = QLabel("Setup progress")
        progress_title.setObjectName("sectionTitle")
        progress_layout.addWidget(progress_title)
        self.progress_summary_label = QLabel("Checking your saved setup…")
        self.progress_summary_label.setWordWrap(True)
        progress_layout.addWidget(self.progress_summary_label)
        self.progress_items_label = QLabel("")
        self.progress_items_label.setObjectName("cardMeta")
        self.progress_items_label.setWordWrap(True)
        self.progress_items_label.setTextFormat(Qt.RichText)
        progress_layout.addWidget(self.progress_items_label)
        self.continue_setup_btn = QPushButton("Continue setup")
        self.continue_setup_btn.setObjectName("primaryAction")
        self.continue_setup_btn.clicked.connect(self._open_next_setup_step)
        progress_layout.addWidget(self.continue_setup_btn, alignment=Qt.AlignLeft)
        layout.addWidget(progress_card)

        ai_card = QFrame()
        ai_card.setObjectName("card")
        ai_layout = QVBoxLayout(ai_card)
        ai_layout.setContentsMargins(18, 16, 18, 16)
        ai_layout.setSpacing(8)
        ai_title = QLabel("Safe AI change flow")
        ai_title.setObjectName("sectionTitle")
        ai_layout.addWidget(ai_title)
        ai_hint = QLabel(
            "Use the safer flow: preview → review → apply locally → push when ready. This keeps AI-assisted edits easy to inspect before anything goes back online."
        )
        ai_hint.setWordWrap(True)
        ai_layout.addWidget(ai_hint)
        ai_actions = QHBoxLayout()
        ai_actions.addWidget(QPushButton("Queue local edit"))
        ai_actions.itemAt(0).widget().clicked.connect(lambda: self.main_window.queue_local_edit())
        bulk_btn = QPushButton("Bulk replace")
        bulk_btn.clicked.connect(self.main_window.open_bulk_replace)
        ai_actions.addWidget(bulk_btn)
        ai_actions.addStretch(1)
        ai_layout.addLayout(ai_actions)
        layout.addWidget(ai_card)

        jobs_card = QFrame()
        jobs_card.setObjectName("card")
        jobs_layout = QVBoxLayout(jobs_card)
        jobs_layout.setContentsMargins(18, 16, 18, 16)
        jobs_layout.setSpacing(8)
        jobs_title = QLabel("Recent jobs")
        jobs_title.setObjectName("sectionTitle")
        jobs_layout.addWidget(jobs_title)
        self.recent_jobs_label = QLabel("No recent jobs yet.")
        self.recent_jobs_label.setWordWrap(True)
        self.recent_jobs_label.setTextFormat(Qt.RichText)
        jobs_layout.addWidget(self.recent_jobs_label)
        layout.addWidget(jobs_card)

        layout.addStretch(1)

    def _open_next_setup_step(self):
        service = self._next_setup_service or None
        self.main_window.show_setup_page(service)

    def refresh(self):
        tray = getattr(self.main_window, "tray_app", None)
        if not tray:
            return

        snapshot = tray.get_health_snapshot() if hasattr(tray, "get_health_snapshot") else {}
        self.health_headline_label.setText(snapshot.get("headline") or "Waiting for the first refresh…")
        self.health_summary_label.setText(snapshot.get("summary") or "No recent sync has been recorded yet.")
        item_lines = [
            f"• <b>{item.get('label', 'Status')}</b>: {item.get('value', '')}"
            for item in (snapshot.get("items") or [])
            if item.get("value")
        ]
        self.health_items_label.setText("<br>".join(item_lines) or "Run a health check to fill this in.")
        next_action = snapshot.get("next_action") or ""
        self.next_action_label.setText(f"Next best step: {next_action}" if next_action else "")

        progress = tray.get_setup_progress() if hasattr(tray, "get_setup_progress") else {}
        self.progress_summary_label.setText(progress.get("summary") or "Setup progress will appear here.")
        progress_lines = [
            f"{'✓' if item.get('ready') else '○'} <b>{item.get('label', 'Step')}</b> — {item.get('detail', '')}"
            for item in (progress.get("items") or [])
        ]
        self.progress_items_label.setText("<br>".join(progress_lines) or "No setup details available yet.")
        self._next_setup_service = progress.get("next_service")
        if self._next_setup_service:
            self.continue_setup_btn.setText("Continue recommended step")
            self.continue_setup_btn.setEnabled(True)
        else:
            self.continue_setup_btn.setText("Core setup looks ready")
            self.continue_setup_btn.setEnabled(False)

        recent_jobs = tray.get_recent_jobs(limit=8) if hasattr(tray, "get_recent_jobs") else []
        job_lines = [
            f"• <b>{job.get('label', 'Task')}</b> — {job.get('summary', '')} <span style='color:#94a3b8'>({job.get('when_text', 'recently')})</span>"
            for job in recent_jobs
        ]
        self.recent_jobs_label.setText(
            "<br>".join(job_lines) or "No recent jobs yet. Run a sync, backup, or health check to populate this history."
        )


class SettingsPage(QWidget):
    """App-wide settings and general maintenance controls."""

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)

        # ── Header ────────────────────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("card")
        hdr_layout = QVBoxLayout(header)
        hdr_layout.setContentsMargins(20, 18, 20, 18)
        hdr_layout.setSpacing(4)

        eyebrow = QLabel("App settings")
        eyebrow.setObjectName("eyebrow")
        hdr_layout.addWidget(eyebrow)

        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        hdr_layout.addWidget(title)

        intro = QLabel(
            "Manage how the app behaves on this computer and take care of backups here. "
            "If you want to connect or adjust a service, use that service page on the left."
        )
        intro.setWordWrap(True)
        hdr_layout.addWidget(intro)
        layout.addWidget(header)

        # ── Startup ───────────────────────────────────────────────────────────
        startup_card = QFrame()
        startup_card.setObjectName("card")
        startup_layout = QVBoxLayout(startup_card)
        startup_layout.setContentsMargins(18, 16, 18, 16)
        startup_layout.setSpacing(10)

        startup_lbl = QLabel("Start with Windows")
        startup_lbl.setObjectName("sectionTitle")
        startup_layout.addWidget(startup_lbl)

        startup_hint = QLabel(
            "Keep this on to have the app open automatically when you sign in. "
            "You can turn it off here any time."
        )
        startup_hint.setWordWrap(True)
        startup_layout.addWidget(startup_hint)

        startup_row = QHBoxLayout()
        self.startup_toggle = QCheckBox("Open this app when Windows starts")
        self.startup_toggle.setChecked(is_windows_startup_enabled())
        self.startup_toggle.toggled.connect(self._set_windows_startup_enabled)
        startup_row.addWidget(self.startup_toggle)
        startup_row.addStretch(1)
        startup_layout.addLayout(startup_row)

        self.startup_status_label = QLabel("")
        self.startup_status_label.setObjectName("cardSummary")
        self.startup_status_label.setWordWrap(True)
        startup_layout.addWidget(self.startup_status_label)
        layout.addWidget(startup_card)
        self._refresh_startup_status()

        tray = main_window.tray_app

        # ── Backups & Logs ────────────────────────────────────────────────────
        support_card = QFrame()
        support_card.setObjectName("card")
        support_layout = QVBoxLayout(support_card)
        support_layout.setContentsMargins(18, 16, 18, 16)
        support_layout.setSpacing(10)
        support_lbl = QLabel("Backups and logs")
        support_lbl.setObjectName("sectionTitle")
        support_layout.addWidget(support_lbl)
        support_hint = QLabel("Back up your local database now, or open the activity logs to see what happened recently.")
        support_hint.setWordWrap(True)
        support_layout.addWidget(support_hint)

        support_row = QHBoxLayout()
        backup_btn = QPushButton("Back up now")
        if callable(getattr(tray, "run_backup_now", None)):
            backup_btn.clicked.connect(lambda: tray.run_backup_now(silent=True))
        else:
            backup_btn.setEnabled(False)
        support_row.addWidget(backup_btn)

        logs_btn = QPushButton("Open logs")
        if callable(getattr(tray, "open_logs", None)):
            logs_btn.clicked.connect(tray.open_logs)
        else:
            logs_btn.setEnabled(False)
        support_row.addWidget(logs_btn)
        support_row.addStretch(1)
        support_layout.addLayout(support_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("cardSummary")
        self.status_label.setWordWrap(True)
        support_layout.addWidget(self.status_label)
        layout.addWidget(support_card)

        layout.addStretch(1)

    def _refresh_startup_status(self):
        enabled = is_windows_startup_enabled()
        toggle_text = "On" if enabled else "Off"
        detail = "The app will open when Windows starts." if enabled else "The app will stay off until you open it yourself."
        self.startup_status_label.setText(f"{toggle_text} — {detail}")

    def _set_windows_startup_enabled(self, enabled: bool):
        save_env_var("WINDOWS_STARTUP_ENABLED", "1" if enabled else "0")

        try:
            success, message = sync_windows_startup(enabled)
        except Exception as exc:
            success = False
            message = f"Could not update the Windows start-up setting: {exc}"

        self._refresh_startup_status()
        if not success and message:
            self.startup_status_label.setText(message)

    def refresh(self):
        if hasattr(self, "startup_toggle"):
            self.startup_toggle.blockSignals(True)
            self.startup_toggle.setChecked(is_windows_startup_enabled())
            self.startup_toggle.blockSignals(False)
            self._refresh_startup_status()

        tray = getattr(self.main_window, "tray_app", None)
        if not tray:
            return
        parts = []
        if hasattr(tray, "get_backup_summary_text"):
            parts.append(f"Backup: {tray.get_backup_summary_text()}")
        maintenance = getattr(tray, "maintenance_manager", None)
        if maintenance:
            parts.append(f"Night clean-up: {maintenance.get_status_text()}")
        self.status_label.setText("  ·  ".join(parts))


class MainWindow(QMainWindow):
    def __init__(self, tray_app, parent=None):
        super().__init__(parent)
        self.tray_app = tray_app
        self.db = getattr(tray_app, "db", None)
        self.registry = PluginRegistry()
        self.service_pages: list[ServiceModulePage] = []
        self._pages_loaded = False

        self.current_status_message = "Connecting to database…"
        self.setWindowTitle(f"{get_app_display_name()} — {get_app_version()}")
        self.setMinimumSize(1240, 780)
        self.setStyleSheet(build_dark_stylesheet())
        self.statusBar().showMessage(self.current_status_message)

        central = QWidget()
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Sidebar ───────────────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 20, 12, 16)
        sidebar_layout.setSpacing(4)

        brand = QLabel(get_app_display_name())
        brand.setStyleSheet("font-size: 15pt; font-weight: 700; padding: 4px 8px;")
        brand.setWordWrap(True)
        sidebar_layout.addWidget(brand)

        brand_meta = QLabel(f"{get_app_version()} · {get_app_tagline()}")
        brand_meta.setObjectName("cardMeta")
        brand_meta.setWordWrap(True)
        sidebar_layout.addWidget(brand_meta)

        nav_hint = QLabel("Choose a section")
        nav_hint.setObjectName("cardMeta")
        nav_hint.setWordWrap(True)
        sidebar_layout.addWidget(nav_hint)

        sidebar_layout.addSpacing(8)

        self.nav_list = QListWidget()
        sidebar_layout.addWidget(self.nav_list, 1)

        settings_btn = QPushButton("⚙  Settings")
        settings_btn.clicked.connect(self._navigate_to_settings)
        sidebar_layout.addWidget(settings_btn)
        root.addWidget(sidebar)

        # ── Page stack ────────────────────────────────────────────────────────
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        # Dashboard, setup, and settings are always available immediately.
        self.overview_page = OverviewPage(self)
        self._add_page("Dashboard", self.overview_page)

        self.health_page = HealthHistoryPage(self)
        self._add_page("Health & history", self.health_page)

        on_complete = getattr(self.tray_app, "on_wizard_complete", lambda: None)
        self.setup_page = SetupWizard(self.db, on_complete, tray_app=self.tray_app, embedded=True)
        self._setup_index = self.nav_list.count()
        self._add_page("Setup", self.setup_page)

        self.settings_page = SettingsPage(self)
        self._settings_index = self.nav_list.count()
        self._add_page("Settings", self.settings_page)

        self.database_browser_page = None
        self.local_edit_page = None
        self.bulk_replace_page = None
        self.advanced_page = None

        self.nav_list.currentRowChanged.connect(self._on_nav_row_changed)
        self.nav_list.setCurrentRow(0)

    def _on_nav_row_changed(self, row: int):
        item = self.nav_list.item(row)
        if item is None:
            return

        stack_index = item.data(Qt.UserRole)
        if isinstance(stack_index, int) and 0 <= stack_index < self.stack.count():
            self.stack.setCurrentIndex(stack_index)

    def _navigate_to_settings(self):
        if self._settings_index is not None:
            self.nav_list.setCurrentRow(self._settings_index)

    def navigate_to_page(self, label: str) -> bool:
        for index in range(self.nav_list.count()):
            item = self.nav_list.item(index)
            if item and item.text() == label and item.flags() != Qt.NoItemFlags:
                self.nav_list.setCurrentRow(index)
                return True
        return False

    def show_setup_page(self, service: str | None = None):
        if service and hasattr(self.setup_page, "_open_service"):
            self.setup_page._open_service(service)
        elif hasattr(self.setup_page, "_go_to_picker"):
            self.setup_page._go_to_picker()
        self.navigate_to_page("Setup")

    def _ensure_embedded_tool_pages(self):
        if self.database_browser_page is None:
            self.database_browser_page = DatabaseBrowserWindow(self.tray_app, embedded=True)
            self._add_page("Database browser", self.database_browser_page)

        if self.local_edit_page is None:
            self.local_edit_page = LocalEditPage(self)
            self._add_page("Local edit", self.local_edit_page)

        if self.bulk_replace_page is None:
            self.bulk_replace_page = BulkReplacePage(self)
            self._add_page("Bulk replace", self.bulk_replace_page)

    def open_database_browser_page(self):
        self._ensure_embedded_tool_pages()
        if self.database_browser_page and hasattr(self.database_browser_page, "refresh_entries"):
            self.database_browser_page.refresh_entries()
        self.navigate_to_page("Database browser")

    def _load_full_pages(self):
        """Build plugin registry and add all service/advanced/recovery/settings pages.
        Uses processEvents() between steps so the window stays responsive."""
        if self._pages_loaded:
            return
        self._pages_loaded = True

        try:
            self.registry = build_default_registry(tray_app=self.tray_app, db_manager=self.db)
        except Exception:
            return

        app = QApplication.instance()

        # Add a visual section separator item for "Services"
        services_header = QListWidgetItem("SERVICES")
        services_header.setFlags(Qt.NoItemFlags)
        services_header.setForeground(Qt.darkGray)
        self.nav_list.addItem(services_header)

        for plugin in self.registry.all():
            try:
                if plugin.plugin_id == "notion":
                    page = NotionModulePage(self, plugin)
                else:
                    page = ServiceModulePage(plugin)
                self.service_pages.append(page)
                self._add_page(plugin.nav_label, page)
            except Exception:
                pass
            if app:
                app.processEvents()

        # Add a visual section separator item for "Tools"
        tools_header = QListWidgetItem("TOOLS")
        tools_header.setFlags(Qt.NoItemFlags)
        tools_header.setForeground(Qt.darkGray)
        self.nav_list.addItem(tools_header)

        self._ensure_embedded_tool_pages()
        if app:
            app.processEvents()

        try:
            self.advanced_page = AdvancedToolsPage(self)
            self._add_page("Advanced tools", self.advanced_page)
        except Exception:
            self.advanced_page = None
        if app:
            app.processEvents()

        try:
            recovery_page = TimeMachineWindow(self.tray_app, embedded=True)
            self._add_page("Time Machine", recovery_page)
        except Exception:
            pass
        if app:
            app.processEvents()

        # Tell the dashboard to build its service cards now that plugins exist
        try:
            self.overview_page.build_service_cards()
        except Exception:
            pass

        self.current_status_message = "Ready"
        self.statusBar().showMessage(self.current_status_message)

    def apply_branding(self):
        self.setWindowTitle(f"{get_app_display_name()} — {get_app_version()}")

    def set_status(self, message: str):
        self.current_status_message = str(message or "Ready")
        self.statusBar().showMessage(self.current_status_message)
        self.refresh_view()

    def update_activity_status(self, message: str):
        self.current_status_message = str(message or "Ready")
        self.statusBar().showMessage(self.current_status_message)
        try:
            self.overview_page.activity_label.setText(self.current_status_message)
        except Exception:
            pass

    def set_busy(self, busy: bool):
        for button in self.findChildren(QPushButton):
            button.setEnabled(not busy)
        self.statusBar().showMessage(("Working… " if busy else "Ready — ") + self.current_status_message)

    def refresh_status(self):
        # Only update status bar text; do NOT call refresh_view() here
        # to avoid re-entering the heavy page-load/refresh cascade.
        self.statusBar().showMessage(self.current_status_message)

    def open_bulk_replace(self):
        self._ensure_embedded_tool_pages()
        self.navigate_to_page("Bulk replace")
        if self.bulk_replace_page and hasattr(self.bulk_replace_page, "focus_form"):
            self.bulk_replace_page.focus_form()

    def queue_local_edit(self, notion_id: str | None = None):
        self._ensure_embedded_tool_pages()
        self.navigate_to_page("Local edit")
        if self.local_edit_page and hasattr(self.local_edit_page, "focus_form"):
            self.local_edit_page.focus_form(notion_id=notion_id)

    def _add_page(self, label: str, widget: QWidget):
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, self.stack.count())
        self.nav_list.addItem(item)
        self.stack.addWidget(widget)

    def refresh_view(self):
        # Lazily create full page set once DB is connected
        if not self._pages_loaded and self.db and getattr(self.db, "conn", None):
            self._load_full_pages()

        app = QApplication.instance()
        # Refresh each page with processEvents() between to keep UI responsive
        for page in self._get_all_refreshable_pages():
            try:
                page.refresh()
            except Exception:
                pass
            if app:
                app.processEvents()

    def _get_all_refreshable_pages(self):
        pages = [self.overview_page, self.health_page, self.settings_page]
        for page in (
            self.database_browser_page,
            self.local_edit_page,
            self.bulk_replace_page,
            self.advanced_page,
        ):
            if page is not None:
                pages.append(page)
        pages.extend(self.service_pages)
        return pages

    def _drain_next_refresh(self):
        # Legacy — kept for compatibility; refresh_view now handles this.
        pass
