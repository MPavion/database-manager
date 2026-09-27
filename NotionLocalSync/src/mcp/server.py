"""
MCP server for Claude Desktop.

Tools:
  get_index       – full keyword index (one call → complete workspace map)
  search          – ranked full-text + keyword search
  get_page        – full page content + media paths
  list_recent     – most recently updated pages
  push_update     – write title/summary back to Notion
  get_stats       – quick counts

All tools try the local PostgreSQL DB first; if unavailable they fall back to
the Notion API so Claude is never left with nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP

from src.core.config import get_env, get_secret, load_config, logger
from src.core.http import build_retry_session
from src.db.database import DatabaseManager
from src.sync.engine import SyncEngine

load_config()
mcp = FastMCP(get_env("CLAUDE_MCP_NAME", "Notion Local DB"))

# Module-level singletons reused across tool calls
_db:     DatabaseManager | None = None
_engine: SyncEngine | None      = None


def _get_db() -> DatabaseManager:
    global _db
    if _db is None:
        _db = DatabaseManager()
    if not _db.is_connected():
        _db.connect()
    return _db


def _get_engine() -> SyncEngine:
    global _engine
    if _engine is None:
        _engine = SyncEngine(_get_db())
    return _engine


def _notion_headers() -> dict:
    token = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
    if not token:
        raise RuntimeError("Notion token not configured.")
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": get_env("NOTION_VERSION", "2022-06-28"),
    }


def _notion_session():
    s = build_retry_session(user_agent="NotionLocalSync-MCP/3.0")
    s.headers.update(_notion_headers())
    return s


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_index() -> dict:
    """
    Return the complete Claude-optimised workspace index in one call.

    Each entry contains: notion_id, title, keywords (array of tags),
    summary (≤400 chars), page_type, token_estimate, notion_url, updated_at.

    Use this as your table of contents before deciding which pages to load
    in full with get_page(). This is far cheaper than scanning Notion directly.
    """
    try:
        db = _get_db()
        if db.is_connected():
            rows = db.get_full_index()
            return {
                "source":  "local_db",
                "count":   len(rows),
                "entries": [
                    {
                        "notion_id":      r["notion_id"],
                        "title":          r.get("title") or "Untitled",
                        "keywords":       r.get("keywords") or [],
                        "summary":        r.get("summary") or "",
                        "page_type":      r.get("page_type") or "page",
                        "token_estimate": r.get("token_estimate") or 0,
                        "notion_url":     r.get("notion_url") or "",
                        "updated_at":     str(r.get("updated_at") or ""),
                    }
                    for r in rows
                ],
            }
    except Exception as exc:
        logger.warning(f"get_index local DB failed, falling back to Notion: {exc}")

    # Fallback: basic listing from Notion search API
    try:
        session  = _notion_session()
        base     = get_env("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")
        resp     = session.post(f"{base}/search", json={"page_size": 100}, timeout=15)
        resp.raise_for_status()
        results  = resp.json().get("results", [])
        entries  = []
        for r in results:
            nid   = r.get("id", "")
            title = ""
            props = r.get("properties", {})
            for prop in (props.values() if isinstance(props, dict) else []):
                if isinstance(prop, dict) and prop.get("type") == "title":
                    title = "".join(
                        p.get("plain_text", "") for p in prop.get("title") or []
                        if isinstance(p, dict)
                    ).strip()
                    break
            entries.append({
                "notion_id":      nid,
                "title":          title or "Untitled",
                "keywords":       [],
                "summary":        "(live Notion — index not yet built locally)",
                "page_type":      r.get("object", "page"),
                "token_estimate": 0,
                "notion_url":     r.get("url") or "",
                "updated_at":     r.get("last_edited_time") or "",
            })
        return {"source": "notion_api_fallback", "count": len(entries), "entries": entries}
    except Exception as exc:
        return {"error": f"Both local DB and Notion API unavailable: {exc}"}


@mcp.tool()
def search(query: str, limit: int = 10) -> dict:
    """
    Search the workspace by keyword, title, or content.

    Returns ranked results with title, keywords, summary preview, and
    Notion URL. Results come from the local DB (fast, offline) unless the DB
    is unavailable, in which case the Notion API search is used.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query must not be empty"}

    lim = max(1, min(int(limit or 10), 50))

    try:
        db = _get_db()
        if db.is_connected():
            rows = db.search_pages(q, lim)
            return {
                "source": "local_db",
                "query":  q,
                "count":  len(rows),
                "results": [
                    {
                        "notion_id":      r["notion_id"],
                        "title":          r.get("title") or "Untitled",
                        "summary":        r.get("summary_preview") or "",
                        "keywords":       r.get("keywords") or [],
                        "page_type":      r.get("page_type") or "page",
                        "has_media":      bool(r.get("has_media")),
                        "notion_url":     r.get("user_notion_url") or "",
                        "updated_at":     str(r.get("updated_at") or ""),
                        "match_rank":     float(r.get("match_rank") or 0),
                    }
                    for r in rows
                ],
            }
    except Exception as exc:
        logger.warning(f"search local DB failed, falling back: {exc}")

    # Fallback: Notion search API
    try:
        session = _notion_session()
        base    = get_env("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")
        resp    = session.post(f"{base}/search",
                               json={"query": q, "page_size": lim}, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return {
            "source":  "notion_api_fallback",
            "query":   q,
            "count":   len(results),
            "results": [
                {
                    "notion_id":  r.get("id", ""),
                    "title":      "Untitled",
                    "notion_url": r.get("url") or "",
                    "updated_at": r.get("last_edited_time") or "",
                }
                for r in results
            ],
        }
    except Exception as exc:
        return {"error": f"Both local DB and Notion API unavailable: {exc}"}


@mcp.tool()
def get_page(notion_id: str) -> dict:
    """
    Return the full content of a page: title, AI summary, all properties,
    media file paths, and the raw Notion JSON.

    Use this after get_index() or search() to load a specific page in full.
    """
    nid = (notion_id or "").strip()
    if not nid:
        return {"error": "notion_id is required"}

    try:
        db = _get_db()
        if db.is_connected():
            row = db.get_page(nid)
            if row:
                raw = row.get("raw_json") or {}
                props_out = {}
                for name, prop in (raw.get("properties") or {}).items():
                    if isinstance(prop, dict):
                        from src.db.change_tracking import property_preview_value
                        props_out[name] = property_preview_value(prop)
                return {
                    "source":          "local_db",
                    "notion_id":       row["notion_id"],
                    "title":           row.get("title") or "Untitled",
                    "ai_summary":      row.get("ai_summary") or "",
                    "properties":      props_out,
                    "media_paths":     row.get("media_local_paths") or [],
                    "notion_url":      raw.get("url") or "",
                    "source_updated":  str(row.get("source_updated_at") or ""),
                    "local_updated":   str(row.get("updated_at") or ""),
                    "needs_push":      bool(row.get("needs_push")),
                }
    except Exception as exc:
        logger.warning(f"get_page local DB failed for {nid}: {exc}")

    # Fallback: Notion pages API
    try:
        session = _notion_session()
        base    = get_env("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")
        resp    = session.get(f"{base}/pages/{nid}", timeout=15)
        resp.raise_for_status()
        page    = resp.json()
        title   = ""
        for prop in (page.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                title = "".join(
                    p.get("plain_text", "") for p in prop.get("title") or []
                    if isinstance(p, dict)
                ).strip()
                break
        return {
            "source":         "notion_api_fallback",
            "notion_id":      page.get("id", nid),
            "title":          title or "Untitled",
            "notion_url":     page.get("url") or "",
            "source_updated": page.get("last_edited_time") or "",
        }
    except Exception as exc:
        return {"error": f"Page {nid} not found in local DB or Notion: {exc}"}


@mcp.tool()
def list_recent(limit: int = 20) -> dict:
    """
    List the most recently updated pages, newest first.
    Useful for a quick 'what changed lately?' check.
    """
    lim = max(1, min(int(limit or 20), 100))

    try:
        db = _get_db()
        if db.is_connected():
            rows = db._execute(
                """
                SELECT wm.notion_id, wm.title, wm.updated_at, wm.source_updated_at,
                       wm.needs_push, pi.page_type, pi.keywords,
                       COALESCE(wm.raw_json ->> 'url', '') AS notion_url
                  FROM workspace_mirror wm
                  LEFT JOIN page_index pi ON pi.notion_id = wm.notion_id
                 WHERE wm.is_active = TRUE
                 ORDER BY wm.updated_at DESC NULLS LAST
                 LIMIT %s
                """,
                (lim,), fetch="all",
            )
            return {
                "source": "local_db",
                "count":  len(rows or []),
                "pages":  [
                    {
                        "notion_id":      r["notion_id"],
                        "title":          r.get("title") or "Untitled",
                        "page_type":      r.get("page_type") or "page",
                        "keywords":       r.get("keywords") or [],
                        "updated_at":     str(r.get("updated_at") or ""),
                        "source_updated": str(r.get("source_updated_at") or ""),
                        "needs_push":     bool(r.get("needs_push")),
                        "notion_url":     r.get("notion_url") or "",
                    }
                    for r in (rows or [])
                ],
            }
    except Exception as exc:
        logger.warning(f"list_recent failed: {exc}")
        return {"error": str(exc)}


@mcp.tool()
def push_update(notion_id: str, title: str | None = None,
                summary: str | None = None) -> dict:
    """
    Write a title or summary update back to Notion for a page already in the
    local DB.  The local record is flagged needs_push and the next sync will
    propagate it, or you can call this to push immediately.
    """
    nid = (notion_id or "").strip()
    if not nid:
        return {"error": "notion_id is required"}
    if not title and not summary:
        return {"error": "Provide at least one of title or summary to update."}

    try:
        db  = _get_db()
        row = db.get_page(nid)
        if not row:
            return {"error": f"Page {nid} not found in local DB."}

        from src.db.change_tracking import build_local_edit_payload
        payload = build_local_edit_payload(row, title=title, ai_summary=summary)
        ok = db.upsert_page(
            notion_id         = nid,
            title             = payload["title"],
            ai_summary        = payload["ai_summary"],
            raw_json          = payload["raw_json"],
            media_paths       = payload.get("media_local_paths") or [],
            content_hash      = payload["content_hash"],
            source_updated_at = str(row.get("source_updated_at") or ""),
            needs_push        = True,
        )
        if not ok:
            return {"error": "Failed to save update to local DB."}

        # Attempt immediate push
        engine = _get_engine()
        engine.push_to_notion()
        return {"status": "ok", "notion_id": nid, "pushed": True}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def get_stats() -> dict:
    """
    Return basic workspace statistics: page count, pending pushes, media pages,
    last sync time, and whether the local DB is reachable.
    """
    try:
        db  = _get_db()
        if db.is_connected():
            stats = db.get_stats()
            last  = db.get_last_pull_time()
            return {
                "source":          "local_db",
                "db_connected":    True,
                "active_pages":    int(stats.get("active_pages") or 0),
                "pending_push":    int(stats.get("pending_push") or 0),
                "pages_with_media": int(stats.get("pages_with_media") or 0),
                "last_sync":       str(last) if last else "never",
            }
    except Exception as exc:
        logger.warning(f"get_stats: {exc}")

    return {"db_connected": False, "error": "Local DB unavailable."}


def run_mcp_server():
    mcp.run()
