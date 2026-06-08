import copy
import hashlib
import hmac
import html
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from notion_client import Client
from notion_client.errors import APIResponseError

from src.core.config import MEDIA_DIR, get_env, get_secret, logger
from src.core.http import build_retry_session
from src.core.security import SecurityManager
from src.db.change_tracking import normalize_workspace_page_json, property_preview_value
from src.db.database import DatabaseManager


class ClickUpWebhookServer:
    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        callback=None,
        secret: str = "",
    ):
        self.host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        self.port = int(port or 8765)
        self.path = str(path or "/clickup/webhook").strip() or "/clickup/webhook"
        if not self.path.startswith("/"):
            self.path = f"/{self.path}"
        self.callback = callback
        self.secret = str(secret or "").strip()
        self.httpd = None
        self.thread = None
        self.last_message = "ClickUp webhook listener is idle."

    def is_running(self) -> bool:
        return bool(self.httpd and self.thread and self.thread.is_alive())

    def start(self) -> tuple[bool, str]:
        if self.is_running():
            self.last_message = f"ClickUp webhook listener is already running on {self.host}:{self.port}{self.path}."
            return True, self.last_message

        callback = self.callback
        expected_path = self.path
        shared_secret = self.secret

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, status_code: int, payload: dict):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # pragma: no cover - keep the local listener quiet
                return

            def do_GET(self):
                if self.path.split("?", 1)[0] != expected_path:
                    self._send_json(404, {"ok": False, "message": "Unknown ClickUp webhook path."})
                    return
                self._send_json(200, {"ok": True, "message": "ClickUp webhook listener is running."})

            def do_POST(self):
                if self.path.split("?", 1)[0] != expected_path:
                    self._send_json(404, {"ok": False, "message": "Unknown ClickUp webhook path."})
                    return

                body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
                if shared_secret:
                    provided_signature = str(self.headers.get("X-Signature") or self.headers.get("x-signature") or "").strip()
                    expected_signature = hmac.new(shared_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
                    if not provided_signature or not hmac.compare_digest(provided_signature, expected_signature):
                        self._send_json(403, {"ok": False, "message": "Webhook signature check failed."})
                        return

                try:
                    payload = json.loads(body.decode("utf-8") or "{}") if body else {}
                except json.JSONDecodeError:
                    self._send_json(400, {"ok": False, "message": "Webhook payload was not valid JSON."})
                    return

                success = True
                message = "Webhook received."
                if callable(callback):
                    try:
                        success, message = callback(payload)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        logger.error(f"ClickUp webhook callback failed: {exc}")
                        success = False
                        message = str(exc)

                self._send_json(200 if success else 500, {"ok": bool(success), "message": str(message or "Webhook processed.")})

        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            self.thread = threading.Thread(target=self.httpd.serve_forever, name="ClickUpWebhookServer", daemon=True)
            self.thread.start()
            self.last_message = f"ClickUp webhook listener is running on http://{self.host}:{self.port}{self.path}"
            logger.info(self.last_message)
            return True, self.last_message
        except OSError as exc:
            self.httpd = None
            self.thread = None
            self.last_message = f"ClickUp webhook listener could not start: {exc}"
            logger.warning(self.last_message)
            return False, self.last_message

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning(f"Could not stop the ClickUp webhook listener cleanly: {exc}")
        self.httpd = None
        self.thread = None
        self.last_message = "ClickUp webhook listener stopped."


class ClickUpSyncEngine:
    def __init__(self, db_manager: DatabaseManager | None):
        self.db = db_manager
        self.http = build_retry_session(user_agent="NotionLocalSync-ClickUp/2.0")
        self.last_summary = "Ready"
        self.api_token = get_secret("CLICKUP_TOKEN")
        self.team_id = str(get_env("CLICKUP_TEAM_ID", "") or "").strip()
        self.api_base = (get_env("CLICKUP_API_BASE", "https://api.clickup.com/api/v2") or "https://api.clickup.com/api/v2").strip().rstrip("/")
        raw_list_setting = str(get_env("CLICKUP_LIST_IDS", "") or "").strip()
        self.sync_all_lists = raw_list_setting.upper() == "ALL"
        self.list_ids = [part.strip() for part in raw_list_setting.split(",") if part.strip() and part.strip().upper() != "ALL"]

        self.http.headers.update({"Accept": "application/json"})
        if self.api_token:
            self.http.headers.update({"Authorization": self.api_token, "Content-Type": "application/json"})

    def is_configured(self, require_lists: bool = True) -> bool:
        return bool(self.api_token and ((self.sync_all_lists or self.list_ids) if require_lists else True))

    @staticmethod
    def _flatten_custom_value(value):
        if isinstance(value, dict):
            if "label" in value:
                return value.get("label")
            if "name" in value and len(value) == 1:
                return value.get("name")
            return {key: ClickUpSyncEngine._flatten_custom_value(item) for key, item in value.items()}
        if isinstance(value, list):
            flattened = [ClickUpSyncEngine._flatten_custom_value(item) for item in value]
            if all(isinstance(item, dict) and "name" in item for item in value):
                return [str(item.get("name", "")).strip() for item in value if str(item.get("name", "")).strip()]
            return flattened
        return value

    @staticmethod
    def _parse_clickup_timestamp(value):
        if value in (None, "", 0, "0"):
            return None
        if isinstance(value, (int, float)) or (isinstance(value, str) and str(value).strip().isdigit()):
            raw = int(str(value).strip())
            if raw > 10**12:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _html_to_markdown(value: str) -> str:
        text = str(value or "")
        if not text.strip():
            return ""
        replacements = [
            (r"<br\s*/?>", "\n"),
            (r"</p>", "\n\n"),
            (r"<p[^>]*>", ""),
            (r"</div>", "\n"),
            (r"<div[^>]*>", ""),
            (r"</li>", "\n"),
            (r"<li[^>]*>", "- "),
            (r"<h[1-6][^>]*>", "## "),
            (r"</h[1-6]>", "\n\n"),
            (r"<strong[^>]*>|<b[^>]*>", "**"),
            (r"</strong>|</b>", "**"),
            (r"<em[^>]*>|<i[^>]*>", "_"),
            (r"</em>|</i>", "_"),
            (r"<code[^>]*>", "`"),
            (r"</code>", "`"),
        ]
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _request_json(self, method: str, path: str, **kwargs):
        response = self.http.request(method.upper(), f"{self.api_base}/{str(path or '').lstrip('/')}", timeout=60, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def discover_accessible_lists(self) -> list[dict]:
        api_token = getattr(self, "api_token", None)
        if api_token is not None and not str(api_token or "").strip():
            return []

        discovered: list[dict] = []
        seen_ids: set[str] = set()

        payload = self._request_json("GET", "team")
        teams = payload.get("teams", []) if isinstance(payload, dict) else []
        requested_team_id = str(getattr(self, "team_id", "") or "").strip()

        if requested_team_id:
            matching_teams = [team for team in teams if str((team or {}).get("id") or "").strip() == requested_team_id]
            if matching_teams:
                teams = matching_teams

        def append_list(item: dict, team: dict, space: dict, folder: dict | None = None) -> None:
            list_id = str((item or {}).get("id") or "").strip()
            if not list_id or list_id in seen_ids:
                return
            seen_ids.add(list_id)
            discovered.append(
                {
                    "id": list_id,
                    "name": str((item or {}).get("name") or "Untitled list").strip() or "Untitled list",
                    "team_id": str((team or {}).get("id") or "").strip(),
                    "team_name": str((team or {}).get("name") or "").strip(),
                    "space_id": str((space or {}).get("id") or "").strip(),
                    "space_name": str((space or {}).get("name") or "").strip(),
                    "folder_id": str((folder or {}).get("id") or "").strip(),
                    "folder_name": str((folder or {}).get("name") or "").strip(),
                }
            )

        for team in teams:
            team_id = str((team or {}).get("id") or "").strip()
            if not team_id:
                continue

            try:
                space_payload = self._request_json("GET", f"team/{team_id}/space")
            except requests.RequestException as exc:
                logger.warning(f"Could not list ClickUp spaces for team {team_id}: {exc}")
                continue

            spaces = space_payload.get("spaces", []) if isinstance(space_payload, dict) else []
            for space in spaces:
                space_id = str((space or {}).get("id") or "").strip()
                if not space_id:
                    continue

                try:
                    folder_payload = self._request_json("GET", f"space/{space_id}/folder")
                except requests.RequestException as exc:
                    logger.warning(f"Could not list ClickUp folders for space {space_id}: {exc}")
                    folder_payload = {}

                folders = folder_payload.get("folders", []) if isinstance(folder_payload, dict) else []
                for folder in folders:
                    folder_id = str((folder or {}).get("id") or "").strip()
                    if not folder_id:
                        continue
                    try:
                        folder_list_payload = self._request_json("GET", f"folder/{folder_id}/list")
                    except requests.RequestException as exc:
                        logger.warning(f"Could not list ClickUp lists for folder {folder_id}: {exc}")
                        continue

                    for item in folder_list_payload.get("lists", []) if isinstance(folder_list_payload, dict) else []:
                        append_list(item, team, space, folder)

                try:
                    space_list_payload = self._request_json("GET", f"space/{space_id}/list")
                except requests.RequestException as exc:
                    logger.warning(f"Could not list ClickUp lists for space {space_id}: {exc}")
                    continue

                for item in space_list_payload.get("lists", []) if isinstance(space_list_payload, dict) else []:
                    append_list(item, team, space)

        discovered.sort(
            key=lambda item: (
                str(item.get("team_name") or "").lower(),
                str(item.get("space_name") or "").lower(),
                str(item.get("folder_name") or "").lower(),
                str(item.get("name") or "").lower(),
                str(item.get("id") or ""),
            )
        )
        return discovered

    def _target_list_ids(self) -> list[str]:
        if not getattr(self, "sync_all_lists", False):
            return list(getattr(self, "list_ids", []))

        discovered_ids = [
            str(item.get("id") or "").strip()
            for item in self.discover_accessible_lists()
            if str(item.get("id") or "").strip()
        ]
        return list(dict.fromkeys(discovered_ids))

    def get_webhook_config(self) -> dict:
        host = str(get_env("CLICKUP_WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"
        path = str(get_env("CLICKUP_WEBHOOK_PATH", "/clickup/webhook") or "/clickup/webhook").strip() or "/clickup/webhook"
        if not path.startswith("/"):
            path = f"/{path}"
        try:
            port = max(1, min(int(str(get_env("CLICKUP_WEBHOOK_PORT", "8765") or "8765").strip()), 65535))
        except ValueError:
            port = 8765
        enabled = str(get_env("CLICKUP_WEBHOOK_ENABLED", "1")).strip().lower() not in {"0", "false", "off", "no"}
        secret = str(get_env("CLICKUP_WEBHOOK_SECRET", "") or "").strip()
        return {
            "enabled": enabled,
            "host": host,
            "port": port,
            "path": path,
            "secret": secret,
            "endpoint": f"http://{host}:{port}{path}",
        }

    def create_webhook_server(self, callback=None) -> ClickUpWebhookServer:
        config = self.get_webhook_config()
        return ClickUpWebhookServer(
            host=config["host"],
            port=config["port"],
            path=config["path"],
            secret=config["secret"],
            callback=callback,
        )

    def _prepare_task_record(self, task: dict) -> dict:
        assignees = []
        for person in (task or {}).get("assignees", []) or []:
            if isinstance(person, dict):
                assignees.append(
                    {
                        "id": person.get("id"),
                        "username": person.get("username") or person.get("name") or "",
                        "email": person.get("email") or "",
                    }
                )

        custom_fields = {}
        for field in (task or {}).get("custom_fields", []) or []:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or field.get("id") or "").strip()
            if not field_name:
                continue
            custom_fields[field_name] = self._flatten_custom_value(field.get("value"))

        status = task.get("status") or {}
        priority = task.get("priority") or {}
        markdown_description = str(task.get("markdown_description") or "").strip()
        if not markdown_description:
            markdown_description = self._html_to_markdown(task.get("description") or "")

        parent_value = task.get("parent") or task.get("parent_id")
        parent_task_id = str(parent_value.get("id") if isinstance(parent_value, dict) else parent_value or "").strip() or None

        location_value = task.get("list") or {}
        folder_value = task.get("folder") or {}
        space_value = task.get("space") or {}
        return {
            "task_id": str(task.get("id") or "").strip(),
            "name": str(task.get("name") or "Untitled task").strip() or "Untitled task",
            "status": str(status.get("status") or status.get("name") or "").strip(),
            "assignees": assignees,
            "custom_fields": custom_fields,
            "markdown_description": markdown_description,
            "description_html": str(task.get("description") or "").strip(),
            "task_url": str(task.get("url") or "").strip(),
            "list_id": str(location_value.get("id") or task.get("list_id") or "").strip() or None,
            "folder_id": str(folder_value.get("id") or task.get("folder_id") or "").strip() or None,
            "space_id": str(space_value.get("id") or task.get("space_id") or "").strip() or None,
            "parent_task_id": parent_task_id,
            "priority": str(priority.get("priority") or priority.get("color") or "").strip() or None,
            "due_date": self._parse_clickup_timestamp(task.get("due_date")),
            "date_created": self._parse_clickup_timestamp(task.get("date_created")),
            "date_updated": self._parse_clickup_timestamp(task.get("date_updated")),
            "raw_json": copy.deepcopy(task or {}),
        }

    @staticmethod
    def _extract_comment_text(comment: dict) -> str:
        value = (
            comment.get("comment_text")
            or comment.get("comment")
            or comment.get("text")
            or comment.get("body")
            or ""
        )
        if isinstance(value, list):
            value = "\n".join(str(item) for item in value)
        return ClickUpSyncEngine._html_to_markdown(value)

    def _prepare_comment_record(self, task_id: str, comment: dict) -> dict:
        comment_id = str(comment.get("id") or comment.get("comment_id") or "").strip()
        if not comment_id:
            seed = json.dumps(comment or {}, sort_keys=True, default=str)
            comment_id = hashlib.sha1(f"{task_id}:{seed}".encode("utf-8")).hexdigest()
        return {
            "comment_id": comment_id,
            "task_id": str(task_id or "").strip(),
            "comment_text": self._extract_comment_text(comment or {}),
            "user_json": copy.deepcopy((comment or {}).get("user") or {}),
            "date_created": self._parse_clickup_timestamp((comment or {}).get("date") or (comment or {}).get("date_created")),
            "date_updated": self._parse_clickup_timestamp((comment or {}).get("date_updated") or (comment or {}).get("date")),
            "raw_json": copy.deepcopy(comment or {}),
        }

    def fetch_task(self, task_id: str) -> dict:
        return self._request_json("GET", f"task/{str(task_id or '').strip()}", params={"include_markdown_description": "true"})

    def fetch_comments(self, task_id: str) -> list[dict]:
        payload = self._request_json("GET", f"task/{str(task_id or '').strip()}/comment")
        if isinstance(payload, dict):
            comments = payload.get("comments") or payload.get("data") or []
            return [item for item in comments if isinstance(item, dict)]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def fetch_all_tasks(self) -> list[dict]:
        tasks: list[dict] = []
        target_list_ids = self._target_list_ids()
        if not target_list_ids:
            if getattr(self, "sync_all_lists", False):
                logger.warning("ClickUp sync is set to all accessible lists, but no lists were discovered for the current token.")
            return tasks

        for list_id in target_list_ids:
            page = 0
            while True:
                payload = self._request_json(
                    "GET",
                    f"list/{list_id}/task",
                    params={
                        "page": page,
                        "include_closed": "true",
                        "subtasks": "true",
                        "include_markdown_description": "true",
                    },
                )
                batch = payload.get("tasks", []) if isinstance(payload, dict) else []
                if not batch:
                    break
                tasks.extend(item for item in batch if isinstance(item, dict))
                if len(batch) < 100:
                    break
                page += 1
        return tasks

    def collect_tasks_and_comments(self, task_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
        tasks: list[dict] = []
        comments: list[dict] = []

        if task_ids:
            for task_id in dict.fromkeys(str(item).strip() for item in task_ids if str(item).strip()):
                task = self.fetch_task(task_id)
                if not isinstance(task, dict) or not task.get("id"):
                    continue
                prepared_task = self._prepare_task_record(task)
                tasks.append(prepared_task)
                for comment in self.fetch_comments(prepared_task["task_id"]):
                    comments.append(self._prepare_comment_record(prepared_task["task_id"], comment))
            return tasks, comments

        for task in self.fetch_all_tasks():
            detailed_task = task
            if not task.get("markdown_description") and task.get("id"):
                try:
                    detailed_task = self.fetch_task(str(task.get("id")))
                except requests.RequestException:
                    detailed_task = task
            prepared_task = self._prepare_task_record(detailed_task)
            if not prepared_task.get("task_id"):
                continue
            tasks.append(prepared_task)
            for comment in self.fetch_comments(prepared_task["task_id"]):
                comments.append(self._prepare_comment_record(prepared_task["task_id"], comment))

        return tasks, comments

    def run_sync(self, task_ids: list[str] | None = None, source: str = "manual") -> bool:
        sync_label = "ClickUp webhook update" if source == "webhook" else "ClickUp sync"

        if not self.db or (not self.db.conn and not self.db.connect()):
            self.last_summary = f"{sync_label} failed: the local database is not ready yet."
            return False

        if not self.db.ensure_clickup_schema():
            self.last_summary = f"{sync_label} failed: the ClickUp tables could not be prepared."
            return False

        if not self.is_configured(require_lists=not bool(task_ids)):
            missing_bits = ["CLICKUP_TOKEN"]
            if not task_ids:
                missing_bits.append("CLICKUP_LIST_IDS")
            self.last_summary = f"{sync_label} failed: add {' and '.join(missing_bits)} in your local settings first."
            self.db.log_service_sync_run("clickup", "failed", self.last_summary, {"source": source, "task_ids": list(task_ids or [])})
            return False

        try:
            tasks, comments = self.collect_tasks_and_comments(task_ids=task_ids)
            task_scope = [task.get("task_id") for task in tasks if task.get("task_id")]
            full_refresh = not bool(task_ids)
            task_stats = self.db.upsert_clickup_tasks(tasks, retire_missing=full_refresh)
            comment_stats = self.db.upsert_clickup_comments(
                comments,
                scope_task_ids=None if full_refresh else task_scope,
                retire_missing=full_refresh or bool(task_scope),
            )
            self.last_summary = (
                f"{sync_label} complete: {int(task_stats.get('seen', 0) or 0)} task(s) checked, "
                f"{int(task_stats.get('inserted', 0) or 0)} new, "
                f"{int(task_stats.get('updated', 0) or 0)} updated, and "
                f"{int(comment_stats.get('seen', 0) or 0)} comment(s) refreshed."
            )
            details = {
                "source": source,
                "task_scope": task_scope,
                "tasks": task_stats,
                "comments": comment_stats,
            }
            self.db.log_service_sync_run("clickup", "success", self.last_summary, details)
            self.db.log_activity(
                category="clickup",
                action="webhook_update" if source == "webhook" else "sync_tasks",
                summary=self.last_summary,
                status="info",
                details=details,
                snapshot_at=datetime.now(timezone.utc),
            )
            return True
        except requests.RequestException as exc:
            self.last_summary = f"{sync_label} failed: {exc}"
            logger.error(self.last_summary)
            if self.db:
                self.db.log_service_sync_run("clickup", "failed", self.last_summary, {"source": source, "task_ids": list(task_ids or [])})
            return False
        except Exception as exc:
            self.last_summary = f"{sync_label} failed: {exc}"
            logger.error(self.last_summary)
            if self.db:
                self.db.log_service_sync_run("clickup", "failed", self.last_summary, {"source": source, "task_ids": list(task_ids or [])})
            return False

    def handle_webhook_payload(self, payload) -> tuple[bool, str]:
        task_ids = self.extract_task_ids_from_webhook(payload)
        if not task_ids:
            message = "ClickUp webhook received, but it did not include a task ID to refresh."
            self.last_summary = message
            if self.db:
                self.db.log_service_sync_run("clickup", "info", message, {"payload": payload})
            return True, message

        success = self.run_sync(task_ids=task_ids, source="webhook")
        return success, self.last_summary

    @staticmethod
    def extract_task_ids_from_webhook(payload) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()

        def add(value):
            text = str(value or "").strip()
            if text and text not in seen:
                seen.add(text)
                found.append(text)

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"task_id", "parent_id"}:
                        add(value)
                    elif key == "task" and isinstance(value, dict):
                        add(value.get("id"))
                        walk(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return found


class N8nSyncEngine:
    def __init__(self, db_manager: DatabaseManager | None):
        self.db = db_manager
        self.http = build_retry_session(user_agent="NotionLocalSync-n8n/2.0")
        self.last_summary = "Ready"
        raw_base_url = get_env("N8N_API_URL", "") or get_env("N8N_BASE_URL", "")
        self.base_url = self.normalize_base_url(raw_base_url)
        self.verify_ssl = str(get_env("N8N_VERIFY_SSL", "1")).strip().lower() not in {"0", "false", "off", "no"}
        self.api_key = get_secret("N8N_API_KEY")

        self.http.headers.update({"Accept": "application/json"})
        if self.api_key:
            self.http.headers.update({"X-N8N-API-KEY": self.api_key})

    @staticmethod
    def normalize_base_url(url: str) -> str:
        cleaned = str(url or "").strip().rstrip("/")
        if not cleaned:
            return ""

        cleaned = re.sub(r"/(workflows|executions|credentials|users)$", "", cleaned, flags=re.IGNORECASE)
        if re.search(r"/api/v\d+$", cleaned, flags=re.IGNORECASE):
            return cleaned
        if re.search(r"/api$", cleaned, flags=re.IGNORECASE):
            return f"{cleaned}/v1"
        return f"{cleaned}/api/v1"

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _build_url(self, path: str) -> str:
        return f"{self.base_url}/{str(path or '').lstrip('/')}" if self.base_url else str(path or "")

    @staticmethod
    def _extract_workflow_items(payload) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "workflows", "items", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if payload.get("id") and payload.get("name"):
                return [payload]
        return []

    @staticmethod
    def _prepare_restore_payload(workflow_json: dict) -> dict:
        source = copy.deepcopy(workflow_json or {})
        payload = {
            "name": str(source.get("name") or "Restored workflow").strip() or "Restored workflow",
            "nodes": copy.deepcopy(source.get("nodes") or []),
            "connections": copy.deepcopy(source.get("connections") or {}),
            "settings": copy.deepcopy(source.get("settings") or {}),
            "staticData": copy.deepcopy(source.get("staticData") or {}),
            "pinData": copy.deepcopy(source.get("pinData") or {}),
            "meta": copy.deepcopy(source.get("meta") or {}),
            "tags": copy.deepcopy(source.get("tags") or []),
            "active": bool(source.get("active")),
        }
        if source.get("versionId"):
            payload["versionId"] = source.get("versionId")
        return payload

    def _request_json(self, method: str, path: str, **kwargs):
        response = self.http.request(
            method.upper(),
            self._build_url(path),
            timeout=60,
            verify=self.verify_ssl,
            **kwargs,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def sync_workflows(self) -> bool:
        if not self.db or (not self.db.conn and not self.db.connect()):
            self.last_summary = "n8n sync failed: the local database is not ready yet."
            return False

        if not self.db.ensure_n8n_schema():
            self.last_summary = "n8n sync failed: the n8n workflow table could not be prepared."
            return False

        if not self.is_configured():
            self.last_summary = "n8n sync failed: add `N8N_API_URL` and `N8N_API_KEY` in your `.env` first."
            self.db.log_service_sync_run("n8n", "failed", self.last_summary, {"endpoint": self.base_url})
            return False

        try:
            workflows: list[dict] = []
            next_cursor = None
            while True:
                params = {"limit": 250}
                if next_cursor:
                    params["cursor"] = next_cursor

                payload = self._request_json("GET", "workflows", params=params)
                workflows.extend(self._extract_workflow_items(payload))
                next_cursor = (payload.get("nextCursor") or payload.get("next_cursor")) if isinstance(payload, dict) else None
                if not next_cursor:
                    break

            stats = self.db.upsert_n8n_workflows(workflows)
            self.last_summary = (
                f"n8n sync complete: {int(stats.get('seen', 0) or 0)} workflow(s) checked, "
                f"{int(stats.get('inserted', 0) or 0)} new, "
                f"{int(stats.get('updated', 0) or 0)} updated, "
                f"{int(stats.get('unchanged', 0) or 0)} unchanged."
            )
            self.db.log_service_sync_run("n8n", "success", self.last_summary, stats)
            self.db.log_activity(
                category="n8n",
                action="sync_workflows",
                summary=self.last_summary,
                status="info",
                details=stats,
                snapshot_at=datetime.now(timezone.utc),
            )
            return True
        except requests.RequestException as exc:
            self.last_summary = f"n8n sync failed: {exc}"
            logger.error(self.last_summary)
            if self.db:
                self.db.log_service_sync_run("n8n", "failed", self.last_summary, {"endpoint": self.base_url})
            return False
        except Exception as exc:
            self.last_summary = f"n8n sync failed: {exc}"
            logger.error(self.last_summary)
            if self.db:
                self.db.log_service_sync_run("n8n", "failed", self.last_summary, {"endpoint": self.base_url})
            return False

    def restore_workflow_from_date(self, workflow_id: str, snapshot_at, create_if_missing: bool = True) -> tuple[bool, str]:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return False, "Choose a workflow ID first."

        if not self.db or (not self.db.conn and not self.db.connect()):
            self.last_summary = "n8n restore failed: the local database is not ready yet."
            return False, self.last_summary

        if not self.is_configured():
            self.last_summary = "n8n restore failed: add `N8N_API_URL` and `N8N_API_KEY` in your `.env` first."
            return False, self.last_summary

        snapshot = self.db.get_n8n_workflow_snapshot(workflow_id, snapshot_at)
        if not snapshot:
            self.last_summary = f"No saved n8n workflow snapshot was found for {workflow_id} at that time."
            return False, self.last_summary

        payload = self._prepare_restore_payload(snapshot.get("workflow_json") or {})

        try:
            restored_action = "updated"
            try:
                response_payload = self._request_json("PUT", f"workflows/{workflow_id}", json=payload)
            except requests.HTTPError as exc:
                response = getattr(exc, "response", None)
                if not create_if_missing or not response or response.status_code != 404:
                    raise
                post_payload = dict(payload)
                post_payload.pop("versionId", None)
                response_payload = self._request_json("POST", "workflows", json=post_payload)
                restored_action = "created"

            restored_items = self._extract_workflow_items(response_payload)
            if not restored_items and isinstance(response_payload, dict):
                restored_items = [response_payload]
            if restored_items:
                self.db.upsert_n8n_workflows(restored_items)

            when_text = self.db._normalize_timestamp(snapshot.get("valid_from")) or str(snapshot_at)
            self.last_summary = f"n8n restore complete: workflow {workflow_id} was {restored_action} from the {when_text} snapshot."
            self.db.log_service_sync_run(
                "n8n",
                "success",
                self.last_summary,
                {"workflow_id": workflow_id, "snapshot_at": when_text, "restore_action": restored_action},
            )
            self.db.log_activity(
                category="n8n",
                action="restore_workflow",
                summary=self.last_summary,
                status="info",
                details={"workflow_id": workflow_id, "snapshot_at": when_text, "restore_action": restored_action},
                snapshot_at=snapshot_at,
            )
            return True, self.last_summary
        except requests.RequestException as exc:
            self.last_summary = f"n8n restore failed: {exc}"
            logger.error(self.last_summary)
            if self.db:
                self.db.log_service_sync_run("n8n", "failed", self.last_summary, {"workflow_id": workflow_id})
            return False, self.last_summary
        except Exception as exc:
            self.last_summary = f"n8n restore failed: {exc}"
            logger.error(self.last_summary)
            if self.db:
                self.db.log_service_sync_run("n8n", "failed", self.last_summary, {"workflow_id": workflow_id})
            return False, self.last_summary


class SyncEngine:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.notion = None
        self.http = build_retry_session(user_agent="NotionLocalSync-Notion/2.0")
        self.download_cache: dict[str, str] = {}
        self.last_summary = "Ready"
        self.last_run_stats = {
            "pull": {"seen": 0, "changed": 0, "skipped": 0, "failed_databases": 0},
            "push": {"attempted": 0, "updated": 0, "failed": 0, "skipped": 0},
        }

        db_setting = get_env("NOTION_DB_ID").strip()
        self.sync_all_databases = db_setting.upper() == "ALL"
        self.db_ids = [db_id.strip() for db_id in db_setting.split(",") if db_id.strip() and db_id.strip().upper() != "ALL"]
        self.security = SecurityManager()

        token = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        if token:
            self.notion = Client(auth=token)

        self.conflicts: list[dict] = []

    def _prepare_page_for_storage(self, page: dict) -> dict:
        prepared_page, issues = normalize_workspace_page_json(page)
        page_id = str(prepared_page.get("id") or (page or {}).get("id") or "").strip() or "unknown"
        if issues:
            logger.warning(f"Normalized malformed Notion payload for {page_id}: {'; '.join(issues[:4])}")
        link_builder = getattr(self.db, "build_page_link_index", None)
        if not callable(link_builder):
            link_builder = DatabaseManager.build_page_link_index
        if callable(link_builder):
            prepared_page["_business_brain_links"] = link_builder(prepared_page)
        return prepared_page

    @staticmethod
    def _database_title(schema: dict, fallback: str = "Untitled database") -> str:
        return "".join(
            part.get("plain_text", "")
            for part in ((schema or {}).get("title") or [])
            if isinstance(part, dict)
        ).strip() or fallback

    @classmethod
    def _describe_database_schema(cls, schema: dict, db_id: str) -> tuple[str, str]:
        title = cls._database_title(schema, fallback=f"Database {db_id}")
        properties = (schema or {}).get("properties", {}) or {}
        if not isinstance(properties, dict):
            properties = {}
        lines = [f"Database schema for {title}"]

        for name, prop in properties.items():
            prop_type = str((prop or {}).get("type") or "unknown").strip() or "unknown"
            option_names: list[str] = []
            if prop_type == "status":
                option_names = [str(item.get("name") or "").strip() for item in ((prop.get("status") or {}).get("options") or []) if str(item.get("name") or "").strip()]
            elif prop_type == "select":
                option_names = [str(item.get("name") or "").strip() for item in ((prop.get("select") or {}).get("options") or []) if str(item.get("name") or "").strip()]
            line = f"- {name}: {prop_type}"
            if option_names:
                line += f" ({', '.join(option_names)})"
            lines.append(line)

        if len(lines) == 1:
            lines.append("- No property schema was returned by Notion yet.")

        return f"Database schema — {title}", "\n".join(lines)

    def _build_database_schema_payload(self, db_id: str, schema: dict, existing_record: dict | None = None) -> dict | None:
        if not db_id or not isinstance(schema, dict) or not schema:
            return None

        prepared_schema = self._prepare_page_for_storage(schema)
        title, ai_summary = self._describe_database_schema(prepared_schema, db_id)
        source_updated_at = self._page_last_edited_time(prepared_schema)
        media_paths: list[str] = []
        content_hash = self.db.compute_content_hash(title, ai_summary, prepared_schema, media_paths)

        if not self.db.page_requires_update(existing_record, source_updated_at, content_hash):
            return None

        return {
            "notion_id": db_id,
            "title": title,
            "ai_summary": ai_summary,
            "raw_json": prepared_schema,
            "media_paths": media_paths,
            "content_hash": content_hash,
            "source_updated_at": source_updated_at,
            "needs_push": False,
        }

    def _page_last_edited_time(self, page: dict) -> str:
        return str(page.get("last_edited_time") or "").strip()

    @staticmethod
    def _timestamp_not_newer(remote_value, local_value) -> bool:
        remote_ts = DatabaseManager._normalize_timestamp(remote_value)
        local_ts = DatabaseManager._normalize_timestamp(local_value)
        return bool(remote_ts and local_ts and remote_ts <= local_ts)

    def _workspace_latest_remote_edit_time(self) -> str:
        if not self.notion or not hasattr(self.notion, "search"):
            return ""

        try:
            response = self.notion.search(
                page_size=1,
                sort={"direction": "descending", "timestamp": "last_edited_time"},
            )
        except TypeError:
            response = self.notion.search(page_size=1)
        except Exception as exc:
            logger.warning(f"Quick sync check could not read the latest remote edit time: {exc}")
            return ""

        results = response.get("results", []) if isinstance(response, dict) else []
        if not results:
            return ""
        return self._page_last_edited_time(results[0])

    def _can_skip_workspace_pull(self) -> bool:
        if not getattr(self, "sync_all_databases", False) or not self.db or not hasattr(self.db, "get_stats"):
            return False

        try:
            stats = self.db.get_stats(max_age_seconds=15.0)
        except TypeError:
            stats = self.db.get_stats()
        except Exception:
            return False

        latest_local = (stats or {}).get("latest_source_update")
        latest_remote = self._workspace_latest_remote_edit_time()
        return self._timestamp_not_newer(latest_remote, latest_local)

    def _local_media_is_current(self, file_path: Path, source_updated_at: str) -> bool:
        if not file_path.exists() or file_path.stat().st_size == 0:
            return False

        if not source_updated_at:
            return True

        try:
            notion_dt = datetime.fromisoformat(source_updated_at.replace("Z", "+00:00"))
            local_dt = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
            return local_dt >= notion_dt
        except Exception:
            return True

    def download_media(self, url: str, filename: str, source_updated_at: str = "") -> str:
        safe_filename = re.sub(r'[<>:"/\\|?*]+', '_', Path(filename).name)
        file_path = MEDIA_DIR / safe_filename
        cache_key = f"{safe_filename}|{source_updated_at}"

        cached_path = self.download_cache.get(cache_key)
        if cached_path and Path(cached_path).exists():
            return cached_path

        if self._local_media_is_current(file_path, source_updated_at):
            self.download_cache[cache_key] = str(file_path)
            return str(file_path)

        try:
            response = self.http.get(url, stream=True, timeout=30)
            response.raise_for_status()
            temp_path = file_path.parent / f"{file_path.name}.part"
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(1024):
                    if chunk:
                        f.write(chunk)
            temp_path.replace(file_path)
            self.download_cache[cache_key] = str(file_path)
            return str(file_path)
        except Exception as e:
            logger.error(f"Failed to download media {url}: {e}")
        return ""

    def extract_text_and_media(self, page: dict, source_updated_at: str = ""):
        title = "Untitled"
        ai_summary = ""
        media_paths = []

        safe_page, _ = normalize_workspace_page_json(page)
        page_id = str(safe_page.get("id") or (page or {}).get("id") or "page").strip() or "page"
        props = safe_page.get("properties", {}) if isinstance(safe_page.get("properties", {}), dict) else {}

        for key, prop in props.items():
            if not isinstance(prop, dict):
                logger.warning(f"Skipping malformed Notion property {page_id}:{key}")
                continue

            prop_type = str(prop.get("type") or "").strip().lower()

            if prop_type == "title":
                title_list = prop.get("title", [])
                if title_list:
                    title = "".join(
                        item.get("plain_text", "")
                        for item in title_list
                        if isinstance(item, dict)
                    ).strip() or "Untitled"

            if prop_type == "rich_text":
                texts = prop.get("rich_text", [])
                text_content = "".join(
                    t.get("plain_text", "")
                    for t in texts
                    if isinstance(t, dict)
                )
                if text_content:
                    ai_summary += f"{key}: {text_content}\n"

            if prop_type in {"status", "select", "multi_select", "number", "checkbox", "date", "url", "email", "phone_number", "relation", "people"}:
                display_value = DatabaseManager._display_property_value(prop)
                if display_value:
                    ai_summary += f"{key}: {display_value}\n"

            if prop_type == "files":
                for file_item in prop.get("files", []):
                    if not isinstance(file_item, dict) or file_item.get("type") != "file":
                        continue

                    url = ((file_item.get("file") or {}).get("url") or "").strip()
                    if not url:
                        continue

                    filename = f"{page_id}_{file_item.get('name', 'attachment')}"
                    local_path = self.download_media(url, filename, source_updated_at)
                    if local_path:
                        proxy_name = Path(local_path).name
                        media_paths.append(local_path)
                        ai_summary += f"{key} Media Link: http://localhost:{get_env('MEDIA_PROXY_PORT', '8080')}/{proxy_name}\n"

        return title, ai_summary.strip(), media_paths

    @staticmethod
    def _extract_notion_title(item: dict, fallback_title: str = "Untitled") -> str:
        title_value = (item or {}).get("title") or (item or {}).get("name") or []
        if isinstance(title_value, str):
            return title_value.strip() or fallback_title
        if isinstance(title_value, list):
            text = "".join(
                part.get("plain_text", "")
                for part in title_value
                if isinstance(part, dict)
            ).strip()
            return text or fallback_title
        return fallback_title

    def _resolve_queryable_data_source_ids(self, db_id: str, schema: dict | None = None) -> list[str]:
        resolved_ids: list[str] = []
        seen_ids: set[str] = set()

        def add(value) -> None:
            normalized = DatabaseManager._normalize_notion_id(value)
            if normalized and normalized not in seen_ids:
                seen_ids.add(normalized)
                resolved_ids.append(normalized)

        schema = schema or self._retrieve_database_schema(db_id)
        object_type = str((schema or {}).get("object") or "").strip().lower()
        if object_type == "database":
            for item in (schema or {}).get("data_sources", []) or []:
                add((item or {}).get("id"))

        if not resolved_ids:
            add((schema or {}).get("id") or db_id)

        return resolved_ids

    @classmethod
    def _build_database_schema_summary(cls, schema: dict) -> str:
        source_schema = schema or {}
        object_type = str(source_schema.get("object") or "database").strip().lower() or "database"
        properties = source_schema.get("properties") or {}
        summary_lines = [
            f"Notion {object_type} schema snapshot.",
            f"Property count: {len(properties)}.",
        ]

        parent_type = str(((source_schema.get("parent") or {}).get("type") or "")).strip()
        if parent_type:
            summary_lines.append(f"Parent type: {parent_type}.")

        description = source_schema.get("description") or []
        if isinstance(description, list):
            description_text = "".join(
                part.get("plain_text", "")
                for part in description
                if isinstance(part, dict)
            ).strip()
            if description_text:
                summary_lines.append(f"Description: {description_text}")

        if properties:
            summary_lines.append("Properties:")
            for property_name, prop in list(sorted(properties.items(), key=lambda item: item[0].lower()))[:25]:
                prop_type = str((prop or {}).get("type") or "unknown").strip().lower() or "unknown"
                option_names: list[str] = []
                if prop_type in {"status", "select"}:
                    option_names = [
                        str((item or {}).get("name") or "").strip()
                        for item in ((prop.get(prop_type) or {}).get("options") or [])
                        if str((item or {}).get("name") or "").strip()
                    ]
                elif prop_type == "multi_select":
                    option_names = [
                        str((item or {}).get("name") or "").strip()
                        for item in ((prop.get("multi_select") or {}).get("options") or [])
                        if str((item or {}).get("name") or "").strip()
                    ]

                options_suffix = ""
                if option_names:
                    display_names = ", ".join(option_names[:6])
                    if len(option_names) > 6:
                        display_names = f"{display_names}, …"
                    options_suffix = f" ({display_names})"
                summary_lines.append(f"- {property_name}: {prop_type}{options_suffix}")

        return "\n".join(summary_lines).strip()

    def _build_database_schema_payload(self, db_id: str, schema: dict | None = None) -> dict | None:
        schema_payload, issues = normalize_workspace_page_json(copy.deepcopy(schema or self._retrieve_database_schema(db_id) or {}))
        if not isinstance(schema_payload, dict) or not schema_payload:
            return None
        if issues:
            logger.warning(f"Normalized malformed Notion schema payload for {db_id}: {'; '.join(issues[:4])}")

        notion_id = DatabaseManager._normalize_notion_id(schema_payload.get("id") or db_id)
        if not notion_id:
            return None

        schema_payload.setdefault("object", str(schema_payload.get("object") or "database").strip().lower() or "database")
        schema_payload["id"] = notion_id
        schema_payload["_business_brain_links"] = DatabaseManager.build_page_link_index(schema_payload)

        title = self._extract_notion_title(schema_payload, fallback_title="Untitled database")
        ai_summary = self._build_database_schema_summary(schema_payload)
        media_paths: list[str] = []

        return {
            "notion_id": notion_id,
            "title": f"Database schema — {title}",
            "ai_summary": ai_summary,
            "raw_json": schema_payload,
            "media_paths": media_paths,
            "content_hash": self.db.compute_content_hash(f"Database schema — {title}", ai_summary, schema_payload, media_paths),
            "source_updated_at": self._page_last_edited_time(schema_payload) or str(schema_payload.get("created_time") or "").strip(),
            "needs_push": False,
        }

    def discover_accessible_databases(self) -> list[dict]:
        if not self.notion:
            return []

        results = []
        next_cursor = None

        while True:
            search_args = {
                "filter": {"property": "object", "value": "data_source"},
                "page_size": 100,
            }
            if next_cursor:
                search_args["start_cursor"] = next_cursor

            response = self.notion.search(**search_args)
            results.extend(response.get("results", []))

            if not response.get("has_more"):
                break
            next_cursor = response.get("next_cursor")

        databases: list[dict] = []
        seen_ids: set[str] = set()

        def append_database(item: dict, fallback_title: str = "Untitled database") -> None:
            resolved_ids = self._resolve_queryable_data_source_ids((item or {}).get("id"), schema=item if isinstance(item, dict) else None)
            db_id = resolved_ids[0] if resolved_ids else DatabaseManager._normalize_notion_id((item or {}).get("id"))
            if not db_id or db_id in seen_ids:
                return

            seen_ids.add(db_id)
            title = self._extract_notion_title(item, fallback_title=fallback_title)
            databases.append(
                {
                    "id": db_id,
                    "title": title,
                    "url": str((item or {}).get("url") or ""),
                    "parent_type": ((item.get("parent") or {}) if isinstance(item, dict) else {}).get("type", "unknown"),
                }
            )

        for item in results:
            append_database(item)

        try:
            linked_missing = self.scan_for_unshared_database_references(databases)
            for finding in linked_missing:
                target_id = DatabaseManager._normalize_notion_id((finding or {}).get("id"))
                if not target_id or target_id in seen_ids:
                    continue

                schema = self._retrieve_database_schema(target_id)
                if not schema:
                    continue

                append_database(schema, fallback_title="Linked database")
        except Exception as exc:
            logger.warning(f"Could not augment the Notion discovery list with linked databases: {exc}")

        databases.sort(key=lambda item: item["title"].lower())
        return databases

    def _retrieve_database_schema(self, db_id: str) -> dict:
        if not self.notion or not db_id:
            return {}

        try:
            return self.notion.data_sources.retrieve(data_source_id=db_id)
        except Exception:
            try:
                return self.notion.databases.retrieve(database_id=db_id)
            except Exception as exc:
                logger.warning(f"Could not inspect Notion database schema for {db_id}: {exc}")
                return {}

    @staticmethod
    def _extract_database_reference_ids(value) -> list[str]:
        found_ids: list[str] = []
        seen_ids: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if key in {"database_id", "data_source_id"}:
                        normalized = DatabaseManager._normalize_notion_id(item)
                        if normalized and normalized not in seen_ids:
                            seen_ids.add(normalized)
                            found_ids.append(normalized)
                    else:
                        walk(item)
                return

            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(value)
        return found_ids

    def scan_for_unshared_database_references(self, databases: list[dict] | None = None) -> list[dict]:
        if not self.notion:
            return []

        visible_databases = list(databases or self.discover_accessible_databases())
        visible_ids = {
            DatabaseManager._normalize_notion_id((item or {}).get("id"))
            for item in visible_databases
            if (item or {}).get("id")
        }
        findings: list[dict] = []
        seen_keys: set[tuple[str, str, str]] = set()

        for item in visible_databases:
            source_id = DatabaseManager._normalize_notion_id((item or {}).get("id"))
            source_title = ((item or {}).get("title") or "Untitled database").strip() or "Untitled database"
            if not source_id:
                continue

            schema = self._retrieve_database_schema(source_id)
            properties = (schema or {}).get("properties", {})
            if not isinstance(properties, dict):
                properties = {}

            for property_name, prop in properties.items():
                prop_type = str((prop or {}).get("type") or "").strip().lower() or "unknown"
                for target_id in self._extract_database_reference_ids(prop):
                    if not target_id or target_id == source_id or target_id in visible_ids:
                        continue

                    key = (source_id, property_name, target_id)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    findings.append(
                        {
                            "id": target_id,
                            "source_id": source_id,
                            "source_title": source_title,
                            "property_name": property_name,
                            "property_type": prop_type,
                        }
                    )

        findings.sort(key=lambda item: (item.get("source_title", "").lower(), item.get("property_name", "").lower(), item.get("id", "")))
        return findings

    def _target_db_ids(self) -> list[str]:
        return [item["id"] for item in self.discover_accessible_databases()] if self.sync_all_databases else self.db_ids

    def _query_database_pages(self, db_id: str, schema: dict | None = None) -> list[dict]:
        if not self.notion or not hasattr(self.notion, "data_sources") or not hasattr(self.notion.data_sources, "query"):
            raise RuntimeError("The installed Notion client cannot query data sources.")

        results: list[dict] = []
        target_ids = self._resolve_queryable_data_source_ids(db_id, schema=schema)
        if not target_ids:
            raise RuntimeError(f"No queryable Notion data source was found for {db_id}.")

        for target_id in target_ids:
            next_cursor = None
            while True:
                query_args = {"page_size": 100}
                if next_cursor:
                    query_args["start_cursor"] = next_cursor

                response = self.notion.data_sources.query(data_source_id=target_id, **query_args)
                results.extend(response.get("results", []))

                if not response.get("has_more"):
                    break
                next_cursor = response.get("next_cursor")

        return results

    def _build_rich_text_payload(self, text: str, chunk_size: int = 1800) -> list[dict]:
        clean_text = (text or "").strip()
        if not clean_text:
            return []

        chunks = [clean_text[i:i + chunk_size] for i in range(0, len(clean_text), chunk_size)]
        return [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:10]]

    def _build_push_properties(self, record: dict) -> dict:
        raw_json, _ = normalize_workspace_page_json(record.get("raw_json") or {})
        existing_properties = raw_json.get("properties", {}) if isinstance(raw_json.get("properties", {}), dict) else {}
        properties = {}

        title = (record.get("title") or "Untitled").strip() or "Untitled"
        summary = (record.get("ai_summary") or "").strip()

        for key, prop in existing_properties.items():
            if prop.get("type") == "title":
                properties[key] = {"title": self._build_rich_text_payload(title[:1800])}
                break

        if summary:
            for key, prop in existing_properties.items():
                if prop.get("type") == "rich_text":
                    properties[key] = {"rich_text": self._build_rich_text_payload(summary)}
                    break

        return properties

    def _log_notion_sync_run(self, mode: str, status: str, summary: str, details: dict | None = None) -> None:
        if not self.db or not hasattr(self.db, "log_service_sync_run"):
            return

        payload = {"mode": str(mode or "sync")}
        if details:
            payload.update(details)

        try:
            self.db.log_service_sync_run("notion", str(status or "info"), str(summary or ""), payload)
        except Exception as exc:
            logger.warning(f"Could not write the Notion sync history entry: {exc}")

    def pull_from_notion(self):
        if not self.notion:
            self.last_summary = "Pull failed: Notion is not configured yet."
            logger.error("Notion client not configured. Cannot pull from Notion.")
            self._log_notion_sync_run("pull", "failed", self.last_summary, {"reason": "not_configured"})
            return False

        if self._can_skip_workspace_pull():
            self.last_run_stats["pull"] = {
                "seen": 0,
                "changed": 0,
                "skipped": 0,
                "protected_local": 0,
                "malformed_pages": 0,
                "failed_databases": 0,
                "fast_checked_databases": 0,
            }
            self.last_summary = "No remote changes were found during the quick sync check."
            logger.info(self.last_summary)
            self._log_notion_sync_run("pull", "success", self.last_summary, dict(self.last_run_stats["pull"]))
            return True

        target_db_ids = self._target_db_ids()
        if not target_db_ids:
            self.last_summary = "Pull failed: Notion is not configured yet."
            logger.error("No Notion databases are configured for sync.")
            self._log_notion_sync_run("pull", "failed", self.last_summary, {"reason": "no_target_databases"})
            return False

        logger.info(f"Starting Notion -> Local DB sync process for {len(target_db_ids)} database(s)...")
        total_seen = 0
        total_changed = 0
        total_skipped = 0
        protected_local = 0
        failures = []
        fast_checked_databases = 0
        malformed_pages = 0

        for db_id in target_db_ids:
            try:
                schema = self._retrieve_database_schema(db_id)
                quick_check_ids = [db_id]
                latest_remote = self._workspace_latest_remote_edit_time() if len(target_db_ids) == 1 else ""
                if latest_remote:
                    existing_map = self.db.get_existing_page_map(quick_check_ids)
                    existing_schema = existing_map.get(db_id)
                    if existing_schema and self._timestamp_not_newer(latest_remote, existing_schema.get("source_updated_at")):
                        fast_checked_databases += 1
                        logger.info(f"Fast check for database {db_id}: no newer remote edit detected.")
                        continue

                results = self._query_database_pages(db_id, schema=schema)
                total_seen += len(results)
                existing_map = self.db.get_existing_page_map([db_id, *[page.get("id") for page in results if page.get("id")]])
                payloads = []
                schema_payload = self._build_database_schema_payload(db_id, schema=schema)
                if schema_payload:
                    payloads.append(schema_payload)
                db_skipped = 0

                for page in results:
                    try:
                        page_id = page.get("id")
                        if not page_id:
                            malformed_pages += 1
                            logger.warning(f"Skipped a Notion page without an id in database {db_id}.")
                            continue

                        source_updated_at = self._page_last_edited_time(page)
                        existing_record = existing_map.get(page_id)
                        if existing_record and existing_record.get("needs_push"):
                            total_skipped += 1
                            db_skipped += 1
                            protected_local += 1
                            continue

                        if existing_record and self.db._normalize_timestamp(existing_record.get("source_updated_at")) == self.db._normalize_timestamp(source_updated_at):
                            total_skipped += 1
                            db_skipped += 1
                            continue

                        prepared_page = self._prepare_page_for_storage(page)
                        title, ai_summary, media_paths = self.extract_text_and_media(prepared_page, source_updated_at)
                        content_hash = self.db.compute_content_hash(title, ai_summary, prepared_page, media_paths)

                        if not self.db.page_requires_update(existing_record, source_updated_at, content_hash):
                            total_skipped += 1
                            db_skipped += 1
                            continue

                        payloads.append(
                            {
                                "notion_id": page_id,
                                "title": title,
                                "ai_summary": ai_summary,
                                "raw_json": prepared_page,
                                "media_paths": media_paths,
                                "content_hash": content_hash,
                                "source_updated_at": source_updated_at,
                                "needs_push": False,
                            }
                        )
                    except Exception as page_exc:
                        malformed_pages += 1
                        page_label = str((page or {}).get("id") or "unknown").strip() or "unknown"
                        logger.warning(f"Skipped malformed Notion page {page_label}: {page_exc}")
                        if self.db and hasattr(self.db, "log_activity"):
                            self.db.log_activity(
                                category="notion",
                                action="skip_malformed_page",
                                summary=f"Skipped malformed Notion page {page_label} during pull.",
                                status="warning",
                                details={"database_id": db_id, "error": str(page_exc)},
                                notion_id=page_label,
                            )
                        continue

                changed_count, extra_skipped = self.db.upsert_pages(payloads)
                total_changed += changed_count
                total_skipped += extra_skipped
                logger.info(
                    f"Processed database {db_id}: {changed_count} changed page(s), {db_skipped + extra_skipped} skipped page(s)."
                )
            except Exception as e:
                failures.append(db_id)
                logger.error(f"Pull from Notion failed for database {db_id}: {e}")

        self.last_run_stats["pull"] = {
            "seen": total_seen,
            "changed": total_changed,
            "skipped": total_skipped,
            "protected_local": protected_local,
            "malformed_pages": malformed_pages,
            "failed_databases": len(failures),
            "fast_checked_databases": fast_checked_databases,
        }

        pull_details = {
            **self.last_run_stats["pull"],
            "failed_database_ids": list(failures),
        }

        if failures and total_changed == 0 and total_seen == 0:
            self.last_summary = "Pull failed for every selected Notion database."
            self._log_notion_sync_run("pull", "failed", self.last_summary, pull_details)
            return False

        failure_note = f" Failed databases: {len(failures)}." if failures else ""
        protected_note = f" Protected local edits: {protected_local}." if protected_local else ""
        quick_note = f" Quick checks avoided a full scan for {fast_checked_databases} database(s)." if fast_checked_databases else ""
        self.last_summary = f"Pulled {total_changed} changed page(s) and skipped {total_skipped} page(s).{protected_note}{quick_note}{failure_note}"
        logger.info(
            f"Pull complete. Seen {total_seen} page(s), changed {total_changed}, skipped {total_skipped}, protected local edits {protected_local}."
        )
        self._log_notion_sync_run("pull", "warning" if failures else "success", self.last_summary, pull_details)
        return True

    @staticmethod
    def _is_page_gone(error):
        """Return True when a Notion API error means the page no longer exists
        (deleted, unshared from the integration, or archived/in trash) and a
        push should not be retried. Transient errors (timeouts, 429, 5xx) return
        False so they keep retrying."""
        code = getattr(error, "code", None)
        status = getattr(error, "status", None)
        message = str(error).lower()
        if code == "object_not_found" or status == 404 or "could not find page" in message:
            return True
        if "object_not_found" in message:
            return True
        if "archived" in message or "in trash" in message or "is in the trash" in message:
            return True
        return False

    def push_to_notion(self):
        if not self.notion:
            self.last_summary = "Push failed: Notion is not configured yet."
            logger.error("Notion client not configured. Cannot push to Notion.")
            self._log_notion_sync_run("push", "failed", self.last_summary, {"reason": "not_configured"})
            return False

        records = self.db.get_active_pages(only_needs_push=True)
        if not records:
            self.last_run_stats["push"] = {"attempted": 0, "updated": 0, "failed": 0, "skipped": 0}
            self.last_summary = "No local database changes are waiting to push to Notion."
            logger.info("No pending local changes found to push to Notion.")
            self._log_notion_sync_run("push", "success", self.last_summary, dict(self.last_run_stats["push"]))
            return True

        logger.info(f"Starting Local DB -> Notion push process for {len(records)} dirty record(s)...")
        updated_ids = []
        skipped_count = 0
        conflict_count = 0
        failure_count = 0
        gone_ids = []
        gone_count = 0
        self.conflicts = []
        normalizer = getattr(self.db, "_normalize_timestamp", DatabaseManager._normalize_timestamp)

        for record in records:
            notion_id = record.get("notion_id")
            if not notion_id:
                skipped_count += 1
                continue

            try:
                remote_page = self.notion.pages.retrieve(page_id=notion_id)
                remote_last_edited = normalizer(self._page_last_edited_time(remote_page))
                local_last_seen = normalizer(record.get("source_updated_at"))
                if local_last_seen and remote_last_edited and remote_last_edited != local_last_seen:
                    conflict_count += 1
                    self.conflicts.append(
                        {
                            "notion_id": notion_id,
                            "remote_last_edited": remote_last_edited,
                            "local_last_seen": local_last_seen,
                        }
                    )
                    logger.warning(
                        f"Skipped pushing page {notion_id}: Notion changed remotely after the last pull ({remote_last_edited})."
                    )
                    continue
            except Exception as exc:
                if self._is_page_gone(exc):
                    gone_ids.append(notion_id)
                    gone_count += 1
                    logger.warning(
                        f"Page {notion_id} no longer exists in Notion (deleted/archived); "
                        f"tombstoning locally to stop further push attempts: {exc}"
                    )
                    continue
                logger.warning(f"Could not confirm remote edit state for page {notion_id} before push: {exc}")

            properties = self._build_push_properties(record)
            if not properties:
                skipped_count += 1
                logger.info(f"Skipped pushing page {notion_id}: no writable title/rich_text properties found.")
                continue

            try:
                self.notion.pages.update(page_id=notion_id, properties=properties)
                updated_ids.append(notion_id)
            except Exception as e:
                if self._is_page_gone(e):
                    gone_ids.append(notion_id)
                    gone_count += 1
                    logger.warning(
                        f"Page {notion_id} no longer exists in Notion (deleted/archived); "
                        f"tombstoning locally to stop further push attempts: {e}"
                    )
                else:
                    failure_count += 1
                    logger.error(f"Push to Notion failed for page {notion_id}: {e}")

        local_mark_failed = False
        if updated_ids:
            local_mark_failed = not self.db.mark_pages_pushed(updated_ids)
            if local_mark_failed:
                failure_count += len(updated_ids)
                logger.warning("Notion updates succeeded, but the local push confirmation step failed. Those pages may still appear as pending until the next refresh.")

        if gone_ids:
            if not self.db.tombstone_missing_pages(gone_ids):
                logger.warning(
                    "Detected pages missing from Notion, but the local tombstone step failed. "
                    "They may continue to appear as pending until the next refresh."
                )

        self.last_run_stats["push"] = {
            "attempted": len(records),
            "updated": len(updated_ids),
            "failed": failure_count,
            "skipped": skipped_count,
            "conflicts": conflict_count,
            "gone": gone_count,
        }
        local_note = " Local confirmation needs attention." if local_mark_failed else ""
        self.last_summary = (
            f"Pushed {len(updated_ids)} page(s) to Notion, skipped {skipped_count}, "
            f"conflicts {conflict_count}, gone {gone_count}, failed {failure_count}.{local_note}"
        )
        logger.info(self.last_summary)
        push_success = failure_count == 0 or bool(updated_ids)
        push_status = "success"
        if not push_success:
            push_status = "failed"
        elif failure_count or conflict_count or gone_count:
            push_status = "warning"

        self._log_notion_sync_run(
            "push",
            push_status,
            self.last_summary,
            {
                **self.last_run_stats["push"],
                "updated_ids": list(updated_ids),
            },
        )
        return push_success

    def perform_sync(self):
        pull_success = self.pull_from_notion()
        pull_summary = self.last_summary
        if not pull_success:
            return False

        push_success = self.push_to_notion()
        pull_stats = self.last_run_stats.get("pull", {})
        push_stats = self.last_run_stats.get("push", {})
        self.last_summary = (
            f"Pull: {pull_stats.get('changed', 0)} changed, {pull_stats.get('skipped', 0)} skipped. "
            f"Push: {push_stats.get('updated', 0)} updated, {push_stats.get('failed', 0)} failed."
        )
        if not push_success:
            logger.warning(f"Push step reported issues after pull. Latest pull summary: {pull_summary}")
        return pull_success and push_success
