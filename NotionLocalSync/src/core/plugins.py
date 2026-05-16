from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Callable

from src.core.config import get_claude_mcp_name, get_env, get_secret
from src.core.maintenance import MaintenanceManager
from src.sync.service_engines import PlannedServiceSyncEngine


@dataclass(slots=True)
class QuickAction:
    label: str
    callback: Callable[[], None] | None = None
    primary: bool = False


@dataclass(slots=True)
class ServiceStatus:
    state: str
    summary: str
    details: list[str] = field(default_factory=list)
    meta: str = ""


# Dashboard state language is intentionally distinct:
# - Not set up: missing credentials or required setup details
# - Connected: the service is linked, but the first sync/check still needs to happen
# - Ready: the service is fully usable now

def _format_status_time(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value) if value else "Never"


class ServicePlugin(ABC):
    plugin_id = "service"
    display_name = "Service"
    nav_label = "Service"
    schema_name = "service"
    description = "Pluggable service module."
    accent_color = "#7dd3fc"

    def __init__(self, tray_app=None, db_manager=None):
        self.tray_app = tray_app
        self.db_manager = db_manager
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        self._initialized = True
        if self.db_manager and getattr(self.db_manager, "conn", None) and hasattr(self.db_manager, "ensure_service_schema"):
            self.db_manager.ensure_service_schema(
                self.schema_name,
                display_name=self.display_name,
                description=self.description,
            )

    def get_status(self) -> ServiceStatus:
        return ServiceStatus(
            state="Ready",
            summary="The module shell is ready with local storage, status tracking, and quick actions.",
            details=[f"Database schema reserved: {self.schema_name}"],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        return []


class PluginRegistry:
    def __init__(self):
        self._plugins: list[ServicePlugin] = []

    def register(self, plugin: ServicePlugin):
        self._plugins = [item for item in self._plugins if item.plugin_id != plugin.plugin_id]
        self._plugins.append(plugin)
        # Do NOT call plugin.initialize() here — it runs DB queries
        # that block the UI thread.  Initialization happens lazily
        # when get_status() or a quick-action first needs it.

    def all(self) -> list[ServicePlugin]:
        return list(self._plugins)


class NotionPlugin(ServicePlugin):
    plugin_id = "notion"
    display_name = "Notion"
    nav_label = "Notion"
    schema_name = "notion"
    description = "Two-way sync shell for the current Notion local mirror and MCP workflow."
    accent_color = "#8b5cf6"

    def get_status(self) -> ServiceStatus:
        try:
            if self.tray_app and hasattr(self.tray_app, "get_runtime_stats"):
                stats = self.tray_app.get_runtime_stats()
            elif self.db_manager and callable(getattr(self.db_manager, "get_stats", None)):
                stats = self.db_manager.get_stats()
            else:
                stats = {}
        except Exception:
            stats = {}
        connected = bool(stats.get("connected"))
        target = get_env("NOTION_DB_ID", "Not configured") or "Not configured"
        maintenance_manager = getattr(self.tray_app, "maintenance_manager", None)
        maintenance = maintenance_manager.get_task_readiness(MaintenanceManager.TASK_NOTION) if maintenance_manager else {}
        last_synced_at = _format_status_time(stats.get("last_push_at") or stats.get("last_pull_at"))
        state = "Ready" if connected else "Not set up"
        summary = (
            "The Notion module is ready to sync against the local PostgreSQL mirror and run the built-in housekeeping rules."
            if connected
            else "Finish the database and Notion setup to enable live sync and automated maintenance."
        )
        return ServiceStatus(
            state=state,
            summary=summary,
            meta=f"Last synced: {last_synced_at}",
            details=[
                f"Target databases: {target}",
                f"Active mirrored pages: {int(stats.get('active_pages', 0) or 0)}",
                f"Pending push queue: {int(stats.get('pending_push', 0) or 0)}",
                f"Maintenance: {maintenance.get('summary', 'Notion housekeeping status not available yet.')}",
                f"Claude MCP profile: {get_claude_mcp_name()}",
            ],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        tray = self.tray_app
        actions: list[QuickAction] = []
        if tray:
            actions.extend(
                [
                    QuickAction("Run Smart Sync", lambda: tray.run_sync("sync"), primary=True),
                    QuickAction("Resync Notion", lambda: tray.run_connection_resync("notion")),
                    QuickAction("Import only", lambda: tray.run_sync("pull")),
                    QuickAction("Push local changes", lambda: tray.run_sync("push")),
                    QuickAction("Run maintenance", lambda: tray.run_manual_maintenance("full_notion_dedup")),
                    QuickAction("Check access", getattr(tray, "check_notion_access", None)),
                    QuickAction("Run health check", getattr(tray, "run_health_check", None)),
                    QuickAction("Configure MCP", getattr(tray, "configure_mcp", None)),
                ]
            )
        return actions


class ClaudeMcpPlugin(ServicePlugin):
    plugin_id = "claude_mcp"
    display_name = "Claude (MCP)"
    nav_label = "Claude (MCP)"
    schema_name = "claude_mcp"
    description = "Connect Claude Desktop to this app so your AI tools can safely work with the local mirror."
    accent_color = "#14b8a6"

    def get_status(self) -> ServiceStatus:
        tray = self.tray_app
        status_text = (
            tray.get_mcp_status_text()
            if tray and callable(getattr(tray, "get_mcp_status_text", None))
            else "MCP status not available yet"
        )
        normalized = str(status_text or "").lower()

        if normalized.startswith("configured as"):
            state = "Ready"
            summary = "Claude Desktop is linked and ready to use this app through MCP."
        elif "not configured yet" in normalized or "appdata not available" in normalized or "missing" in normalized:
            state = "Not set up"
            summary = "Add the Claude Desktop link here to let your AI tools talk directly to this app."
        else:
            state = "Connected"
            summary = "The Claude Desktop connection file exists, but it may need a quick review."

        return ServiceStatus(
            state=state,
            summary=summary,
            details=[
                f"Claude profile name: {get_claude_mcp_name()}",
                f"Status: {status_text}",
            ],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        tray = self.tray_app
        actions: list[QuickAction] = []
        if tray:
            if callable(getattr(tray, "configure_mcp", None)):
                actions.append(QuickAction("Configure Claude MCP", tray.configure_mcp, primary=True))
            if callable(getattr(tray, "open_logs", None)):
                actions.append(QuickAction("Open logs", tray.open_logs))
        return actions


class N8nPlugin(ServicePlugin):
    plugin_id = "n8n"
    display_name = "n8n"
    nav_label = "n8n"
    schema_name = "n8n"
    description = "Workflow backup, restore, and audit module for your n8n automations."
    accent_color = "#22c55e"

    def get_status(self) -> ServiceStatus:
        try:
            stats = self.db_manager.get_n8n_status() if self.db_manager and callable(getattr(self.db_manager, "get_n8n_status", None)) else {}
        except Exception:
            stats = {}
        endpoint = str(get_env("N8N_API_URL", "") or get_env("N8N_BASE_URL", "")).strip() or "Not configured yet"
        api_ready = bool(str(endpoint).strip() and endpoint != "Not configured yet" and get_secret("N8N_API_KEY"))
        current_workflows = int(stats.get("current_workflows", 0) or 0)
        enabled_workflows = int(stats.get("enabled_workflows", 0) or 0)
        total_versions = int(stats.get("total_versions", 0) or 0)
        last_synced_at = _format_status_time(stats.get("last_synced_at"))

        live_archive_ready = bool(
            stats.get("connected") and (current_workflows > 0 or total_versions > 0 or last_synced_at != "Never")
        )

        if live_archive_ready and api_ready:
            state = "Ready"
        elif live_archive_ready or api_ready:
            state = "Connected"
        else:
            state = "Not set up"

        summary = str(stats.get("last_sync_summary") or "").strip()
        if not summary:
            summary = (
                "Run a sync to pull your n8n workflows into the local vault and keep restore points handy."
                if api_ready
                else "Add your n8n API URL and key to start backing up workflows locally."
            )

        return ServiceStatus(
            state=state,
            summary=summary,
            meta=f"Last synced: {last_synced_at}",
            details=[
                f"API endpoint: {endpoint}",
                f"Current workflows: {current_workflows} | Enabled: {enabled_workflows}",
                f"Saved workflow versions: {total_versions}",
                f"Last sync: {last_synced_at}",
            ],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        tray = self.tray_app
        actions: list[QuickAction] = []
        if tray:
            if callable(getattr(tray, "run_n8n_sync", None)):
                actions.append(QuickAction("Sync n8n now", tray.run_n8n_sync, primary=True))
            if callable(getattr(tray, "run_backup_now", None)):
                actions.append(QuickAction("Run backup now", lambda: tray.run_backup_now(silent=False)))
            if callable(getattr(tray, "show_wizard", None)):
                actions.append(QuickAction("Setup", lambda: tray.show_wizard("n8n")))
        return actions


class WordPressPlugin(ServicePlugin):
    plugin_id = "wordpress"
    display_name = "WordPress"
    nav_label = "WordPress"
    schema_name = "wordpress"
    description = "Site clean-up and content health module for connected WordPress sites."
    accent_color = "#f97316"

    def get_status(self) -> ServiceStatus:
        maintenance_manager = getattr(self.tray_app, "maintenance_manager", None)
        readiness = maintenance_manager.get_task_readiness(MaintenanceManager.TASK_WORDPRESS) if maintenance_manager else {}
        state_snapshot = maintenance_manager.get_status_snapshot().get(MaintenanceManager.TASK_WORDPRESS, {}) if maintenance_manager and hasattr(maintenance_manager, "get_status_snapshot") else {}

        site_url = str(get_env("WORDPRESS_URL", "")).strip() or "Not configured yet"
        site_label = str(get_env("WORDPRESS_SITE_LABEL", "WordPress site") or "WordPress site").strip() or "WordPress site"
        old_draft_days = str(get_env("WORDPRESS_OLD_DRAFT_DAYS", "120") or "120").strip() or "120"
        trash_days = str(get_env("WORDPRESS_TRASH_RETENTION_DAYS", "30") or "30").strip() or "30"

        if readiness.get("disabled"):
            state = "Turned off"
        else:
            state = "Ready" if readiness.get("ready") else "Not set up"

        summary = readiness.get("summary") or (
            "WordPress clean-up tools are ready to help manage drafts, spam, trash, and old media."
            if site_url != "Not configured yet"
            else "Add your WordPress site details to unlock the built-in clean-up tools."
        )
        last_run = state_snapshot.get("last_run_at")
        last_run_text = _format_status_time(last_run) if last_run else "Never"

        return ServiceStatus(
            state=state,
            summary=summary,
            meta=f"Last synced: {last_run_text}",
            details=[
                f"Site: {site_label} — {site_url}",
                f"Last run: {state_snapshot.get('last_summary', 'No WordPress clean-up has run yet.')}",
                f"Draft retention: {old_draft_days} day(s) | Trash retention: {trash_days} day(s)",
            ],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        tray = self.tray_app
        actions: list[QuickAction] = []
        if tray:
            if callable(getattr(tray, "run_manual_maintenance", None)):
                actions.append(QuickAction("Run WordPress clean-up", lambda: tray.run_manual_maintenance("full_wordpress_dedup"), primary=True))
            if callable(getattr(tray, "show_wizard", None)):
                actions.append(QuickAction("Setup", lambda: tray.show_wizard("wordpress")))
            if callable(getattr(tray, "run_backup_now", None)):
                actions.append(QuickAction("Run backup now", lambda: tray.run_backup_now(silent=False)))
        return actions


class MauticPlugin(ServicePlugin):
    plugin_id = "mautic"
    display_name = "Mautic"
    nav_label = "Mautic"
    schema_name = "mautic"
    description = "Marketing contact and campaign archive module for Mautic workspaces."
    accent_color = "#f59e0b"

    def get_status(self) -> ServiceStatus:
        try:
            stats = self.db_manager.get_mautic_stats() if self.db_manager and callable(getattr(self.db_manager, "get_mautic_stats", None)) else {}
        except Exception:
            stats = {}
        base_url = str(get_env("MAUTIC_BASE_URL", "")).strip() or "Not configured yet"
        token_ready = bool(get_secret("MAUTIC_ACCESS_TOKEN"))
        oauth_ready = bool(str(get_env("MAUTIC_CLIENT_ID", "")).strip() and get_secret("MAUTIC_CLIENT_SECRET"))
        login_ready = bool(str(get_env("MAUTIC_USERNAME", "")).strip() and get_secret("MAUTIC_PASSWORD"))
        archive_ready = token_ready or oauth_ready or login_ready or bool(str(get_env("MAUTIC_SSH_HOST", "")).strip())
        contact_count = int(stats.get("contact_count", 0) or 0)
        campaign_count = int(stats.get("campaign_count", 0) or 0)
        last_synced_at = _format_status_time(stats.get("last_synced_at"))

        if contact_count > 0 or campaign_count > 0 or last_synced_at != "Never":
            state = "Ready"
        elif archive_ready and base_url != "Not configured yet":
            state = "Connected"
        else:
            state = "Not set up"

        summary = str(stats.get("last_sync_summary") or "").strip()
        if not summary:
            summary = (
                "The local archive is ready for Mautic contacts, campaigns, and recovery snapshots."
                if archive_ready
                else "Add your Mautic URL plus API or SSH details to prepare the archive layer."
            )

        return ServiceStatus(
            state=state,
            summary=summary,
            meta=f"Last synced: {last_synced_at}",
            details=[
                f"Mautic URL: {base_url}",
                f"Archived contacts: {contact_count} | Campaigns: {campaign_count}",
                f"Last archive refresh: {last_synced_at}",
            ],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        tray = self.tray_app
        actions: list[QuickAction] = []
        if tray:
            if callable(getattr(tray, "show_wizard", None)):
                actions.append(QuickAction("Setup", lambda: tray.show_wizard("mautic"), primary=True))
            if callable(getattr(tray, "run_backup_now", None)):
                actions.append(QuickAction("Run backup now", lambda: tray.run_backup_now(silent=False)))
            if callable(getattr(tray, "open_logs", None)):
                actions.append(QuickAction("Open logs", tray.open_logs))
        return actions


class RoadmapServicePlugin(ServicePlugin):
    def __init__(
        self,
        plugin_id: str,
        display_name: str,
        description: str,
        accent_color: str,
        tray_app=None,
        db_manager=None,
    ):
        super().__init__(tray_app=tray_app, db_manager=db_manager)
        self.plugin_id = plugin_id
        self.display_name = display_name
        self.nav_label = display_name
        self.schema_name = plugin_id.lower().replace("-", "_")
        self.description = description
        self.accent_color = accent_color
        self.engine = PlannedServiceSyncEngine(
            service_key=self.schema_name,
            display_name=self.display_name,
            future_focus=[
                f"Dedicated {self.display_name} credentials and connection flow",
                "Per-service pull / push jobs",
                "Recovery snapshots and audit history",
            ],
        )

    def get_status(self) -> ServiceStatus:
        maintenance_task_map = {
            "clickup": MaintenanceManager.TASK_CLICKUP,
            "wordpress": MaintenanceManager.TASK_WORDPRESS,
        }
        task_name = maintenance_task_map.get(self.plugin_id)
        maintenance_manager = getattr(self.tray_app, "maintenance_manager", None)

        if self.plugin_id == "clickup":
            try:
                stats = self.db_manager.get_clickup_status() if self.db_manager and callable(getattr(self.db_manager, "get_clickup_status", None)) else {}
            except Exception:
                stats = {}
            readiness = maintenance_manager.get_task_readiness(MaintenanceManager.TASK_CLICKUP) if maintenance_manager else {}
            webhook_enabled = str(get_env("CLICKUP_WEBHOOK_ENABLED", "1")).strip().lower() not in {"0", "false", "off", "no"}
            webhook_host = str(get_env("CLICKUP_WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
            webhook_port = str(get_env("CLICKUP_WEBHOOK_PORT", "8765") or "8765").strip() or "8765"
            webhook_path = str(get_env("CLICKUP_WEBHOOK_PATH", "/clickup/webhook") or "/clickup/webhook").strip() or "/clickup/webhook"
            if not webhook_path.startswith("/"):
                webhook_path = f"/{webhook_path}"

            token_ready = bool(get_env("CLICKUP_TOKEN", "") or get_env("CLICKUP_TOKEN_ENCRYPTED", ""))
            configured_lists = str(get_env("CLICKUP_LIST_IDS", "") or "").strip()
            syncs_all_lists = configured_lists.upper() == "ALL"
            list_count = len([part for part in configured_lists.split(",") if part.strip() and part.strip().upper() != "ALL"])
            if stats.get("connected") and (int(stats.get("current_tasks", 0) or 0) > 0 or int(stats.get("current_comments", 0) or 0) > 0):
                state = "Ready"
            elif token_ready and (syncs_all_lists or list_count > 0):
                state = "Connected"
            else:
                state = "Not set up"

            summary = stats.get("last_sync_summary") or readiness.get(
                "summary",
                "Add your ClickUp token and list IDs to start pulling tasks, comments, and webhook updates into the local vault.",
            )
            return ServiceStatus(
                state=state,
                summary=summary,
                meta=f"Last synced: {_format_status_time(stats.get('last_synced_at'))}",
                details=[
                    f"Reserved PostgreSQL schema: {self.schema_name}",
                    f"Mirrored tasks: {int(stats.get('current_tasks', 0) or 0)} | Active comments: {int(stats.get('current_comments', 0) or 0)}",
                    f"Webhook listener: {'On' if webhook_enabled else 'Off'} — http://{webhook_host}:{webhook_port}{webhook_path}",
                    (
                        "Manual sync scope: all accessible ClickUp lists will be discovered automatically"
                        if syncs_all_lists
                        else f"Manual sync scope: {list_count} ClickUp list(s) configured"
                    ),
                    f"Maintenance: {readiness.get('summary', 'ClickUp tidy-up status is not available yet.')}",
                ],
            )

        if task_name and maintenance_manager:
            readiness = maintenance_manager.get_task_readiness(task_name)
            state_snapshot = maintenance_manager.get_status_snapshot().get(task_name, {})
            if readiness.get("disabled"):
                state = "Turned off"
            else:
                state = "Ready" if readiness.get("ready") else "Not set up"

            return ServiceStatus(
                state=state,
                summary=readiness.get("summary", f"{self.display_name} maintenance status is not available yet."),
                details=[
                    f"Reserved PostgreSQL schema: {self.schema_name}",
                    f"Current automation: {readiness.get('summary', 'No maintenance summary yet.')}",
                    f"Last run: {state_snapshot.get('last_summary', 'No maintenance has run yet.')}",
                ],
            )

        snapshot = self.engine.snapshot()
        return ServiceStatus(
            state=snapshot.readiness,
            summary=snapshot.summary,
            details=[
                f"Reserved PostgreSQL schema: {self.schema_name}",
                *snapshot.capabilities,
            ],
        )

    def get_quick_actions(self) -> list[QuickAction]:
        tray = self.tray_app
        if not tray:
            return []

        actions: list[QuickAction] = []
        if self.plugin_id == "clickup":
            if callable(getattr(tray, "run_clickup_sync", None)):
                actions.append(QuickAction("Sync ClickUp now", tray.run_clickup_sync, primary=True))
            if callable(getattr(tray, "run_connection_resync", None)):
                actions.append(QuickAction("Resync ClickUp", lambda: tray.run_connection_resync("clickup")))
            if callable(getattr(tray, "copy_clickup_webhook_details", None)):
                actions.append(QuickAction("Copy webhook details", tray.copy_clickup_webhook_details))
            if callable(getattr(tray, "run_manual_maintenance", None)):
                actions.append(QuickAction("Run ClickUp tidy-up", lambda: tray.run_manual_maintenance("full_clickup_dedup")))
            if callable(getattr(tray, "show_wizard", None)):
                actions.append(QuickAction("Setup", lambda: tray.show_wizard("clickup")))
            return actions

        if callable(getattr(tray, "show_wizard", None)):
            actions.append(QuickAction("Setup", lambda: tray.show_wizard(self.plugin_id), primary=True))
        if callable(getattr(tray, "run_backup_now", None)):
            actions.append(QuickAction("Run backup now", lambda: tray.run_backup_now(silent=False)))
        return actions


def build_default_registry(tray_app=None, db_manager=None) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(NotionPlugin(tray_app=tray_app, db_manager=db_manager))
    registry.register(
        RoadmapServicePlugin(
            plugin_id="clickup",
            display_name="ClickUp",
            description="Task sync and recovery module for ClickUp workspaces.",
            accent_color="#38bdf8",
            tray_app=tray_app,
            db_manager=db_manager,
        )
    )
    registry.register(WordPressPlugin(tray_app=tray_app, db_manager=db_manager))
    registry.register(N8nPlugin(tray_app=tray_app, db_manager=db_manager))
    registry.register(MauticPlugin(tray_app=tray_app, db_manager=db_manager))
    registry.register(ClaudeMcpPlugin(tray_app=tray_app, db_manager=db_manager))
    return registry
