from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP
from psycopg2.extras import RealDictCursor

from src.core.config import get_env, get_secret, load_config, logger
from src.core.http import build_retry_session
from src.db.database import DatabaseManager
from src.sync.engine import SyncEngine

load_config()
mcp = FastMCP("Business Brain")


def _safe_text(value: str | None) -> str:
    return str(value or "").strip()


def _normalize_limit(value: int | None, default: int = 20, minimum: int = 1, maximum: int = 200) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _format_db_connect_error(last_error: str | None) -> str:
    text = _safe_text(last_error)
    if _is_connection_slots_error(last_error):
        return (
            "Could not connect to the Business Brain database: PostgreSQL has no free client slots. "
            "Close idle MCP clients or increase max_connections, then try again."
        )
    return text or "Could not connect to the Business Brain database."


def _is_connection_slots_error(last_error: str | None) -> bool:
    lowered = _safe_text(last_error).lower()
    return (
        "too many clients" in lowered
        or "too many connections" in lowered
        or "remaining connection slots are reserved" in lowered
    )


def _normalize_page_size(value: int | None, default: int = 100) -> int:
    return _normalize_limit(value, default=default, minimum=1, maximum=100)


def _notion_version() -> str:
    return _safe_text(get_env("NOTION_VERSION", "2022-06-28")) or "2022-06-28"


def _notion_api_base() -> str:
    return _safe_text(get_env("NOTION_API_BASE", "https://api.notion.com/v1")).rstrip("/") or "https://api.notion.com/v1"


def _notion_http_session(token: str):
    session = build_retry_session(user_agent="BusinessBrain-MCP/2.0")
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": _notion_version(),
        }
    )
    return session


def _get_notion_token() -> str:
    token = _safe_text(get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED"))
    if not token:
        raise RuntimeError("Notion is not configured. Set NOTION_TOKEN first.")
    return token


def _get_notion_client(db: DatabaseManager):
    engine = SyncEngine(db)
    if not engine.notion:
        raise RuntimeError("Notion is not configured. Set NOTION_TOKEN first.")
    return engine.notion


def _safe_text_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [_safe_text(value) for value in values if _safe_text(value)]


def _text_chunks(value: str, size: int = 1900) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    safe_size = max(1, min(int(size or 1900), 1900))
    return [text[index : index + safe_size] for index in range(0, len(text), safe_size)]


def _build_rich_text(value: str) -> list[dict[str, Any]]:
    text = _safe_text(value)
    if not text:
        return []
    return [{"type": "text", "text": {"content": chunk}} for chunk in _text_chunks(text)]


def _paragraph_block(text: str) -> dict[str, Any] | None:
    rich_text = _build_rich_text(text)
    if not rich_text:
        return None
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _heading_block(level: int, text: str) -> dict[str, Any] | None:
    rich_text = _build_rich_text(text)
    if not rich_text:
        return None
    normalized_level = min(max(int(level or 2), 1), 3)
    block_type = f"heading_{normalized_level}"
    return {"object": "block", "type": block_type, block_type: {"rich_text": rich_text}}


def _list_item_block(block_type: str, text: str) -> dict[str, Any] | None:
    rich_text = _build_rich_text(text)
    if not rich_text:
        return None
    return {"object": "block", "type": block_type, block_type: {"rich_text": rich_text}}


def _todo_block(text: str, checked: bool = False) -> dict[str, Any] | None:
    rich_text = _build_rich_text(text)
    if not rich_text:
        return None
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": rich_text, "checked": bool(checked)}}


def _callout_block(text: str) -> dict[str, Any] | None:
    rich_text = _build_rich_text(text)
    if not rich_text:
        return None
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": rich_text,
            "icon": {"emoji": "💡"},
            "color": "gray_background",
        },
    }


def _quote_block(text: str) -> dict[str, Any] | None:
    rich_text = _build_rich_text(text)
    if not rich_text:
        return None
    return {"object": "block", "type": "quote", "quote": {"rich_text": rich_text}}


def _code_block(code: str, language: str = "plain text") -> dict[str, Any] | None:
    rich_text = _build_rich_text(code)
    if not rich_text:
        return None
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": rich_text,
            "language": _safe_text(language) or "plain text",
        },
    }


def _divider_block() -> dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def _append_if_present(target: list[dict[str, Any]], block: dict[str, Any] | None) -> None:
    if block:
        target.append(block)


def _build_section_blocks(
    heading: str = "",
    heading_level: int = 2,
    paragraphs: list[str] | None = None,
    bulleted_items: list[str] | None = None,
    numbered_items: list[str] | None = None,
    todo_items: list[str] | None = None,
    checked_items: list[str] | None = None,
    callout: str = "",
    quote: str = "",
    code: str = "",
    code_language: str = "plain text",
    divider_before: bool = False,
    divider_after: bool = False,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    if divider_before:
        blocks.append(_divider_block())

    _append_if_present(blocks, _heading_block(heading_level, heading))

    for paragraph in _safe_text_list(paragraphs):
        _append_if_present(blocks, _paragraph_block(paragraph))

    _append_if_present(blocks, _callout_block(callout))
    _append_if_present(blocks, _quote_block(quote))

    for item in _safe_text_list(bulleted_items):
        _append_if_present(blocks, _list_item_block("bulleted_list_item", item))

    for item in _safe_text_list(numbered_items):
        _append_if_present(blocks, _list_item_block("numbered_list_item", item))

    for item in _safe_text_list(todo_items):
        _append_if_present(blocks, _todo_block(item, checked=False))

    for item in _safe_text_list(checked_items):
        _append_if_present(blocks, _todo_block(item, checked=True))

    _append_if_present(blocks, _code_block(code, language=code_language))

    if divider_after:
        blocks.append(_divider_block())

    return blocks


def _list_block_children(notion, block_id: str, page_size: int = 100, max_pages: int = 10) -> tuple[list[dict[str, Any]], int, bool]:
    safe_page_size = _normalize_page_size(page_size, default=100)
    unlimited_pages = int(max_pages or 0) <= 0
    safe_max_pages = _normalize_limit(max_pages, default=10, minimum=1, maximum=200)
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    page_count = 0

    while unlimited_pages or page_count < safe_max_pages:
        kwargs: dict[str, Any] = {"block_id": block_id, "page_size": safe_page_size}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = notion.blocks.children.list(**kwargs)
        batch = cast(list[dict[str, Any]], response.get("results", []))
        results.extend(batch)
        page_count += 1

        if not response.get("has_more"):
            return results, page_count, False
        cursor = cast(str | None, response.get("next_cursor"))
        if not cursor:
            return results, page_count, False

    return results, page_count, True


def _safe_blocks(children: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not children:
        return []
    return [item for item in children if isinstance(item, dict)]


def _chunked(items: list[dict[str, Any]], size: int = 100) -> list[list[dict[str, Any]]]:
    if not items:
        return []
    safe_size = max(1, min(int(size or 100), 100))
    return [items[index : index + safe_size] for index in range(0, len(items), safe_size)]


@contextmanager
def _db_session():
    try:
        configured_retries = int(_safe_text(get_env("PG_MCP_CONNECT_RETRIES", "3")) or "3")
    except (TypeError, ValueError):
        configured_retries = 3
    max_retries = _normalize_limit(configured_retries, default=3, minimum=1, maximum=8)
    backoff_seconds = 0.25
    db = DatabaseManager()

    for attempt in range(1, max_retries + 1):
        if db.connect(initialize=False, ensure_database=False):
            break

        last_error = db.last_error
        if not _is_connection_slots_error(last_error) or attempt >= max_retries:
            raise RuntimeError(_format_db_connect_error(last_error))

        logger.warning(
            f"Business Brain DB connection attempt {attempt}/{max_retries} hit connection limits; retrying in {backoff_seconds:.2f}s."
        )
        time.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2.0, 2.0)

    try:
        yield db
    finally:
        db.close()


@mcp.tool(description="Get a small, fast summary of the Business Brain workspace.")
def workspace_catalog_stats() -> dict[str, Any]:
    with _db_session() as db:
        conn = db.conn
        if conn is None:
            raise RuntimeError("Database connection is not available.")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM workspace_catalog_stats")
            row = cur.fetchone()
        conn.rollback()
        return dict(row or {})


@mcp.tool(description="Check current PostgreSQL connection usage to diagnose MCP availability issues.")
def db_connection_diagnostics() -> dict[str, Any]:
    with _db_session() as db:
        diagnostics = db.get_db_connection_diagnostics()

        usage = int(diagnostics.get("usage_percent", 0) or 0)
        total = int(diagnostics.get("total_connections", 0) or 0)
        usable = int(diagnostics.get("usable_connections", 0) or 0)
        error_text = _safe_text(cast(str | None, diagnostics.get("error")))

        if error_text:
            diagnostics["status"] = "error"
            diagnostics["summary"] = error_text
        elif usage >= 90:
            diagnostics["status"] = "critical"
            diagnostics["summary"] = f"PostgreSQL connection usage is critical ({total}/{usable}, {usage}%)."
        elif usage >= 80:
            diagnostics["status"] = "warning"
            diagnostics["summary"] = f"PostgreSQL connection usage is high ({total}/{usable}, {usage}%)."
        else:
            diagnostics["status"] = "ok"
            diagnostics["summary"] = f"PostgreSQL connection usage is healthy ({total}/{usable}, {usage}%)."

        return diagnostics


@mcp.tool(description="Search the Business Brain catalog without scanning the full mirror.")
def search_workspace_catalog(search_text: str, result_limit: int = 8) -> list[dict[str, Any]]:
    term = _safe_text(search_text)
    if not term:
        return []

    safe_limit = _normalize_limit(result_limit, default=8, maximum=50)
    with _db_session() as db:
        conn = db.conn
        if conn is None:
            raise RuntimeError("Database connection is not available.")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM search_workspace_catalog(%s, %s)", (term, safe_limit))
            rows = [dict(row) for row in cur.fetchall()]
        conn.rollback()
        return rows


@mcp.tool(description="Fetch a plain-language context preview for one Business Brain page.")
def get_workspace_page_context(notion_id: str) -> list[dict[str, Any]]:
    page_id = _safe_text(notion_id)
    if not page_id:
        return []

    with _db_session() as db:
        conn = db.conn
        if conn is None:
            raise RuntimeError("Database connection is not available.")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM get_workspace_page_context(%s)", (page_id,))
            rows = [dict(row) for row in cur.fetchall()]
        conn.rollback()
        return rows


@mcp.tool(description="Run a read-only SQL query against the local Business Brain mirror.")
def run_sql_query(sql: str, limit: int = 200) -> dict[str, Any]:
    statement = _safe_text(sql).rstrip(";")
    if not statement:
        raise ValueError("SQL is empty.")

    lowered = statement.lower()
    if not lowered.startswith(("select", "with", "explain")):
        raise ValueError("Read queries must start with SELECT, WITH, or EXPLAIN.")

    safe_limit = _normalize_limit(limit, default=200, maximum=1000)
    with _db_session() as db:
        conn = db.conn
        if conn is None:
            raise RuntimeError("Database connection is not available.")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(statement)
            rows = [dict(row) for row in cur.fetchmany(safe_limit + 1)] if cur.description else []
        conn.rollback()

        truncated = len(rows) > safe_limit
        return {
            "row_count": len(rows[:safe_limit]),
            "truncated": truncated,
            "rows": rows[:safe_limit],
        }


@mcp.tool(description="Run one SQL statement that changes the local Business Brain mirror.")
def execute_sql(sql: str) -> dict[str, Any]:
    statement = _safe_text(sql).rstrip(";")
    if not statement:
        raise ValueError("SQL is empty.")
    if ";" in statement:
        raise ValueError("Run one SQL statement at a time.")

    lowered = statement.lower()
    blocked_tokens = ("drop database", "alter system", "copy ", "\\copy", "grant ", "revoke ")
    if any(token in lowered for token in blocked_tokens):
        raise ValueError("That SQL statement is blocked for safety.")

    with _db_session() as db:
        conn = db.conn
        if conn is None:
            raise RuntimeError("Database connection is not available.")

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(statement)
                rows = [dict(row) for row in cur.fetchall()] if cur.description else []
                row_count = cur.rowcount
            conn.commit()

            safe_limit = 200
            return {
                "ok": True,
                "row_count": row_count,
                "truncated": len(rows) > safe_limit,
                "rows": rows[:safe_limit],
            }
        except Exception as exc:
            conn.rollback()
            raise RuntimeError(f"SQL execution failed: {exc}") from exc


@mcp.tool(description="List recently editable pages and whether they still need to be pushed to Notion.")
def list_editable_pages(limit: int = 50) -> list[dict[str, Any]]:
    with _db_session() as db:
        pages = db.list_editable_pages(limit=_normalize_limit(limit, default=50, maximum=200))
        return cast(list[dict[str, Any]], pages)


@mcp.tool(description="Queue a local title or summary edit for one page, ready for the next Notion push.")
def queue_local_edit(notion_id: str, title: str = "", ai_summary: str = "") -> dict[str, Any]:
    with _db_session() as db:
        success, message = db.queue_local_edit(
            notion_id=_safe_text(notion_id),
            title=_safe_text(title) or None,
            ai_summary=_safe_text(ai_summary) or None,
        )
        return {
            "ok": bool(success),
            "message": message,
            "notion_id": _safe_text(notion_id),
        }


@mcp.tool(description="Preview a bulk text cleanup before making any local Business Brain changes.")
def preview_bulk_replace(
    search_text: str,
    replace_text: str = "",
    field_scope: str = "title,ai_summary",
    case_sensitive: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    with _db_session() as db:
        fields = [part.strip() for part in str(field_scope or "title,ai_summary").split(",") if part.strip()]
        preview = db.preview_bulk_replace(
            search_text=search_text,
            replace_text=replace_text,
            fields=fields,
            case_sensitive=bool(case_sensitive),
            limit=_normalize_limit(limit, default=20, maximum=100),
        )
        return cast(dict[str, Any], preview)


@mcp.tool(description="Apply a bulk text cleanup locally and mark the changed pages for the next Notion push.")
def apply_bulk_replace(
    search_text: str,
    replace_text: str,
    field_scope: str = "title,ai_summary",
    case_sensitive: bool = False,
) -> dict[str, Any]:
    with _db_session() as db:
        fields = [part.strip() for part in str(field_scope or "title,ai_summary").split(",") if part.strip()]
        success, message, details = db.apply_bulk_replace(
            search_text=search_text,
            replace_text=replace_text,
            fields=fields,
            case_sensitive=bool(case_sensitive),
        )
        return {
            "ok": bool(success),
            "message": message,
            "details": cast(dict[str, Any], details),
        }


@mcp.tool(description="Push all queued local Business Brain edits back to Notion now.")
def push_local_changes() -> dict[str, Any]:
    with _db_session() as db:
        engine = SyncEngine(db)
        success = engine.push_to_notion()
        return {
            "ok": bool(success),
            "summary": engine.last_summary,
            "stats": cast(dict[str, Any], engine.last_run_stats.get("push", {})),
            "conflicts": cast(list[dict[str, Any]], engine.conflicts[:50]),
        }


@mcp.tool(description="Send a direct Notion API request with full endpoint access (use Notion v1 paths like /pages/{id} or /blocks/{id}/children).")
def notion_api_request(
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    notion_path = _safe_text(path)
    if not notion_path:
        raise ValueError("Notion API path is required.")

    if not notion_path.startswith("/"):
        notion_path = f"/{notion_path}"

    verb = _safe_text(method).upper() or "GET"
    if verb not in {"GET", "POST", "PATCH", "DELETE"}:
        raise ValueError("method must be one of GET, POST, PATCH, DELETE.")

    timeout_value = _normalize_limit(timeout_seconds, default=60, minimum=5, maximum=180)
    session = _notion_http_session(_get_notion_token())
    url = f"{_notion_api_base()}{notion_path}"
    params = query if isinstance(query, dict) else None
    payload = body if isinstance(body, dict) else None

    try:
        response = session.request(
            verb,
            url,
            params=params,
            json=payload,
            timeout=timeout_value,
        )
        content_type = _safe_text(response.headers.get("Content-Type"))
        result: dict[str, Any] = {
            "ok": 200 <= response.status_code < 300,
            "status_code": int(response.status_code),
            "path": notion_path,
            "method": verb,
        }
        if "application/json" in content_type.lower():
            result["data"] = cast(dict[str, Any], response.json())
        else:
            result["text"] = response.text
        return result
    except Exception as exc:
        raise RuntimeError(f"Notion API request failed: {exc}") from exc


@mcp.tool(description="List child blocks for a Notion page or block, with automatic pagination.")
def notion_list_block_children(block_id: str, page_size: int = 100, max_pages: int = 10) -> dict[str, Any]:
    target_id = _safe_text(block_id)
    if not target_id:
        raise ValueError("block_id is required.")

    with _db_session() as db:
        notion = _get_notion_client(db)
        results, page_count, has_more = _list_block_children(notion, target_id, page_size=page_size, max_pages=max_pages)

        return {
            "block_id": target_id,
            "count": len(results),
            "page_count": page_count,
            "has_more": bool(has_more),
            "results": results,
        }


@mcp.tool(description="Append blocks to a Notion page or block. Accepts raw Notion block objects.")
def notion_append_blocks(block_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    target_id = _safe_text(block_id)
    block_children = _safe_blocks(children)
    if not target_id:
        raise ValueError("block_id is required.")
    if not block_children:
        raise ValueError("children must contain at least one block object.")

    with _db_session() as db:
        notion = _get_notion_client(db)
        appended = 0
        last_response: dict[str, Any] = {}
        for chunk in _chunked(block_children, size=100):
            response = notion.blocks.children.append(block_id=target_id, children=chunk)
            last_response = cast(dict[str, Any], response)
            appended += len(chunk)
        return {
            "ok": True,
            "block_id": target_id,
            "appended": appended,
            "last_response": last_response,
        }


@mcp.tool(description="Replace all top-level content blocks on a page with new blocks.")
def notion_replace_page_content(page_id: str, children: list[dict[str, Any]], max_delete: int = 1000) -> dict[str, Any]:
    target_id = _safe_text(page_id)
    block_children = _safe_blocks(children)
    if not target_id:
        raise ValueError("page_id is required.")
    if not block_children:
        raise ValueError("children must contain at least one block object.")

    safe_max_delete = _normalize_limit(max_delete, default=1000, minimum=1, maximum=5000)
    unlimited_delete = int(max_delete or 0) <= 0

    with _db_session() as db:
        notion = _get_notion_client(db)
        existing, _, _ = _list_block_children(notion, target_id, page_size=100, max_pages=0)

        if not unlimited_delete and len(existing) > safe_max_delete:
            raise ValueError(
                f"Refusing to replace content because the page has {len(existing)} blocks, above max_delete={safe_max_delete}."
            )

        deleted = 0
        for block in existing:
            block_id = _safe_text(cast(str | None, block.get("id")))
            if not block_id:
                continue
            notion.blocks.delete(block_id=block_id)
            deleted += 1

        appended = 0
        for chunk in _chunked(block_children, size=100):
            notion.blocks.children.append(block_id=target_id, children=chunk)
            appended += len(chunk)

        return {
            "ok": True,
            "page_id": target_id,
            "deleted": deleted,
            "appended": appended,
        }


@mcp.tool(description="Append a complete section to a Notion page using simple text lists instead of raw Notion block JSON.")
def notion_append_section(
    page_id: str,
    heading: str = "",
    heading_level: int = 2,
    paragraphs: list[str] | None = None,
    bulleted_items: list[str] | None = None,
    numbered_items: list[str] | None = None,
    todo_items: list[str] | None = None,
    checked_items: list[str] | None = None,
    callout: str = "",
    quote: str = "",
    code: str = "",
    code_language: str = "plain text",
    divider_before: bool = False,
    divider_after: bool = False,
) -> dict[str, Any]:
    target_id = _safe_text(page_id)
    if not target_id:
        raise ValueError("page_id is required.")

    children = _build_section_blocks(
        heading=heading,
        heading_level=heading_level,
        paragraphs=paragraphs,
        bulleted_items=bulleted_items,
        numbered_items=numbered_items,
        todo_items=todo_items,
        checked_items=checked_items,
        callout=callout,
        quote=quote,
        code=code,
        code_language=code_language,
        divider_before=bool(divider_before),
        divider_after=bool(divider_after),
    )
    if not children:
        raise ValueError("At least one non-empty heading, paragraph, list item, callout, quote, or code block is required.")

    with _db_session() as db:
        notion = _get_notion_client(db)
        appended = 0
        for chunk in _chunked(children, size=100):
            notion.blocks.children.append(block_id=target_id, children=chunk)
            appended += len(chunk)

        return {
            "ok": True,
            "page_id": target_id,
            "appended": appended,
            "section": {
                "heading": _safe_text(heading),
                "paragraphs": len(_safe_text_list(paragraphs)),
                "bulleted_items": len(_safe_text_list(bulleted_items)),
                "numbered_items": len(_safe_text_list(numbered_items)),
                "todo_items": len(_safe_text_list(todo_items)),
                "checked_items": len(_safe_text_list(checked_items)),
                "has_callout": bool(_safe_text(callout)),
                "has_quote": bool(_safe_text(quote)),
                "has_code": bool(_safe_text(code)),
            },
        }


@mcp.tool(description="Update one existing Notion block (for example paragraph, heading, to_do, callout, etc.).")
def notion_update_block(block_id: str, block_payload: dict[str, Any]) -> dict[str, Any]:
    target_id = _safe_text(block_id)
    if not target_id:
        raise ValueError("block_id is required.")
    if not isinstance(block_payload, dict) or not block_payload:
        raise ValueError("block_payload must be a non-empty object.")

    with _db_session() as db:
        notion = _get_notion_client(db)
        response = notion.blocks.update(block_id=target_id, **block_payload)
        return {
            "ok": True,
            "block_id": target_id,
            "result": cast(dict[str, Any], response),
        }


@mcp.tool(description="Archive (delete) one Notion block.")
def notion_delete_block(block_id: str) -> dict[str, Any]:
    target_id = _safe_text(block_id)
    if not target_id:
        raise ValueError("block_id is required.")

    with _db_session() as db:
        notion = _get_notion_client(db)
        response = notion.blocks.delete(block_id=target_id)
        return {
            "ok": True,
            "block_id": target_id,
            "result": cast(dict[str, Any], response),
        }


@mcp.tool(description="Update Notion page properties or archive state directly.")
def notion_update_page(page_id: str, properties: dict[str, Any] | None = None, archived: bool | None = None) -> dict[str, Any]:
    target_id = _safe_text(page_id)
    if not target_id:
        raise ValueError("page_id is required.")

    kwargs: dict[str, Any] = {"page_id": target_id}
    if isinstance(properties, dict) and properties:
        kwargs["properties"] = properties
    if archived is not None:
        kwargs["archived"] = bool(archived)

    if len(kwargs) == 1:
        raise ValueError("Provide properties and/or archived to update the page.")

    with _db_session() as db:
        notion = _get_notion_client(db)
        response = notion.pages.update(**kwargs)
        return {
            "ok": True,
            "page_id": target_id,
            "result": cast(dict[str, Any], response),
        }


@mcp.tool(description="Create a new Notion page in a database or under a parent page.")
def notion_create_page(
    parent: dict[str, Any],
    properties: dict[str, Any],
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(parent, dict) or not parent:
        raise ValueError("parent is required and must be an object like {'database_id': '...'} or {'page_id': '...'}.")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("properties is required and must be a non-empty object.")

    kwargs: dict[str, Any] = {
        "parent": parent,
        "properties": properties,
    }
    safe_children = _safe_blocks(children)
    if safe_children:
        kwargs["children"] = safe_children

    with _db_session() as db:
        notion = _get_notion_client(db)
        response = notion.pages.create(**kwargs)
        return {
            "ok": True,
            "result": cast(dict[str, Any], response),
        }


def run_business_brain_mcp() -> None:
    logger.info("Starting Business Brain MCP bridge with write access.")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_business_brain_mcp()
