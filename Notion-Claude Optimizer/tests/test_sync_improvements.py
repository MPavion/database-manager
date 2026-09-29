import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QLabel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src

from src.core.config import (
    build_windows_startup_command,
    get_app_display_name,
    get_claude_mcp_name,
    get_secret,
    is_windows_startup_enabled,
)
from src.core.http import build_retry_session
from src.core.plugins import N8nPlugin, PluginRegistry, ServiceStatus, build_default_registry
from src.core.maintenance import MaintenanceManager
from src.core.backup_manager import BackupManager
from src.db.change_tracking import normalize_workspace_page_json, property_preview_value
from src.db.database import DatabaseManager
from src.db.schema_loader import load_schema_sql
from src.sync.engine import ClickUpSyncEngine, N8nSyncEngine, SyncEngine
from src.ui.dashboard import AdminDashboard
from src.ui.database_browser import DatabaseBrowserWindow
from src.ui.main_window import (
    MainWindow,
    NotionModulePage,
    OverviewPage,
    ServiceSummaryCard,
    SettingsPage,
    _badge_style_for_state,
    build_compact_activity_text,
)
from src.ui.time_machine import TimeMachineWindow
from src.ui.tray import TrayApp
from src.ui.wizard import SetupWizard


class SecurityAndResilienceTests(unittest.TestCase):
    def test_configure_qt_environment_sets_existing_font_dir(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QT_QPA_FONTDIR", None)
            font_dir = src.configure_qt_environment()

        self.assertTrue(font_dir)
        self.assertTrue(Path(font_dir).exists())
        self.assertEqual(os.environ.get("QT_QPA_FONTDIR"), font_dir)

    @patch.dict(os.environ, {"CLICKUP_TOKEN_ENCRYPTED": "encrypted-value", "CLICKUP_TOKEN": "fallback"}, clear=False)
    def test_get_secret_prefers_encrypted_values(self):
        with patch("src.core.config.SecurityManager") as security_manager:
            security_manager.return_value.decrypt.return_value = "decrypted-token"
            self.assertEqual(get_secret("CLICKUP_TOKEN"), "decrypted-token")

    def test_build_retry_session_configures_http_retries(self):
        session = build_retry_session(total_retries=4, backoff_factor=0.3)
        adapter = session.get_adapter("https://")

        self.assertEqual(adapter.max_retries.total, 4)
        self.assertEqual(adapter.max_retries.backoff_factor, 0.3)
        self.assertIn("NotionLocalSync", session.headers.get("User-Agent", ""))


class DatabaseSyncDecisionTests(unittest.TestCase):
    def test_ensure_service_schema_handles_jsonb_default_sql(self):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, *args, **kwargs):
                return None

        class FakeConnection:
            def __init__(self):
                self.commit_calls = 0
                self.rollback_calls = 0

            def cursor(self, *args, **kwargs):
                return FakeCursor()

            def commit(self):
                self.commit_calls += 1

            def rollback(self):
                self.rollback_calls += 1

        db = DatabaseManager()
        db.conn = FakeConnection()

        self.assertTrue(db.ensure_service_schema("notion", display_name="Notion", description="Live sync module"))
        self.assertEqual(db.conn.commit_calls, 1)
        self.assertEqual(db.conn.rollback_calls, 0)

    def test_get_clickup_status_rolls_back_after_query_failure(self):
        class FakeCursor:
            def __init__(self):
                self.execute_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, *args, **kwargs):
                self.execute_calls += 1
                if self.execute_calls == 2:
                    raise RuntimeError("sync_runs missing")

            def fetchone(self):
                return {
                    "current_tasks": 4,
                    "open_tasks": 2,
                    "current_comments": 1,
                    "last_synced_at": None,
                }

        class FakeConnection:
            def __init__(self):
                self.rollback_calls = 0
                self.cursor_instance = FakeCursor()

            def cursor(self, *args, **kwargs):
                return self.cursor_instance

            def rollback(self):
                self.rollback_calls += 1

        db = DatabaseManager()
        db.conn = FakeConnection()

        with patch.object(db, "ensure_clickup_schema", return_value=True):
            status = db.get_clickup_status()

        self.assertFalse(status["connected"])
        self.assertEqual(db.conn.rollback_calls, 1)

    def test_get_stats_reuses_recent_snapshot_to_keep_refreshes_light(self):
        class FakeCursor:
            def __init__(self):
                self.execute_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, *args, **kwargs):
                self.execute_calls += 1

            def fetchone(self):
                return {
                    "active_pages": 12,
                    "pending_push": 3,
                    "total_versions": 18,
                    "pages_with_media": 4,
                    "last_pull_at": None,
                    "last_push_at": None,
                    "latest_local_update": None,
                    "latest_source_update": None,
                    "database_size_text": "24 MB",
                }

        class FakeConnection:
            def __init__(self):
                self.rollback_calls = 0
                self.cursor_instance = FakeCursor()

            def cursor(self, *args, **kwargs):
                return self.cursor_instance

            def rollback(self):
                self.rollback_calls += 1

        db = DatabaseManager()
        db.conn = FakeConnection()

        first = db.get_stats()
        second = db.get_stats()

        self.assertTrue(first["connected"])
        self.assertEqual(second["active_pages"], 12)
        self.assertEqual(db.conn.cursor_instance.execute_calls, 1)

    def test_get_stats_query_uses_notion_sync_runs_for_last_sync_times(self):
        class FakeCursor:
            def __init__(self):
                self.executed_sql = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, *args, **kwargs):
                self.executed_sql.append(str(query))

            def fetchone(self):
                return {
                    "active_pages": 0,
                    "pending_push": 0,
                    "total_versions": 0,
                    "pages_with_media": 0,
                    "last_pull_at": None,
                    "last_push_at": None,
                    "latest_local_update": None,
                    "latest_source_update": None,
                    "database_size_text": "1 MB",
                }

        class FakeConnection:
            def __init__(self):
                self.rollback_calls = 0
                self.cursor_instance = FakeCursor()

            def cursor(self, *args, **kwargs):
                return self.cursor_instance

            def rollback(self):
                self.rollback_calls += 1

        db = DatabaseManager()
        db.conn = FakeConnection()

        db.get_stats(force_refresh=True)

        combined_sql = "\n".join(db.conn.cursor_instance.executed_sql)
        self.assertIn("notion.sync_runs", combined_sql)

    def test_pull_and_push_log_successful_notion_runs_even_when_nothing_changes(self):
        class FakeDatabase:
            def __init__(self):
                self.logged_runs = []

            def get_active_pages(self, only_needs_push=False):
                return []

            def log_service_sync_run(self, service_key, status, summary, details=None):
                self.logged_runs.append(
                    {
                        "service_key": service_key,
                        "status": status,
                        "summary": summary,
                        "details": details or {},
                    }
                )
                return True

        db = FakeDatabase()
        engine = SyncEngine(db)
        engine.notion = object()

        with patch.object(engine, "_can_skip_workspace_pull", return_value=True):
            self.assertTrue(engine.pull_from_notion())

        self.assertTrue(engine.push_to_notion())
        self.assertEqual(len(db.logged_runs), 2)
        self.assertEqual(db.logged_runs[0]["service_key"], "notion")
        self.assertEqual(db.logged_runs[0]["status"], "success")
        self.assertEqual(db.logged_runs[0]["details"].get("mode"), "pull")
        self.assertEqual(db.logged_runs[1]["service_key"], "notion")
        self.assertEqual(db.logged_runs[1]["status"], "success")
        self.assertEqual(db.logged_runs[1]["details"].get("mode"), "push")

    def test_schema_guards_jsonb_array_walks_in_business_brain_queries(self):
        schema_sql = load_schema_sql()

        self.assertIn("CREATE OR REPLACE FUNCTION jsonb_safe_array", schema_sql)
        self.assertIn("CREATE OR REPLACE FUNCTION jsonb_safe_object", schema_sql)
        self.assertNotIn("DROP FUNCTION IF EXISTS jsonb_safe_array(JSONB);", schema_sql)
        self.assertNotIn("DROP FUNCTION IF EXISTS jsonb_safe_object(JSONB);", schema_sql)
        self.assertIn("jsonb_array_elements(jsonb_safe_array(prop.value -> 'title'))", schema_sql)
        self.assertIn("jsonb_array_elements(jsonb_safe_array(wm.raw_json -> '_business_brain_links' -> 'linked_resources'))", schema_sql)
        self.assertIn("FROM jsonb_each(jsonb_safe_object(wm.raw_json -> 'properties'))", schema_sql)
        self.assertIn("WHEN 'checkbox' THEN CASE", schema_sql)
        self.assertIn("WHEN prop.value ->> 'checkbox' IN ('true', 'false')", schema_sql)

    def test_normalize_workspace_page_json_repairs_malformed_property_shapes(self):
        page, issues = normalize_workspace_page_json(
            {
                "id": "32fb5266-4d9d-814d-b066-d42005cf38ac",
                "url": "https://www.notion.so/example/Page-32fb52664d9d814db066d42005cf38ac",
                "properties": {
                    "Name": {"type": "title", "title": {"plain_text": "Single object title"}},
                    "Notes": {"type": "rich_text", "rich_text": None},
                    "People": {"type": "people", "people": {"name": "Ada"}},
                    "Related": {"type": "relation", "relation": {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}},
                },
                "_business_brain_links": {"linked_notion_ids": {"bad": True}, "linked_resources": None},
            }
        )

        self.assertTrue(issues)
        self.assertIsInstance(page["properties"], dict)
        self.assertIsInstance(page["properties"]["Name"]["title"], list)
        self.assertEqual(page["properties"]["Name"]["title"][0]["plain_text"], "Single object title")
        self.assertEqual(page["properties"]["Notes"]["rich_text"], [])
        self.assertEqual(page["properties"]["People"]["people"][0]["name"], "Ada")
        self.assertEqual(page["properties"]["Related"]["relation"][0]["id"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(page["_business_brain_links"]["linked_notion_ids"], [])
        self.assertEqual(page["_business_brain_links"]["linked_resources"], [])

    def test_normalize_workspace_page_json_preserves_database_schema_objects(self):
        schema, issues = normalize_workspace_page_json(
            {
                "object": "data_source",
                "properties": {
                    "Name": {"type": "title", "title": {}},
                    "Tags": {"type": "multi_select", "multi_select": {"options": [{"name": "One"}]}},
                    "Status": {"type": "status", "status": {"options": [{"name": "Open"}]}},
                },
            }
        )

        self.assertEqual(issues, [])
        self.assertIsInstance(schema["properties"]["Name"]["title"], dict)
        self.assertIsInstance(schema["properties"]["Tags"]["multi_select"], dict)
        self.assertEqual(schema["properties"]["Tags"]["multi_select"]["options"][0]["name"], "One")

    def test_property_preview_value_handles_malformed_notion_shapes(self):
        self.assertEqual(
            property_preview_value({"type": "title", "title": {"plain_text": "Hello"}}),
            "Hello",
        )
        self.assertEqual(
            property_preview_value({"type": "people", "people": {"name": "Grace"}}),
            "Grace",
        )

    def test_check_workspace_json_health_summarizes_recent_issues(self):
        db = DatabaseManager()
        summary = db.check_workspace_json_health(
            rows=[
                {
                    "notion_id": "page-1",
                    "raw_json": {
                        "properties": {
                            "Name": {"type": "title", "title": {"plain_text": "Broken"}},
                            "Tags": {"type": "multi_select", "multi_select": {"name": "One"}},
                        }
                    },
                },
                {
                    "notion_id": "page-2",
                    "raw_json": {"properties": {"Name": {"type": "title", "title": []}}},
                },
            ],
            log_results=False,
        )

        self.assertEqual(summary["checked_pages"], 2)
        self.assertEqual(summary["pages_with_issues"], 1)
        self.assertGreaterEqual(summary["issue_count"], 2)
        self.assertEqual(summary["sample_pages"], ["page-1"])

    def test_compute_content_hash_is_stable_for_equivalent_payloads(self):
        raw_json_a = {
            "id": "page-1",
            "archived": False,
            "properties": {"Name": {"type": "title"}},
        }
        raw_json_b = {
            "properties": {"Name": {"type": "title"}},
            "archived": False,
            "id": "page-1",
        }

        hash_a = DatabaseManager.compute_content_hash("Title", "Summary", raw_json_a, ["a.png", "b.pdf"])
        hash_b = DatabaseManager.compute_content_hash("Title", "Summary", raw_json_b, ["a.png", "b.pdf"])

        self.assertEqual(hash_a, hash_b)

    def test_page_requires_update_skips_unchanged_records(self):
        existing = {
            "source_updated_at": "2026-04-05T12:00:00.000Z",
            "content_hash": "same-hash",
        }

        self.assertFalse(DatabaseManager.page_requires_update(existing, "2026-04-05T12:00:00.000Z", "same-hash"))
        self.assertFalse(DatabaseManager.page_requires_update(existing, "2026-04-06T12:00:00.000Z", "same-hash"))
        self.assertTrue(DatabaseManager.page_requires_update(existing, "2026-04-05T12:00:00.000Z", "new-hash"))

    def test_page_requires_update_protects_pending_local_edits(self):
        existing = {
            "source_updated_at": "2026-04-05T12:00:00.000Z",
            "content_hash": "local-edit-hash",
            "needs_push": True,
        }

        self.assertFalse(DatabaseManager.page_requires_update(existing, "2026-04-05T12:00:00.000Z", "remote-hash"))

    def test_page_requires_update_treats_equivalent_timestamp_formats_as_unchanged(self):
        existing = {
            "source_updated_at": datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc),
            "content_hash": "same-hash",
        }

        self.assertFalse(DatabaseManager.page_requires_update(existing, "2026-04-05T12:00:00.000Z", "same-hash"))

    def test_compute_content_hash_ignores_volatile_notion_urls(self):
        raw_json_a = {
            "id": "32fb5266-4d9d-814d-b066-d42005cf38ac",
            "url": "https://www.notion.so/workspace/Page-32fb52664d9d814db066d42005cf38ac",
            "properties": {
                "Link": {"type": "url", "url": "https://www.notion.so/workspace/Related-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                "Attachment": {"type": "files", "files": [{"type": "file", "file": {"url": "https://cdn.example.com/a", "expiry_time": "soon"}}]},
            },
        }
        raw_json_b = {
            "id": "32fb5266-4d9d-814d-b066-d42005cf38ac",
            "url": "https://www.notion.so/workspace/Page-ffffffffffffffffffffffffffffffff",
            "properties": {
                "Link": {"type": "url", "url": "https://www.notion.so/workspace/Related-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                "Attachment": {"type": "files", "files": [{"type": "file", "file": {"url": "https://cdn.example.com/b", "expiry_time": "later"}}]},
            },
        }

        hash_a = DatabaseManager.compute_content_hash("Title", "Summary", raw_json_a, [])
        hash_b = DatabaseManager.compute_content_hash("Title", "Summary", raw_json_b, [])

        self.assertEqual(hash_a, hash_b)

    def test_build_page_link_index_separates_internal_and_user_links(self):
        page = {
            "id": "32fb5266-4d9d-814d-b066-d42005cf38ac",
            "url": "https://www.notion.so/workspace/Main-32fb52664d9d814db066d42005cf38ac",
            "properties": {
                "Related": {
                    "type": "relation",
                    "relation": [
                        {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
                        {"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
                    ],
                },
                "Notes": {
                    "type": "rich_text",
                    "rich_text": [
                        {
                            "type": "mention",
                            "mention": {"type": "page", "page": {"id": "cccccccc-cccc-cccc-cccc-cccccccccccc"}},
                            "plain_text": "Mentioned page",
                        },
                        {
                            "type": "text",
                            "text": {
                                "content": "Open related record",
                                "link": {"url": "https://www.notion.so/workspace/Related-dddddddddddddddddddddddddddddddd"},
                            },
                            "plain_text": "Open related record",
                            "href": "https://www.notion.so/workspace/Related-dddddddddddddddddddddddddddddddd",
                        },
                    ],
                },
            },
        }

        link_index = DatabaseManager.build_page_link_index(page)

        self.assertEqual(link_index["internal_resource_uri"], "bb://page/32fb5266-4d9d-814d-b066-d42005cf38ac")
        self.assertEqual(link_index["user_notion_url"], page["url"])
        self.assertEqual(
            link_index["linked_notion_ids"],
            [
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "dddddddd-dddd-dddd-dddd-dddddddddddd",
            ],
        )
        self.assertTrue(all(item["internal_resource_uri"].startswith("bb://page/") for item in link_index["linked_resources"]))
        self.assertTrue(all("user_notion_url" not in item or isinstance(item["user_notion_url"], str) for item in link_index["linked_resources"]))

    def test_build_local_edit_payload_marks_page_for_push(self):
        existing = {
            "notion_id": "page-1",
            "title": "Old title",
            "ai_summary": "Old summary",
            "raw_json": {
                "properties": {
                    "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Old title"}, "plain_text": "Old title"}]},
                    "Notes": {"type": "rich_text", "rich_text": [{"type": "text", "text": {"content": "Old summary"}, "plain_text": "Old summary"}]},
                }
            },
            "media_local_paths": [],
        }

        payload = DatabaseManager.build_local_edit_payload(existing, "New title", "New summary")

        self.assertEqual(payload["title"], "New title")
        self.assertEqual(payload["ai_summary"], "New summary")
        self.assertTrue(payload["needs_push"])
        self.assertNotEqual(payload["content_hash"], "")
        self.assertEqual(
            payload["raw_json"]["properties"]["Name"]["title"][0]["text"]["content"],
            "New title",
        )

    def test_build_bulk_replace_payload_updates_all_matching_fields(self):
        existing = {
            "notion_id": "page-1",
            "title": "Acme roadmap",
            "ai_summary": "Follow up with acme clients. ACME renewal is next week.",
            "raw_json": {
                "properties": {
                    "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Acme roadmap"}, "plain_text": "Acme roadmap"}]},
                    "Notes": {"type": "rich_text", "rich_text": [{"type": "text", "text": {"content": "Follow up with acme clients. ACME renewal is next week."}, "plain_text": "Follow up with acme clients. ACME renewal is next week."}]},
                }
            },
            "media_local_paths": [],
        }

        payload = DatabaseManager.build_bulk_replace_payload(
            existing,
            "acme",
            "Orbit",
            fields=["title", "ai_summary"],
            case_sensitive=False,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["title"], "Orbit roadmap")
        self.assertIn("Orbit clients", payload["ai_summary"])
        self.assertIn("Orbit renewal", payload["ai_summary"])
        self.assertTrue(payload["needs_push"])
        self.assertEqual(payload["match_count"], 3)
        self.assertEqual(set(payload["changed_fields"]), {"title", "ai_summary"})

    def test_build_bulk_replace_payload_returns_none_when_no_match_exists(self):
        existing = {
            "notion_id": "page-1",
            "title": "Weekly review",
            "ai_summary": "Nothing to change here.",
            "raw_json": {"properties": {}},
            "media_local_paths": [],
        }

        payload = DatabaseManager.build_bulk_replace_payload(
            existing,
            "missing text",
            "replacement",
            fields=["title", "ai_summary"],
        )

        self.assertIsNone(payload)


class ClickUpSyncEngineTests(unittest.TestCase):
    def test_prepare_task_record_prefers_markdown_and_flattens_fields(self):
        engine = ClickUpSyncEngine(db_manager=None)
        task = {
            "id": "task-1",
            "name": "Write launch plan",
            "description": "<p>Fallback <strong>HTML</strong></p>",
            "markdown_description": "# Launch plan\n- confirm scope",
            "status": {"status": "in progress", "type": "custom"},
            "assignees": [{"id": 7, "username": "martin", "email": "m@example.com"}],
            "custom_fields": [
                {"name": "Priority", "value": {"label": "High"}},
                {"name": "Tags", "value": [{"name": "client"}, {"name": "urgent"}]},
            ],
            "url": "https://app.clickup.com/t/123",
            "date_created": "1712000000000",
            "date_updated": "1712003600000",
        }

        record = engine._prepare_task_record(task)

        self.assertEqual(record["task_id"], "task-1")
        self.assertEqual(record["name"], "Write launch plan")
        self.assertEqual(record["status"], "in progress")
        self.assertEqual(record["markdown_description"], "# Launch plan\n- confirm scope")
        self.assertEqual(record["assignees"][0]["username"], "martin")
        self.assertEqual(record["custom_fields"]["Priority"], "High")
        self.assertEqual(record["custom_fields"]["Tags"], ["client", "urgent"])

    def test_discover_accessible_lists_collects_lists_from_spaces_and_folders(self):
        engine = ClickUpSyncEngine.__new__(ClickUpSyncEngine)
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append(path)
            responses = {
                "team": {"teams": [{"id": "team-1", "name": "Main Team"}]},
                "team/team-1/space": {"spaces": [{"id": "space-1", "name": "Operations"}]},
                "space/space-1/folder": {"folders": [{"id": "folder-1", "name": "Client Work"}]},
                "folder/folder-1/list": {"lists": [{"id": "list-2", "name": "Launches"}]},
                "space/space-1/list": {"lists": [{"id": "list-1", "name": "General"}]},
            }
            return responses[path]

        engine._request_json = fake_request

        lists = engine.discover_accessible_lists()

        self.assertEqual([item["id"] for item in lists], ["list-1", "list-2"])
        self.assertEqual(lists[0]["name"], "General")
        self.assertIn("team/team-1/space", calls)
        self.assertIn("folder/folder-1/list", calls)

    def test_fetch_all_tasks_uses_discovered_lists_when_all_is_selected(self):
        engine = ClickUpSyncEngine.__new__(ClickUpSyncEngine)
        engine.sync_all_lists = True
        engine.list_ids = []
        engine.discover_accessible_lists = lambda: [
            {"id": "list-a", "name": "Alpha"},
            {"id": "list-b", "name": "Beta"},
        ]

        seen_paths = []

        def fake_request(method, path, **kwargs):
            seen_paths.append(path)
            if path == "list/list-a/task":
                return {"tasks": [{"id": "task-1"}]}
            if path == "list/list-b/task":
                return {"tasks": [{"id": "task-2"}]}
            return {"tasks": []}

        engine._request_json = fake_request

        tasks = engine.fetch_all_tasks()

        self.assertEqual([item["id"] for item in tasks], ["task-1", "task-2"])
        self.assertEqual(seen_paths, ["list/list-a/task", "list/list-b/task"])

    def test_extract_task_ids_from_webhook_payload(self):
        payload = {
            "event": "taskUpdated",
            "task_id": "abc123",
            "history_items": [
                {"task_id": "abc123"},
                {"parent_id": "parent-9"},
            ],
            "task": {"id": "nested-1"},
        }

        task_ids = ClickUpSyncEngine.extract_task_ids_from_webhook(payload)

        self.assertEqual(task_ids, ["abc123", "parent-9", "nested-1"])


class N8nSyncEngineTests(unittest.TestCase):
    def test_extract_workflows_handles_dict_and_list_payloads(self):
        engine = N8nSyncEngine(db_manager=None)

        dict_payload = {"data": [{"id": "wf-1", "name": "Nightly Backup", "active": True}]}
        list_payload = [{"id": "wf-2", "name": "Lead Router", "active": False}]

        self.assertEqual(len(engine._extract_workflow_items(dict_payload)), 1)
        self.assertEqual(len(engine._extract_workflow_items(list_payload)), 1)
        self.assertEqual(engine._extract_workflow_items(dict_payload)[0]["name"], "Nightly Backup")

    def test_base_url_accepts_site_root_and_appends_api_prefix(self):
        with (
            patch("src.sync.engine.get_env", side_effect=lambda key, default="": "https://n8n.example.com" if key == "N8N_API_URL" else default),
            patch("src.sync.engine.get_secret", return_value="api-key"),
        ):
            engine = N8nSyncEngine(db_manager=None)

        self.assertEqual(engine.base_url, "https://n8n.example.com/api/v1")
        self.assertEqual(engine._build_url("workflows"), "https://n8n.example.com/api/v1/workflows")

    def test_prepare_restore_payload_keeps_logic_and_drops_read_only_fields(self):
        engine = N8nSyncEngine(db_manager=None)

        workflow_json = {
            "id": "wf-42",
            "name": "Restore Me",
            "active": True,
            "nodes": [{"id": "node-1", "name": "Start"}],
            "connections": {"Start": {}},
            "settings": {"executionOrder": "v1"},
            "createdAt": "2026-04-05T12:00:00Z",
            "updatedAt": "2026-04-05T12:30:00Z",
            "versionId": "abc123",
        }

        payload = engine._prepare_restore_payload(workflow_json)

        self.assertEqual(payload["name"], "Restore Me")
        self.assertTrue(payload["active"])
        self.assertEqual(payload["nodes"], workflow_json["nodes"])
        self.assertEqual(payload["connections"], workflow_json["connections"])
        self.assertNotIn("createdAt", payload)
        self.assertNotIn("updatedAt", payload)


class PluginScaffoldTests(unittest.TestCase):
    def test_default_registry_includes_wordpress_module(self):
        class FakeDb:
            def ensure_service_schema(self, *args, **kwargs):
                return True

        registry = build_default_registry(tray_app=None, db_manager=FakeDb())
        plugin_ids = [plugin.plugin_id for plugin in registry.all()]

        self.assertIn("wordpress", plugin_ids)

    def test_n8n_plugin_uses_live_status_and_actions(self):
        class FakeDb:
            def ensure_service_schema(self, *args, **kwargs):
                return True

            def get_n8n_status(self):
                return {
                    "connected": True,
                    "current_workflows": 7,
                    "enabled_workflows": 5,
                    "total_versions": 12,
                    "last_synced_at": None,
                    "last_sync_status": "success",
                    "last_sync_summary": "n8n sync complete.",
                }

        class FakeTray:
            def run_n8n_sync(self):
                return None

            def show_wizard(self):
                return None

            def run_backup_now(self, silent=False):
                return None

        registry = build_default_registry(tray_app=FakeTray(), db_manager=FakeDb())
        n8n_plugin = next(plugin for plugin in registry.all() if plugin.plugin_id == "n8n")

        status = n8n_plugin.get_status()
        action_labels = [action.label for action in n8n_plugin.get_quick_actions()]

        self.assertEqual(status.state, "Connected")
        self.assertIn("7", " ".join(status.details))
        self.assertNotIn("scaffold", status.summary.lower())
        self.assertIn("Sync n8n now", action_labels)


class SetupWizardValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def _build_wizard(self):
        fake_db = SimpleNamespace(connect=lambda: True)
        wizard = SetupWizard(fake_db, lambda: None)
        self.addCleanup(wizard.close)
        return wizard

    def test_save_mautic_reports_saved_and_validated(self):
        wizard = self._build_wizard()
        wizard.mautic_base_url.setText("https://mautic.example.com")
        wizard.mautic_username.setText("martinadmin")
        wizard.mautic_password.setText("secret")
        wizard._validate_mautic_settings = lambda *args, **kwargs: (True, "Mautic accepted the sign-in details.")

        with (
            patch("src.ui.wizard.save_env_var"),
            patch("src.ui.wizard.save_secret"),
            patch("src.ui.wizard.QMessageBox.information") as info_box,
        ):
            wizard._save_mautic()

        self.assertTrue(info_box.called)
        message = info_box.call_args.args[2]
        self.assertIn("saved", message.lower())
        self.assertIn("valid", message.lower())

    def test_save_mautic_accepts_client_id_and_secret(self):
        wizard = self._build_wizard()
        wizard.mautic_base_url.setText("https://mautic.example.com")
        wizard.mautic_client_id.setText("client-id")
        wizard.mautic_client_secret.setText("client-secret")
        wizard._validate_mautic_settings = lambda *args, **kwargs: (True, "Mautic accepted the client ID and secret.")

        with (
            patch("src.ui.wizard.save_env_var") as save_env_var,
            patch("src.ui.wizard.save_secret") as save_secret,
            patch("src.ui.wizard.QMessageBox.information") as info_box,
        ):
            wizard._save_mautic()

        saved_values = {call.args[0]: call.args[1] for call in save_env_var.call_args_list}
        self.assertEqual(saved_values["MAUTIC_CLIENT_ID"], "client-id")
        save_secret.assert_any_call("MAUTIC_CLIENT_SECRET", "client-secret")
        self.assertTrue(info_box.called)
        message = info_box.call_args.args[2]
        self.assertIn("client id and secret", message.lower())

    def test_validate_mautic_supports_client_credentials_flow(self):
        wizard = self._build_wizard()

        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}

            def json(self):
                return self._payload

        fake_session = SimpleNamespace(
            post=lambda *args, **kwargs: FakeResponse(200, {"access_token": "oauth-token"}),
            get=lambda *args, **kwargs: FakeResponse(200, {"contacts": []}),
        )

        with patch("src.ui.wizard.build_retry_session", return_value=fake_session):
            is_valid, detail = wizard._validate_mautic_settings(
                base_url="https://mautic.example.com",
                access_token="",
                client_id="client-id",
                client_secret="client-secret",
                username="",
                password="",
                verify_ssl=True,
            )

        self.assertTrue(is_valid)
        self.assertIn("client id and secret", detail.lower())

    def test_save_n8n_reports_saved_and_validated(self):
        wizard = self._build_wizard()
        wizard.n8n_api_url.setText("https://n8n.example.com")
        wizard.n8n_api_key.setText("api-key")
        wizard._validate_n8n_settings = lambda *args, **kwargs: (True, "n8n API responded correctly.")

        with (
            patch("src.ui.wizard.save_env_var") as save_env,
            patch("src.ui.wizard.save_secret"),
            patch("src.ui.wizard.QMessageBox.information") as info_box,
        ):
            wizard._save_n8n()

        save_env.assert_any_call("N8N_API_URL", "https://n8n.example.com/api/v1")
        self.assertTrue(info_box.called)
        message = info_box.call_args.args[2]
        self.assertIn("saved", message.lower())
        self.assertIn("valid", message.lower())

    def test_setup_picker_uses_ready_and_not_set_up_labels(self):
        wizard = self._build_wizard()
        wizard._is_configured = lambda key: key == "mautic"

        picker_page = wizard._build_picker_page()
        picker = picker_page.widget()
        label_texts = [label.text() for label in picker.findChildren(QLabel)]

        self.assertIn("Ready", label_texts)
        self.assertIn("Not set up", label_texts)
        self.assertNotIn("✓ Connected", label_texts)

    def test_save_database_uses_backup_hours_and_drops_sync_setting(self):
        wizard = self._build_wizard()

        self.assertFalse(hasattr(wizard, "sync_interval"))
        self.assertTrue(hasattr(wizard, "backup_interval_hours"))

        wizard.pg_host.setText("localhost")
        wizard.pg_port.setText("5432")
        wizard.pg_dbname.setText("notion_mirror")
        wizard.pg_user.setText("postgres")
        wizard.proxy_port.setText("8080")
        wizard.backup_interval_hours.setText("6")
        wizard.maintenance_time.setText("02:30")

        with (
            patch("src.ui.wizard.save_env_var") as save_env_var,
            patch("src.ui.wizard.save_secret"),
            patch("src.ui.wizard.QMessageBox.information"),
            patch("src.ui.wizard.QMessageBox.critical"),
        ):
            wizard._save_database()

        saved_values = {call.args[0]: call.args[1] for call in save_env_var.call_args_list}
        self.assertEqual(saved_values["BACKUP_INTERVAL_MINUTES"], "360")
        self.assertNotIn("SYNC_INTERVAL_MINUTES", saved_values)


class DashboardOverviewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_status_badge_colors_and_states_are_distinct(self):
        self.assertEqual(_badge_style_for_state("Ready"), "statusBadgeGreen")
        self.assertEqual(_badge_style_for_state("Connected"), "statusBadgeOrange")
        self.assertEqual(_badge_style_for_state("Not set up"), "statusBadgeRed")

        with patch.dict(os.environ, {"N8N_API_URL": "https://n8n.example.com", "N8N_API_KEY": "test-key"}, clear=False):
            plugin = N8nPlugin(db_manager=SimpleNamespace(get_n8n_status=lambda: {}))
            self.assertEqual(plugin.get_status().state, "Connected")

            ready_plugin = N8nPlugin(
                db_manager=SimpleNamespace(
                    get_n8n_status=lambda: {
                        "connected": True,
                        "current_workflows": 2,
                        "enabled_workflows": 1,
                        "total_versions": 3,
                    }
                )
            )
            self.assertEqual(ready_plugin.get_status().state, "Ready")

    def test_notion_plugin_status_includes_last_sync_meta(self):
        plugin = build_default_registry(
            tray_app=SimpleNamespace(
                get_runtime_stats=lambda: {
                    "connected": True,
                    "active_pages": 12,
                    "pending_push": 2,
                    "last_pull_at": datetime(2026, 4, 8, 9, 15, tzinfo=timezone.utc),
                    "last_push_at": datetime(2026, 4, 8, 9, 45, tzinfo=timezone.utc),
                },
                maintenance_manager=SimpleNamespace(get_task_readiness=lambda task_name: {"summary": "Ready"}),
            ),
            db_manager=SimpleNamespace(),
        ).all()[0]

        status = plugin.get_status()

        self.assertEqual(getattr(status, "meta", ""), "Last synced: 2026-04-08 09:45:00")

    def test_service_summary_card_displays_last_sync_meta(self):
        class FakePlugin:
            plugin_id = "notion"
            nav_label = "Notion"
            display_name = "Notion"
            schema_name = "notion"
            accent_color = "#2563eb"

            def get_status(self):
                return ServiceStatus(
                    state="Ready",
                    summary="Notion ready",
                    details=["Target databases: Main workspace"],
                    meta="Last synced: 2026-04-08 09:45:00",
                )

            def get_quick_actions(self):
                return []

        card = ServiceSummaryCard(FakePlugin())
        card.refresh()

        self.assertEqual(card.last_activity_label.text(), "Last synced: 2026-04-08 09:45:00")
        self.assertFalse(card.last_activity_label.isHidden())

    def test_build_compact_activity_text_keeps_details_short(self):
        maintenance_state = {
            MaintenanceManager.TASK_LOCAL: {"last_status": "success"},
            MaintenanceManager.TASK_NOTION: {"last_status": "never"},
            MaintenanceManager.TASK_CLICKUP: {"last_status": "success"},
            MaintenanceManager.TASK_WORDPRESS: {"last_status": "success"},
            MaintenanceManager.TASK_INTERNAL: {"last_status": "success"},
        }

        headline, meta = build_compact_activity_text(
            "Smart sync finished successfully and checked 14 page updates.",
            "Last sync: Notion sync @ 2026-04-07 09:30:00 (5 minutes ago)",
            maintenance_state,
        )

        self.assertIn("Smart sync finished successfully", headline)
        self.assertIn("5 minutes ago", meta)
        self.assertIn("Clean-up:", meta)
        self.assertNotIn("2026-04-07 09:30:00", meta)

    def test_overview_page_hides_both_scrollbars(self):
        fake_tray = SimpleNamespace(
            last_summary="Ready",
            maintenance_manager=SimpleNamespace(get_status_snapshot=lambda: {}),
            get_last_sync_status_text=lambda force_refresh=False: "Last sync: Never",
        )
        fake_main_window = SimpleNamespace(tray_app=fake_tray, registry=PluginRegistry(), _pages_loaded=False)

        page = OverviewPage(fake_main_window)

        self.assertEqual(page.scroll_area.verticalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
        self.assertEqual(page.scroll_area.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)

    def test_overview_service_cards_stay_in_two_columns(self):
        class FakePlugin:
            def __init__(self, plugin_id: str, nav_label: str):
                self.plugin_id = plugin_id
                self.nav_label = nav_label
                self.display_name = nav_label
                self.schema_name = plugin_id
                self.accent_color = "#2563eb"

            def get_status(self):
                return ServiceStatus(state="Ready", summary=f"{self.display_name} ready", details=[])

            def get_quick_actions(self):
                return []

        registry = PluginRegistry()
        registry.register(FakePlugin("notion", "Notion"))
        registry.register(FakePlugin("clickup", "ClickUp"))
        registry.register(FakePlugin("wordpress", "WordPress"))

        fake_tray = SimpleNamespace(
            last_summary="Ready",
            maintenance_manager=SimpleNamespace(get_status_snapshot=lambda: {}),
            get_last_sync_status_text=lambda force_refresh=False: "Last sync: Never",
        )
        fake_main_window = SimpleNamespace(tray_app=fake_tray, registry=registry, _pages_loaded=True)

        page = OverviewPage(fake_main_window)
        page.build_service_cards()

        first = page._cards_grid.itemAtPosition(0, 0)
        second = page._cards_grid.itemAtPosition(0, 1)
        third = page._cards_grid.itemAtPosition(1, 0)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(third)
        self.assertEqual(first.widget().plugin.plugin_id, "notion")
        self.assertEqual(second.widget().plugin.plugin_id, "clickup")
        self.assertEqual(third.widget().plugin.plugin_id, "wordpress")
        self.assertIsNone(page._cards_grid.itemAtPosition(0, 2))

    def test_resync_button_uses_tray_connection_resync(self):
        calls = []
        page = NotionModulePage.__new__(NotionModulePage)
        page.main_window = SimpleNamespace(
            tray_app=SimpleNamespace(run_connection_resync=lambda service="all": calls.append(service))
        )

        with patch("src.ui.main_window.QMessageBox.information") as info_box:
            NotionModulePage.resync_notion_databases(page)

        self.assertEqual(calls, ["notion"])
        info_box.assert_not_called()

    def test_main_window_settings_button_opens_the_settings_page(self):
        class FakePlugin:
            def __init__(self, plugin_id: str, nav_label: str):
                self.plugin_id = plugin_id
                self.nav_label = nav_label
                self.display_name = nav_label
                self.schema_name = plugin_id

            def get_status(self):
                return ServiceStatus(state="Ready", summary=f"{self.display_name} ready", details=[])

            def get_quick_actions(self):
                return []

        registry = PluginRegistry()
        registry.register(FakePlugin("clickup", "ClickUp"))
        registry.register(FakePlugin("wordpress", "WordPress"))

        fake_db = SimpleNamespace(conn=object())
        fake_tray = SimpleNamespace(db=fake_db)

        with patch("src.ui.main_window.build_default_registry", return_value=registry):
            window = MainWindow(fake_tray)
            self.addCleanup(window.close)
            window.refresh_view()

            window._navigate_to_settings()

            self.assertIs(window.stack.currentWidget(), window.settings_page)

    def test_main_window_bulk_replace_stays_inside_main_window(self):
        fake_db = SimpleNamespace(conn=object())
        fake_tray = SimpleNamespace(db=fake_db)

        with patch("src.ui.main_window.build_default_registry", return_value=PluginRegistry()):
            window = MainWindow(fake_tray)
            self.addCleanup(window.close)
            window.refresh_view()

            with patch("src.ui.dashboard.BulkReplaceDialog") as dialog_cls:
                window.open_bulk_replace()

            self.assertTrue(hasattr(window, "bulk_replace_page"))
            self.assertIs(window.stack.currentWidget(), window.bulk_replace_page)
            dialog_cls.assert_not_called()


class DatabaseBrowserDisplayTests(unittest.TestCase):
    def test_format_entry_label_shows_status_hints(self):
        entry = {
            "title": "Project Plan",
            "notion_id": "page-123",
            "needs_push": True,
            "has_media": True,
            "linked_page_count": 3,
        }

        label = DatabaseBrowserWindow.format_entry_label(entry)

        self.assertIn("Project Plan", label)
        self.assertIn("waiting to push", label.lower())
        self.assertIn("media", label.lower())
        self.assertIn("3 link", label.lower())

    def test_build_page_details_html_includes_summary_properties_and_links(self):
        entry = {
            "title": "Project Plan",
            "notion_id": "page-123",
            "updated_at": datetime(2026, 4, 5, 15, 30, tzinfo=timezone.utc),
            "needs_push": True,
        }
        context = {
            "title": "Project Plan",
            "ai_summary": "Plan for the next release.",
            "property_preview": {"Status": "Active", "Owner": "Micah"},
            "media_count": 2,
            "has_media": True,
            "internal_links": [
                {
                    "title": "Related spec",
                    "internal_resource_uri": "bb://page/abc",
                    "user_notion_url": "https://notion.so/related-spec",
                }
            ],
            "user_notion_url": "https://notion.so/project-plan",
        }

        html = DatabaseBrowserWindow.build_page_details_html(entry, context)

        self.assertIn("Project Plan", html)
        self.assertIn("Plan for the next release.", html)
        self.assertIn("Status", html)
        self.assertIn("Related spec", html)
        self.assertIn("Open in Notion", html)


class TimeMachineRecoveryTests(unittest.TestCase):
    def test_build_time_machine_diff_lists_changed_fields(self):
        snapshot = {
            "notion_id": "page-1",
            "title": "Recovered title",
            "ai_summary": "Recovered summary",
            "media_local_paths": ["old.pdf"],
            "raw_json": {
                "properties": {
                    "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Recovered title"}, "plain_text": "Recovered title"}]},
                    "Status": {"type": "status", "status": {"name": "Active"}},
                }
            },
        }
        current = {
            "notion_id": "page-1",
            "title": "Current title",
            "ai_summary": "Current summary",
            "media_local_paths": ["new.pdf"],
            "raw_json": {
                "properties": {
                    "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Current title"}, "plain_text": "Current title"}]},
                    "Status": {"type": "status", "status": {"name": "Inactive"}},
                }
            },
        }

        changes = DatabaseManager.build_time_machine_diff(snapshot, current)
        changed_keys = {item["field_key"] for item in changes}

        self.assertIn("title", changed_keys)
        self.assertIn("property::Status", changed_keys)
        self.assertTrue(any(item["push_supported"] for item in changes if item["field_key"] == "property::Status"))

    def test_build_restore_payload_only_changes_selected_fields(self):
        snapshot = {
            "notion_id": "page-1",
            "title": "Recovered title",
            "ai_summary": "Recovered summary",
            "media_local_paths": ["restored.pdf"],
            "source_updated_at": "2026-04-05T10:00:00.000Z",
            "raw_json": {
                "properties": {
                    "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Recovered title"}, "plain_text": "Recovered title"}]},
                    "Status": {"type": "status", "status": {"name": "Active"}},
                    "Notes": {"type": "rich_text", "rich_text": [{"type": "text", "text": {"content": "Recovered summary"}, "plain_text": "Recovered summary"}]},
                }
            },
        }
        current = {
            "notion_id": "page-1",
            "title": "Current title",
            "ai_summary": "Current summary",
            "media_local_paths": ["current.pdf"],
            "source_updated_at": "2026-04-06T10:00:00.000Z",
            "raw_json": {
                "properties": {
                    "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Current title"}, "plain_text": "Current title"}]},
                    "Status": {"type": "status", "status": {"name": "Inactive"}},
                    "Notes": {"type": "rich_text", "rich_text": [{"type": "text", "text": {"content": "Current summary"}, "plain_text": "Current summary"}]},
                }
            },
        }

        payload = DatabaseManager.build_restore_payload(current, snapshot, ["title", "property::Status"], queue_for_push=True)

        self.assertEqual(payload["title"], "Recovered title")
        self.assertEqual(payload["ai_summary"], "Current summary")
        self.assertEqual(payload["raw_json"]["properties"]["Status"]["status"]["name"], "Active")
        self.assertEqual(
            payload["raw_json"]["properties"]["Notes"]["rich_text"][0]["text"]["content"],
            "Current summary",
        )
        self.assertTrue(payload["needs_push"])


class TimeMachineUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_load_snapshot_handles_query_errors_gracefully(self):
        class FakeDb:
            def __init__(self):
                self.conn = True

            def connect(self):
                return True

            def get_time_machine_comparison(self, snapshot_at):
                raise RuntimeError("comparison failed")

        tray = SimpleNamespace(db=FakeDb())
        window = TimeMachineWindow(tray, embedded=True)
        self.addCleanup(window.close)

        with patch("src.ui.time_machine.QMessageBox.warning") as warning_box:
            window.load_snapshot()

        self.assertTrue(warning_box.called)
        self.assertIn("could not load", warning_box.call_args.args[2].lower())


class SyncEngineHelperTests(unittest.TestCase):
    def test_page_last_edited_time_reads_notion_timestamp(self):
        engine = SyncEngine.__new__(SyncEngine)
        page = {"last_edited_time": "2026-04-05T09:30:00.000Z"}

        self.assertEqual(engine._page_last_edited_time(page), "2026-04-05T09:30:00.000Z")

    def test_pull_from_notion_short_circuits_when_workspace_has_no_new_changes(self):
        class FakeDb:
            def get_stats(self, force_refresh=False, max_age_seconds=0):
                return {"latest_source_update": "2026-04-06T12:00:00.000Z"}

        class FakeNotion:
            def search(self, **kwargs):
                return {
                    "results": [
                        {
                            "id": "latest-page",
                            "object": "page",
                            "last_edited_time": "2026-04-06T12:00:00.000Z",
                        }
                    ],
                    "has_more": False,
                }

        engine = SyncEngine.__new__(SyncEngine)
        engine.db = FakeDb()
        engine.notion = FakeNotion()
        engine.sync_all_databases = True
        engine.db_ids = []
        engine.last_summary = "Ready"
        engine.last_run_stats = {
            "pull": {"seen": 0, "changed": 0, "skipped": 0, "failed_databases": 0},
            "push": {"attempted": 0, "updated": 0, "failed": 0, "skipped": 0},
        }
        engine._target_db_ids = lambda: (_ for _ in ()).throw(AssertionError("full database discovery should be skipped"))

        success = engine.pull_from_notion()

        self.assertTrue(success)
        self.assertIn("no remote changes", engine.last_summary.lower())
        self.assertEqual(engine.last_run_stats["pull"]["changed"], 0)

    def test_extract_text_and_media_includes_searchable_property_values(self):
        engine = SyncEngine.__new__(SyncEngine)
        page = {
            "id": "page-1",
            "properties": {
                "Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Marketing Plan"}, "plain_text": "Marketing Plan"}]},
                "Status": {"type": "status", "status": {"name": "Active"}},
                "Team": {"type": "select", "select": {"name": "Growth"}},
                "Tags": {"type": "multi_select", "multi_select": [{"name": "Launch"}, {"name": "Q2"}]},
                "Notes": {"type": "rich_text", "rich_text": [{"type": "text", "text": {"content": "Campaign brief"}, "plain_text": "Campaign brief"}]},
            },
        }

        title, summary, media_paths = engine.extract_text_and_media(page)

        self.assertEqual(title, "Marketing Plan")
        self.assertEqual(media_paths, [])
        self.assertIn("Status: Active", summary)
        self.assertIn("Team: Growth", summary)
        self.assertIn("Tags: Launch, Q2", summary)
        self.assertIn("Notes: Campaign brief", summary)

    def test_push_to_notion_skips_pages_with_remote_conflicts(self):
        class FakePages:
            def __init__(self):
                self.updated = []

            def retrieve(self, page_id):
                return {"id": page_id, "last_edited_time": "2026-04-06T12:00:00.000Z"}

            def update(self, **kwargs):
                self.updated.append(kwargs)

        class FakeNotion:
            def __init__(self):
                self.pages = FakePages()

        class FakeDb:
            def __init__(self):
                self.marked = []

            def get_active_pages(self, only_needs_push=False):
                return [
                    {
                        "notion_id": "page-1",
                        "title": "Local title",
                        "ai_summary": "Local summary",
                        "raw_json": {"properties": {"Name": {"type": "title"}}},
                        "needs_push": True,
                        "source_updated_at": "2026-04-05T12:00:00.000Z",
                    }
                ]

            def _normalize_timestamp(self, value):
                return DatabaseManager._normalize_timestamp(value)

            def mark_pages_pushed(self, notion_ids):
                self.marked.extend(notion_ids)
                return True

        engine = SyncEngine.__new__(SyncEngine)
        engine.db = FakeDb()
        engine.notion = FakeNotion()
        engine.last_summary = "Ready"
        engine.last_run_stats = {
            "pull": {"seen": 0, "changed": 0, "skipped": 0, "failed_databases": 0},
            "push": {"attempted": 0, "updated": 0, "failed": 0, "skipped": 0},
        }
        engine.conflicts = []

        success = engine.push_to_notion()

        self.assertTrue(success)
        self.assertEqual(engine.notion.pages.updated, [])
        self.assertEqual(engine.last_run_stats["push"]["conflicts"], 1)
        self.assertIn("conflict", engine.last_summary.lower())

    def test_prepare_page_for_storage_adds_business_brain_link_metadata(self):
        engine = SyncEngine.__new__(SyncEngine)
        engine.db = DatabaseManager()
        page = {
            "id": "32fb5266-4d9d-814d-b066-d42005cf38ac",
            "url": "https://www.notion.so/workspace/Main-32fb52664d9d814db066d42005cf38ac",
            "properties": {
                "Related": {
                    "type": "relation",
                    "relation": [{"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}],
                }
            },
        }

        prepared = engine._prepare_page_for_storage(page)

        self.assertIn("_business_brain_links", prepared)
        self.assertEqual(
            prepared["_business_brain_links"]["internal_resource_uri"],
            "bb://page/32fb5266-4d9d-814d-b066-d42005cf38ac",
        )
        self.assertEqual(
            prepared["_business_brain_links"]["user_notion_url"],
            page["url"],
        )
        self.assertEqual(
            prepared["_business_brain_links"]["linked_notion_ids"],
            ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        )

    def test_discover_accessible_databases_includes_linked_legacy_database_ids(self):
        class FakeDatabases:
            def retrieve(self, database_id):
                if database_id != "legacy-db":
                    raise RuntimeError("unexpected database id")
                return {
                    "id": "legacy-db",
                    "title": [{"plain_text": "Business Plan"}],
                    "url": "https://www.notion.so/legacy-db",
                    "parent": {"type": "page_id"},
                }

        class FakeNotion:
            def __init__(self):
                self.databases = FakeDatabases()

            def search(self, **kwargs):
                return {
                    "results": [
                        {
                            "id": "db-visible",
                            "title": [{"plain_text": "Tasks"}],
                            "url": "https://www.notion.so/db-visible",
                            "parent": {"type": "workspace"},
                        }
                    ],
                    "has_more": False,
                }

        engine = SyncEngine.__new__(SyncEngine)
        engine.notion = FakeNotion()
        engine.scan_for_unshared_database_references = lambda databases=None: [
            {
                "id": "legacy-db",
                "source_id": "db-visible",
                "source_title": "Tasks",
                "property_name": "SOPs",
                "property_type": "relation",
            }
        ]

        databases = engine.discover_accessible_databases()

        self.assertEqual([item["id"] for item in databases], ["legacy-db", "db-visible"])
        self.assertEqual(databases[0]["title"], "Business Plan")

    def test_query_database_pages_resolves_legacy_database_ids_via_data_sources(self):
        class FakeDataSources:
            def __init__(self):
                self.calls = []

            def query(self, data_source_id, **kwargs):
                self.calls.append(data_source_id)
                if data_source_id == "legacy-db":
                    raise RuntimeError("Use the child data source id instead.")
                return {"results": [{"id": f"page-for-{data_source_id}"}], "has_more": False}

        class FakeDatabases:
            def retrieve(self, database_id):
                if database_id != "legacy-db":
                    raise RuntimeError("unexpected database id")
                return {
                    "object": "database",
                    "id": "legacy-db",
                    "title": [{"plain_text": "Legacy SOPs"}],
                    "data_sources": [{"id": "resolved-ds"}],
                }

        class FakeNotion:
            def __init__(self):
                self.data_sources = FakeDataSources()
                self.databases = FakeDatabases()

        engine = SyncEngine.__new__(SyncEngine)
        engine.notion = FakeNotion()

        results = engine._query_database_pages("legacy-db")

        self.assertEqual([item["id"] for item in results], ["page-for-resolved-ds"])
        self.assertEqual(engine.notion.data_sources.calls, ["resolved-ds"])

    def test_pull_from_notion_adds_database_schema_snapshot_to_catalog(self):
        class FakeDb:
            def __init__(self):
                self.saved_payloads = []

            def get_existing_page_map(self, notion_ids):
                return {}

            def compute_content_hash(self, title, ai_summary, raw_json, media_paths):
                return f"hash:{title}:{raw_json.get('object', 'page')}"

            def page_requires_update(self, existing_record, source_updated_at, content_hash):
                return True

            def upsert_pages(self, payloads):
                self.saved_payloads.extend(payloads)
                return len(payloads), 0

        engine = SyncEngine.__new__(SyncEngine)
        engine.db = FakeDb()
        engine.notion = object()
        engine.last_summary = "Ready"
        engine.last_run_stats = {
            "pull": {"seen": 0, "changed": 0, "skipped": 0, "failed_databases": 0},
            "push": {"attempted": 0, "updated": 0, "failed": 0, "skipped": 0},
        }
        engine._target_db_ids = lambda: ["db-1"]
        engine._query_database_pages = lambda db_id, schema=None: [
            {
                "id": "page-1",
                "url": "https://www.notion.so/page-1",
                "last_edited_time": "2026-04-06T12:06:00.000Z",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "SOP - Intake", "text": {"content": "SOP - Intake"}}],
                    }
                },
            }
        ]
        engine._retrieve_database_schema = lambda db_id: {
            "object": "data_source",
            "id": db_id,
            "url": f"https://www.notion.so/{db_id}",
            "last_edited_time": "2026-04-06T12:06:00.000Z",
            "title": [{"plain_text": "Operations - SOPs"}],
            "properties": {
                "Status": {"type": "status", "status": {"options": [{"name": "Draft"}, {"name": "Live"}]}},
                "Owner": {"type": "people"},
            },
        }
        engine.extract_text_and_media = lambda page, source_updated_at="": ("SOP - Intake", "Summary line", [])
        engine._prepare_page_for_storage = SyncEngine._prepare_page_for_storage.__get__(engine, SyncEngine)

        success = engine.pull_from_notion()

        self.assertTrue(success)
        schema_payload = next(
            (payload for payload in engine.db.saved_payloads if payload.get("raw_json", {}).get("object") == "data_source"),
            None,
        )
        self.assertIsNotNone(schema_payload)
        self.assertEqual(schema_payload["notion_id"], "db-1")
        self.assertIn("Database schema", schema_payload["title"])
        self.assertIn("Status", schema_payload["ai_summary"])
        self.assertEqual(
            schema_payload["raw_json"]["_business_brain_links"]["internal_resource_uri"],
            "bb://database/db-1",
        )


class BackupManagerTests(unittest.TestCase):
    def test_describe_interval_returns_friendly_labels(self):
        self.assertEqual(BackupManager.describe_interval(0), "Manual only")
        self.assertEqual(BackupManager.describe_interval(60), "Every 1 hour(s)")
        self.assertEqual(BackupManager.describe_interval(1440), "Every 1 day(s)")

    def test_invalid_backup_provider_falls_back_to_s3(self):
        manager = BackupManager(db_manager=None)

        with patch.dict(os.environ, {"BACKUP_PROVIDER": "google_drive"}, clear=False):
            self.assertEqual(manager.get_configured_provider(), "s3")

    def test_apply_retention_keeps_newest_backup_files(self):
        with TemporaryDirectory() as temp_dir:
            manager = BackupManager(db_manager=None, backup_dir=Path(temp_dir))
            entries = []
            for index in range(3):
                file_path = Path(temp_dir) / f"backup-{index}.json.gz"
                file_path.write_text("demo", encoding="utf-8")
                entries.append({"completed_at": f"2026-04-0{index + 1}T00:00:00+00:00", "local_path": str(file_path)})

            original_value = os.environ.get("BACKUP_RETENTION_COUNT")
            os.environ["BACKUP_RETENTION_COUNT"] = "2"
            try:
                kept, removed = manager.apply_retention(entries)
            finally:
                if original_value is None:
                    os.environ.pop("BACKUP_RETENTION_COUNT", None)
                else:
                    os.environ["BACKUP_RETENTION_COUNT"] = original_value

            self.assertEqual(len(kept), 2)
            self.assertEqual(len(removed), 1)
            self.assertFalse(Path(entries[0]["local_path"]).exists())
            self.assertTrue(Path(entries[-1]["local_path"]).exists())


class TrayAppUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_tray_app_exposes_dashboard_support_actions(self):
        self.assertTrue(hasattr(TrayApp, "run_backup_now"))
        self.assertTrue(hasattr(TrayApp, "run_health_check"))
        self.assertTrue(hasattr(TrayApp, "preview_bulk_replace"))
        self.assertTrue(hasattr(TrayApp, "apply_bulk_replace"))
        self.assertTrue(hasattr(TrayApp, "run_connection_resync"))

    def test_tray_app_prefers_cached_stats_while_background_work_is_running(self):
        class FakeWorker:
            def isRunning(self):
                return True

        class FakeDb:
            def __init__(self):
                self.live_calls = 0

            def get_cached_stats_snapshot(self):
                return {"connected": True, "active_pages": 9, "pending_push": 1}

            def get_stats(self, *args, **kwargs):
                self.live_calls += 1
                return {"connected": True, "active_pages": 99, "pending_push": 10}

        tray = type(
            "TrayStub",
            (),
            {
                "db": FakeDb(),
                "worker": FakeWorker(),
                "n8n_worker": None,
                "clickup_worker": None,
                "maintenance_worker": None,
            },
        )()

        stats = TrayApp.get_runtime_stats(tray)

        self.assertEqual(stats["active_pages"], 9)
        self.assertEqual(tray.db.live_calls, 0)

    def test_tray_app_exposes_time_machine_actions(self):
        self.assertTrue(hasattr(TrayApp, "get_activity_log"))
        self.assertTrue(hasattr(TrayApp, "set_activity_checked"))
        self.assertTrue(hasattr(TrayApp, "restore_from_time_machine"))

    def test_tray_menu_stays_minimal_with_open_and_exit_only(self):
        fake_db = SimpleNamespace(conn=None, connect=lambda: False)
        tray = TrayApp(QIcon(), fake_db, lambda *args, **kwargs: None)
        self.addCleanup(tray.hide)

        action_text = [action.text() for action in tray.menu.actions() if action.text()]
        submenu_titles = [action.menu().title() for action in tray.menu.actions() if action.menu()]

        self.assertEqual(action_text, ["Open Dashboard", "Exit"])
        self.assertEqual(submenu_titles, [])

    def test_on_db_ready_starts_schedule_and_webhook_listener(self):
        calls = []

        class TrayStub:
            dashboard = None

            def refresh_schedule(self):
                calls.append("refresh_schedule")

            def ensure_clickup_webhook_listener(self, show_message=False):
                calls.append(("ensure_clickup_webhook_listener", bool(show_message)))
                return True, "ok"

            def set_status_light(self, state, detail=None):
                calls.append(("set_status_light", state, detail))

        tray = TrayStub()
        TrayApp.on_db_ready(tray, True, "")

        self.assertEqual(calls[0], "refresh_schedule")
        self.assertEqual(calls[1], ("ensure_clickup_webhook_listener", False))
        self.assertEqual(calls[2], ("set_status_light", "ready", "Database connected"))

    def test_last_sync_status_text_includes_relative_age(self):
        status = TrayApp.build_last_sync_status_text(
            {
                "connected": True,
                "last_pull_at": "2026-04-06 12:00:00",
                "last_push_at": "2026-04-06 12:07:00",
            },
            now=datetime(2026, 4, 6, 12, 10, 0),
        )

        self.assertIn("Last sync:", status)
        self.assertIn("2026-04-06 12:07:00", status)
        self.assertIn("3 minutes ago", status)

    def test_last_sync_status_text_defaults_to_never_without_timestamps(self):
        self.assertEqual(
            TrayApp.build_last_sync_status_text({"connected": True}, now=datetime(2026, 4, 6, 12, 10, 0)),
            "Last sync: Never",
        )

    def test_pull_confirmation_explains_one_way_import(self):
        title, message = TrayApp.build_confirmation_details("pull", pending_push=2)

        self.assertIn("one-way", message.lower())
        self.assertIn("notion", message.lower())
        self.assertIn("local mirror", message.lower())
        self.assertIn("2 local edit", message.lower())
        self.assertIn("Import from Notion only", title)

    def test_push_confirmation_mentions_pending_change_count(self):
        title, message = TrayApp.build_confirmation_details("push", pending_push=3)

        self.assertIn("one-way", message.lower())
        self.assertIn("3 queued local change", message.lower())
        self.assertIn("Push local changes only", title)

    def test_notion_access_report_lists_connected_and_missing_databases(self):
        report = TrayApp.build_notion_access_report(
            [{"id": "db-visible", "title": "Projects"}, {"id": "db-notes", "title": "Notes"}],
            "db-visible,db-missing",
        )

        self.assertIn("Projects", report["details_html"])
        self.assertIn("db-missing", report["details_html"])
        self.assertIn("connected", report["summary"].lower())
        self.assertTrue(report["has_warning"])


class WorkerIsolationTests(unittest.TestCase):
    def test_sync_worker_uses_dedicated_database_connection(self):
        class FakeDb:
            def __init__(self):
                self.last_error = ""
                self.closed = False

            def connect(self):
                return True

            def close(self):
                self.closed = True

        fake_db = FakeDb()

        class FakeEngine:
            def __init__(self, db_manager):
                self.db_manager = db_manager
                self.last_summary = "worker ok"

            def perform_sync(self):
                return True

        results = []
        with patch("src.ui.tray.DatabaseManager", return_value=fake_db), patch("src.ui.tray.SyncEngine", FakeEngine):
            worker = __import__("src.ui.tray", fromlist=["SyncWorker"]).SyncWorker("sync")
            worker.finished.connect(lambda mode, success, summary: results.append((mode, success, summary)))
            worker.run()

        self.assertEqual(results, [("sync", True, "worker ok")])
        self.assertTrue(fake_db.closed)

    def test_connection_resync_worker_can_refresh_notion_only(self):
        class FakeDb:
            def __init__(self):
                self.last_error = ""
                self.closed = False

            def connect(self):
                return True

            def close(self):
                self.closed = True

        fake_db = FakeDb()

        class FakeSyncEngine:
            def __init__(self, db_manager):
                self.db_manager = db_manager
                self.notion = object()
                self.last_summary = "Notion refresh complete"

            def discover_accessible_databases(self):
                return [{"id": "db-1", "title": "Projects"}, {"id": "db-2", "title": "Notes"}]

            def scan_for_unshared_database_references(self, databases):
                return [{"id": "db-3", "source_title": "Projects", "property_name": "Related DB"}]

            def pull_from_notion(self):
                self.last_summary = "Pulled 2 changed page(s)."
                return True

        class FakeClickUpEngine:
            def __init__(self, db_manager):
                self.db_manager = db_manager
                self.api_token = "token"
                self.last_summary = "ClickUp refresh complete"

            def discover_accessible_lists(self):
                return [{"id": "list-1", "name": "Ops", "team_id": "team-7"}]

            def run_sync(self):
                self.last_summary = "ClickUp sync complete"
                return True

        saved_pairs = []
        results = []

        with patch("src.ui.tray.DatabaseManager", return_value=fake_db), \
             patch("src.ui.tray.SyncEngine", FakeSyncEngine), \
             patch("src.ui.tray.ClickUpSyncEngine", FakeClickUpEngine), \
             patch("src.ui.tray.save_env_var", side_effect=lambda key, value: saved_pairs.append((key, value))):
            worker = __import__("src.ui.tray", fromlist=["ConnectionResyncWorker"]).ConnectionResyncWorker("notion")
            worker.finished.connect(lambda success, summary, details: results.append((success, summary, details)))
            worker.run()

        self.assertTrue(results)
        self.assertTrue(results[0][0])
        self.assertIn(("NOTION_DB_ID", "ALL"), saved_pairs)
        self.assertNotIn(("CLICKUP_LIST_IDS", "ALL"), saved_pairs)
        self.assertTrue(fake_db.closed)

    def test_connection_resync_worker_can_refresh_clickup_only(self):
        class FakeDb:
            def __init__(self):
                self.last_error = ""
                self.closed = False

            def connect(self):
                return True

            def close(self):
                self.closed = True

        fake_db = FakeDb()

        class FakeSyncEngine:
            def __init__(self, db_manager):
                self.db_manager = db_manager
                self.notion = object()
                self.last_summary = "Notion refresh complete"

        class FakeClickUpEngine:
            def __init__(self, db_manager):
                self.db_manager = db_manager
                self.api_token = "token"
                self.last_summary = "ClickUp refresh complete"

            def discover_accessible_lists(self):
                return [{"id": "list-1", "name": "Ops", "team_id": "team-7"}]

            def run_sync(self):
                self.last_summary = "ClickUp sync complete"
                return True

        saved_pairs = []
        results = []

        with patch("src.ui.tray.DatabaseManager", return_value=fake_db), \
             patch("src.ui.tray.SyncEngine", FakeSyncEngine), \
             patch("src.ui.tray.ClickUpSyncEngine", FakeClickUpEngine), \
             patch("src.ui.tray.save_env_var", side_effect=lambda key, value: saved_pairs.append((key, value))):
            worker = __import__("src.ui.tray", fromlist=["ConnectionResyncWorker"]).ConnectionResyncWorker("clickup")
            worker.finished.connect(lambda success, summary, details: results.append((success, summary, details)))
            worker.run()

        self.assertTrue(results)
        self.assertTrue(results[0][0])
        self.assertIn(("CLICKUP_LIST_IDS", "ALL"), saved_pairs)
        self.assertIn(("CLICKUP_TEAM_ID", "team-7"), saved_pairs)
        self.assertNotIn(("NOTION_DB_ID", "ALL"), saved_pairs)
        self.assertTrue(fake_db.closed)


class DashboardUxTests(unittest.TestCase):
    def test_statistics_summary_mentions_size_and_media(self):
        summary = AdminDashboard.build_statistics_summary(
            {
                "connected": True,
                "database_size_text": "12.5 MB",
                "active_pages": 42,
                "total_versions": 84,
                "pages_with_media": 7,
                "pending_push": 3,
                "last_pull_at": "2026-04-05 09:00:00",
                "last_push_at": "2026-04-05 09:30:00",
                "latest_local_update": "2026-04-05 10:00:00",
                "latest_source_update": "2026-04-05 11:00:00",
            }
        )

        self.assertIn("12.5 MB", summary)
        self.assertIn("Pages with media", summary)
        self.assertIn("Pending push", summary)


class BrandingConfigTests(unittest.TestCase):
    def test_app_display_name_defaults_to_custom_brand(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_app_display_name(), "Martin's Brain Dump Manager")

    def test_claude_mcp_name_defaults_to_business_brain(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_claude_mcp_name(), "Business Brain")


class StartupConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_windows_startup_defaults_to_on(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(is_windows_startup_enabled())

    def test_settings_page_starts_with_windows_toggle_checked_by_default(self):
        fake_tray = SimpleNamespace(
            show_wizard=lambda: None,
            configure_mcp=lambda: None,
            run_backup_now=lambda silent=False: None,
            open_logs=lambda: None,
            get_backup_summary_text=lambda: "Ready",
            maintenance_manager=SimpleNamespace(get_status_text=lambda: "Scheduled"),
        )
        main_window = SimpleNamespace(tray_app=fake_tray)

        with patch.dict(os.environ, {}, clear=True):
            page = SettingsPage(main_window)
            self.addCleanup(page.deleteLater)

            self.assertTrue(page.startup_toggle.isChecked())
            self.assertIn("Windows", page.startup_toggle.text())

    def test_settings_page_focuses_on_app_settings_not_setup_or_mcp(self):
        fake_tray = SimpleNamespace(
            show_wizard=lambda: None,
            configure_mcp=lambda: None,
            run_backup_now=lambda silent=False: None,
            open_logs=lambda: None,
            get_backup_summary_text=lambda: "Ready",
            maintenance_manager=SimpleNamespace(get_status_text=lambda: "Scheduled"),
        )
        main_window = SimpleNamespace(tray_app=fake_tray)

        with patch.dict(os.environ, {}, clear=True):
            page = SettingsPage(main_window)
            self.addCleanup(page.deleteLater)

            label_text = " ".join(
                widget.text() for widget in page.findChildren(QLabel) if hasattr(widget, "text")
            )

            self.assertNotIn("step-by-step guide", label_text.lower())
            self.assertNotIn("Claude MCP connection", label_text)
            self.assertIn("Start with Windows", label_text)
            self.assertIn("Backups and logs", label_text)

    def test_default_registry_includes_claude_mcp_service(self):
        labels = [plugin.nav_label for plugin in build_default_registry().all()]

        self.assertIn("Claude (MCP)", labels)

    def test_build_windows_startup_command_uses_pythonw_and_startup_flag(self):
        command = build_windows_startup_command(
            Path("C:/demo/NotionLocalSync"),
            python_executable="C:/demo/NotionLocalSync/venv/Scripts/python.exe",
        )

        self.assertIn("pythonw.exe", command)
        self.assertIn("src", command)
        self.assertIn("main.py", command)
        self.assertIn("--startup", command)


class MaintenanceManagerTests(unittest.TestCase):
    def test_is_task_due_respects_hourly_interval(self):
        now = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)

        self.assertTrue(MaintenanceManager.is_task_due(None, 6, now=now))
        self.assertFalse(MaintenanceManager.is_task_due("2026-04-05T13:00:00+00:00", 6, now=now))

    def test_maintenance_window_defaults_to_night_hours(self):
        manager = MaintenanceManager(db_manager=None)

        self.assertTrue(manager.is_within_maintenance_window(now=datetime(2026, 4, 6, 3, 0, tzinfo=timezone.utc)))
        self.assertFalse(manager.is_within_maintenance_window(now=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc)))

    @patch.dict(os.environ, {}, clear=True)
    def test_catch_up_after_wake_is_off_by_default(self):
        manager = MaintenanceManager(db_manager=None)

        self.assertFalse(manager.should_catch_up_after_wake())

    @patch.dict(
        os.environ,
        {
            "MAINTENANCE_RUNS_NIGHT_ONLY": "1",
            "MAINTENANCE_START_TIME": "02:30",
            "MAINTENANCE_WINDOW_HOURS": "4",
            "MAINTENANCE_RUN_ON_WAKE": "0",
        },
        clear=False,
    )
    def test_preview_due_maintenance_waits_for_night_window(self):
        manager = MaintenanceManager(db_manager=None)

        should_run, summary, details = manager.preview_due_maintenance(
            state={
                manager.TASK_LOCAL: {"last_run_at": None},
                manager.TASK_INTERNAL: {"last_run_at": None},
            },
            now=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(should_run)
        self.assertIn("waiting for the night window", summary)
        self.assertFalse(details.get("ran_any"))
        self.assertEqual(details.get("tasks"), [])

    @patch.dict(
        os.environ,
        {
            "MAINTENANCE_RUNS_NIGHT_ONLY": "1",
            "MAINTENANCE_START_TIME": "02:30",
            "MAINTENANCE_WINDOW_HOURS": "4",
            "MAINTENANCE_RUN_ON_WAKE": "0",
        },
        clear=False,
    )
    def test_get_due_tasks_waits_until_night_window_opens(self):
        manager = MaintenanceManager(db_manager=None)

        due_tasks = manager.get_due_tasks(
            state={
                manager.TASK_LOCAL: {"last_run_at": None},
                manager.TASK_INTERNAL: {"last_run_at": None},
            },
            local_interval_hours=12,
            notion_interval_hours=24,
            internal_interval_hours=24,
            now=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(due_tasks, [])

    @patch.dict(
        os.environ,
        {
            "MAINTENANCE_RUNS_NIGHT_ONLY": "1",
            "MAINTENANCE_START_TIME": "02:30",
            "MAINTENANCE_WINDOW_HOURS": "4",
            "MAINTENANCE_RUN_ON_WAKE": "1",
        },
        clear=False,
    )
    def test_get_due_tasks_can_catch_up_after_wake(self):
        manager = MaintenanceManager(db_manager=None)

        due_tasks = manager.get_due_tasks(
            state={
                manager.TASK_LOCAL: {"last_run_at": "2026-04-05T01:00:00+00:00"},
                manager.TASK_INTERNAL: {"last_run_at": "2026-04-05T01:00:00+00:00"},
            },
            local_interval_hours=12,
            notion_interval_hours=24,
            internal_interval_hours=24,
            now=datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(due_tasks, [manager.TASK_LOCAL, manager.TASK_INTERNAL])

    def test_notion_readiness_reports_missing_support_files(self):
        manager = MaintenanceManager(db_manager=None, app_root=Path("C:/tmp/app"), workspace_root=Path("C:/tmp/workspace"))

        readiness = manager.get_notion_maintenance_readiness()

        self.assertFalse(readiness["ready"])
        self.assertIn("maintenance_config.json", readiness["summary"])

    @patch.dict(os.environ, {"MAINTENANCE_RUNS_NIGHT_ONLY": "0"}, clear=False)
    def test_due_tasks_include_only_items_ready_to_run(self):
        manager = MaintenanceManager(db_manager=None, app_root=Path("C:/tmp/app"), workspace_root=Path("C:/tmp/workspace"))
        now = datetime(2026, 4, 5, 18, 0, tzinfo=timezone.utc)
        state = {
            "local_database": {"last_run_at": "2026-04-05T02:00:00+00:00"},
            "notion_database": {"last_run_at": "2026-04-05T16:30:00+00:00"},
        }

        due_tasks = manager.get_due_tasks(state, local_interval_hours=12, notion_interval_hours=6, now=now)

        self.assertEqual(due_tasks, ["local_database"])

    def test_format_status_summary_is_user_friendly(self):
        summary = MaintenanceManager.format_status_summary(
            {
                "local_database": {
                    "last_status": "success",
                    "last_summary": "Optimised the local mirror and refreshed query stats.",
                    "last_run_at": "2026-04-05T15:30:00+00:00",
                },
                "notion_database": {
                    "last_status": "skipped",
                    "last_summary": "Waiting for maintenance_config.json before remote cleanup can run.",
                    "last_run_at": None,
                },
            }
        )

        self.assertIn("Local DB", summary)
        self.assertIn("Notion DB", summary)
        self.assertIn("Waiting for maintenance_config.json", summary)

    def test_clickup_readiness_reports_missing_fields(self):
        manager = MaintenanceManager(db_manager=None, app_root=Path("C:/tmp/app"), workspace_root=Path("C:/tmp/workspace"))

        readiness = manager.get_clickup_maintenance_readiness()

        self.assertFalse(readiness["ready"])
        self.assertIn("ClickUp token", readiness["summary"])
        self.assertIn("ClickUp list IDs", readiness["summary"])

    def test_manual_orphaned_media_cleanup_job_is_disabled(self):
        manager = MaintenanceManager(db_manager=None, app_root=Path("C:/tmp/app"), workspace_root=Path("C:/tmp/workspace"))

        with patch.object(manager, "_run_tasks", return_value=(True, "ok", {"ran_any": True})) as run_tasks:
            success, summary, details = manager.run_manual_maintenance("orphaned_media_cleanup")

        run_tasks.assert_not_called()
        self.assertTrue(success)
        self.assertIn("removed", summary.lower())
        self.assertFalse(details.get("ran_any", True))


if __name__ == "__main__":
    unittest.main()
