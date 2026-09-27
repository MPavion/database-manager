"""
Notion → local PostgreSQL pull sync.

The local DB is a read-only mirror of Notion. Claude reads from the DB;
any writes go directly to Notion via its own API. The DB is never pushed to.
"""

from __future__ import annotations

import copy
import re
import traceback as _traceback
from datetime import datetime, timezone
from pathlib import Path

from notion_client import Client

from src.core.config import MEDIA_DIR, get_env, get_secret, logger
from src.core.http import build_retry_session
from src.db.change_tracking import normalize_workspace_page_json, property_preview_value
from src.db.database import DatabaseManager


class SyncEngine:
    def __init__(self, db: DatabaseManager):
        self.db             = db
        self.http           = build_retry_session(user_agent="NotionLocalSync/3.0")
        self.download_cache: dict[str, str] = {}
        self.last_summary   = "Ready"
        self.last_run_stats = {
            "pull": {"seen": 0, "changed": 0, "skipped": 0, "failed": 0},
        }

        db_setting = get_env("NOTION_DB_ID", "ALL").strip()
        self.sync_all = db_setting.upper() == "ALL"
        self.db_ids   = [x.strip() for x in db_setting.split(",")
                         if x.strip() and x.strip().upper() != "ALL"]

        token = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        self.notion: Client | None = Client(auth=token) if token else None

    # ── Internal helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _page_last_edited(page: dict) -> str:
        return str(page.get("last_edited_time") or "").strip()

    @staticmethod
    def _extract_title(item: dict, fallback: str = "Untitled") -> str:
        raw = (item or {}).get("title") or (item or {}).get("name") or []
        if isinstance(raw, str):
            return raw.strip() or fallback
        if isinstance(raw, list):
            return "".join(p.get("plain_text", "") for p in raw
                           if isinstance(p, dict)).strip() or fallback
        return fallback

    def _notion_headers(self) -> dict:
        token   = get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")
        version = get_env("NOTION_VERSION", "2022-06-28")
        return {
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
            "Notion-Version": version,
        }

    def _retrieve_schema(self, db_id: str) -> dict:
        if not self.notion or not db_id:
            return {}
        base = get_env("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")

        for fn in [
            lambda: self.notion.data_sources.retrieve(data_source_id=db_id),
            lambda: self.notion.databases.retrieve(database_id=db_id),
        ]:
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass

        # Final fallback: raw HTTP GET /databases/{id}
        try:
            resp = self.http.get(f"{base}/databases/{db_id}",
                                  headers=self._notion_headers(), timeout=15)
            resp.raise_for_status()
            result = resp.json()
            if isinstance(result, dict):
                return result
        except Exception as exc:
            logger.warning(f"Could not retrieve schema for {db_id}: {exc}")
        return {}

    def _query_pages(self, db_id: str, schema: dict | None = None) -> list[dict]:
        results: list[dict] = []
        cursor  = None
        base    = get_env("NOTION_API_BASE", "https://api.notion.com/v1").rstrip("/")

        while True:
            args: dict = {"page_size": 100}
            if cursor:
                args["start_cursor"] = cursor

            resp = None

            # Primary path: data_sources.query (works for most databases in this SDK version)
            try:
                r = self.notion.data_sources.query(data_source_id=db_id, **args)
                if isinstance(r, dict):
                    resp = r
            except Exception:
                pass

            # Fallback: raw HTTP POST /databases/{id}/query
            if resp is None:
                try:
                    r = self.http.post(
                        f"{base}/databases/{db_id}/query",
                        headers=self._notion_headers(),
                        json=args,
                        timeout=30,
                    )
                    r.raise_for_status()
                    decoded = r.json()
                    if isinstance(decoded, dict):
                        resp = decoded
                except Exception as exc:
                    logger.warning(f"Could not query database {db_id}: {exc}")
                    break

            if resp is None:
                break

            results.extend(r for r in resp.get("results", []) if isinstance(r, dict))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return results

    def _fetch_blocks(self, page_id: str) -> list[dict]:
        if not self.notion or not page_id:
            return []
        blocks: list[dict] = []
        cursor = None
        try:
            while True:
                kwargs: dict = {"block_id": page_id, "page_size": 100}
                if cursor:
                    kwargs["start_cursor"] = cursor
                resp = self.notion.blocks.children.list(**kwargs)
                blocks.extend(resp.get("results", []))
                if not resp.get("has_more"):
                    break
                cursor = resp.get("next_cursor")
        except Exception as exc:
            logger.warning(f"Could not fetch blocks for {page_id}: {exc}")
        return blocks

    _RICH_TEXT_BLOCKS = frozenset({
        "paragraph", "heading_1", "heading_2", "heading_3",
        "bulleted_list_item", "numbered_list_item", "to_do",
        "toggle", "quote", "callout", "code",
    })

    @classmethod
    def _blocks_to_text(cls, blocks: list[dict]) -> str:
        lines: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "").strip()
            bdata = block.get(btype) or {}
            if not isinstance(bdata, dict):
                continue

            if btype in cls._RICH_TEXT_BLOCKS:
                text = "".join(
                    p.get("plain_text", "") for p in bdata.get("rich_text") or []
                    if isinstance(p, dict)
                ).strip()
                if text:
                    if btype == "heading_1":
                        lines.append(f"# {text}")
                    elif btype == "heading_2":
                        lines.append(f"## {text}")
                    elif btype == "heading_3":
                        lines.append(f"### {text}")
                    elif btype in ("bulleted_list_item", "numbered_list_item"):
                        lines.append(f"- {text}")
                    else:
                        lines.append(text)
            elif btype == "child_page":
                t = str(bdata.get("title") or "").strip()
                if t:
                    lines.append(f"[Page: {t}]")
            elif btype == "divider":
                lines.append("---")
        return "\n".join(lines)

    # ── Media download ────────────────────────────────────────────────────────
    def _media_is_current(self, path: Path, source_updated_at: str) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        if not source_updated_at:
            return True
        try:
            remote_dt = datetime.fromisoformat(source_updated_at.replace("Z", "+00:00"))
            local_dt  = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            return local_dt >= remote_dt
        except Exception:
            return True

    def download_media(self, url: str, filename: str, source_updated_at: str = "") -> str:
        safe = re.sub(r'[<>:"/\\|?*]+', "_", Path(filename).name)
        dest = MEDIA_DIR / safe
        cache_key = f"{safe}|{source_updated_at}"

        cached = self.download_cache.get(cache_key)
        if cached and Path(cached).exists():
            return cached
        if self._media_is_current(dest, source_updated_at):
            self.download_cache[cache_key] = str(dest)
            return str(dest)

        try:
            resp = self.http.get(url, stream=True, timeout=30)
            resp.raise_for_status()
            tmp = dest.parent / f"{dest.name}.part"
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1024):
                    if chunk:
                        f.write(chunk)
            tmp.replace(dest)
            self.download_cache[cache_key] = str(dest)
            return str(dest)
        except Exception as exc:
            logger.error(f"Failed to download media {url}: {exc}")
            return ""

    # ── Page content extraction ───────────────────────────────────────────────
    def extract_content(self, page: dict, source_updated_at: str = "",
                         blocks: list[dict] | None = None) -> tuple[str, str, list[str]]:
        """Returns (title, ai_summary, media_paths)."""
        title       = "Untitled"
        ai_summary  = ""
        media_paths: list[str] = []

        safe_page, _ = normalize_workspace_page_json(page)
        page_id = str(safe_page.get("id") or (page or {}).get("id") or "page").strip() or "page"
        props   = safe_page.get("properties", {}) if isinstance(safe_page.get("properties", {}), dict) else {}

        for key, prop in props.items():
            if not isinstance(prop, dict):
                continue
            ptype = str(prop.get("type") or "").strip().lower()

            if ptype == "title":
                title_items = prop.get("title") or []
                t = "".join(
                    item.get("plain_text", "") for item in title_items
                    if isinstance(item, dict)
                ).strip()
                if t:
                    title = t

            elif ptype == "rich_text":
                texts = "".join(
                    t.get("plain_text", "") for t in prop.get("rich_text") or []
                    if isinstance(t, dict)
                ).strip()
                if texts:
                    ai_summary += f"{key}: {texts}\n"

            elif ptype in {"status", "select", "multi_select", "number", "checkbox",
                            "date", "url", "email", "phone_number", "relation", "people"}:
                val = property_preview_value(prop)
                if val:
                    ai_summary += f"{key}: {val}\n"

            elif ptype == "files":
                for file_item in prop.get("files") or []:
                    if not isinstance(file_item, dict) or file_item.get("type") != "file":
                        continue
                    url = ((file_item.get("file") or {}).get("url") or "").strip()
                    if not url:
                        continue
                    fname = f"{page_id}_{file_item.get('name', 'attachment')}"
                    local = self.download_media(url, fname, source_updated_at)
                    if local:
                        proxy_name = Path(local).name
                        media_paths.append(local)
                        port = get_env("MEDIA_PROXY_PORT", "8080")
                        ai_summary += f"{key} (local): http://localhost:{port}/{proxy_name}\n"

        if blocks:
            block_text = self._blocks_to_text(blocks)
            if block_text:
                sep = "\n\n" if ai_summary.strip() else ""
                ai_summary += f"{sep}Page Content:\n{block_text}\n"

        return title, ai_summary.strip(), media_paths

    # ── Schema description ────────────────────────────────────────────────────
    def _describe_schema(self, schema: dict, db_id: str) -> tuple[str, str]:
        if not isinstance(schema, dict):
            return f"Database {db_id}", ""
        title = self._extract_title(schema, fallback=f"Database {db_id}")
        props = schema.get("properties", {}) or {}
        lines = [f"Database schema for {title}"]
        for name, prop in sorted((props or {}).items(), key=lambda x: x[0].lower())[:25]:
            if not isinstance(prop, dict):
                continue
            ptype = str(prop.get("type") or "unknown").strip()
            opts: list[str] = []
            if ptype in ("status", "select"):
                opts = [str((o or {}).get("name") or "").strip()
                        for o in ((prop.get(ptype) or {}).get("options") or [])
                        if str((o or {}).get("name") or "").strip()]
            elif ptype == "multi_select":
                opts = [str((o or {}).get("name") or "").strip()
                        for o in ((prop.get("multi_select") or {}).get("options") or [])
                        if str((o or {}).get("name") or "").strip()]
            suffix = f" ({', '.join(opts[:6])})" if opts else ""
            lines.append(f"- {name}: {ptype}{suffix}")
        return f"Database schema — {title}", "\n".join(lines)

    # ── Discover databases ────────────────────────────────────────────────────
    def discover_accessible_databases(self) -> list[dict]:
        if not self.notion:
            return []
        results: list[dict] = []
        cursor = None
        while True:
            args: dict = {"filter": {"property": "object", "value": "data_source"}, "page_size": 100}
            if cursor:
                args["start_cursor"] = cursor
            resp = self.notion.search(**args)
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

        seen: set[str] = set()
        databases: list[dict] = []
        for item in results:
            nid = DatabaseManager._normalize_notion_id((item or {}).get("id"))
            if not nid or nid in seen:
                continue
            seen.add(nid)
            databases.append({
                "id":          nid,
                "title":       self._extract_title(item),
                "url":         str((item or {}).get("url") or ""),
                "parent_type": ((item.get("parent") or {}) if isinstance(item, dict) else {}).get("type", ""),
            })
        databases.sort(key=lambda x: x["title"].lower())
        return databases

    def _target_db_ids(self) -> list[str]:
        if self.sync_all:
            return [d["id"] for d in self.discover_accessible_databases()]
        return self.db_ids

    # ── Pull ──────────────────────────────────────────────────────────────────
    def pull_from_notion(self) -> bool:
        if not self.notion:
            self.last_summary = "Pull failed: Notion is not configured."
            logger.error(self.last_summary)
            return False

        target_ids = self._target_db_ids()
        if not target_ids:
            self.last_summary = "Pull failed: no Notion databases configured."
            logger.error(self.last_summary)
            return False

        logger.info(f"Pull starting — {len(target_ids)} database(s)...")
        total_seen = total_changed = total_skipped = total_failed = 0
        failed_dbs: list[str] = []
        _healer_errors: list[tuple[str, str, str, str]] = []  # (db_id, type, msg, tb)

        for db_id in target_ids:
            try:
                schema   = self._retrieve_schema(db_id)
                pages    = self._query_pages(db_id, schema=schema)
                all_ids  = [db_id] + [p.get("id") for p in pages if p.get("id")]
                existing = self.db.get_existing_page_map(all_ids)
                payloads: list[dict] = []

                # Store schema as a page entry
                if schema:
                    title, ai_summary = self._describe_schema(schema, db_id)
                    schema_prep = copy.deepcopy(schema)
                    schema_prep["_business_brain_links"] = DatabaseManager.build_page_link_index(schema_prep)
                    sat  = self._page_last_edited(schema_prep) or str(schema_prep.get("created_time") or "").strip()
                    chsh = self.db.compute_content_hash(title, ai_summary, schema_prep, [])
                    ex   = existing.get(db_id)
                    if self.db.page_requires_update(ex, sat, chsh):
                        payloads.append({
                            "notion_id":         db_id,
                            "title":             title,
                            "ai_summary":        ai_summary,
                            "raw_json":          schema_prep,
                            "media_paths":       [],
                            "content_hash":      chsh,
                            "source_updated_at": sat,
                        })

                total_seen += len(pages)

                for page in pages:
                    page_id = page.get("id")
                    if not page_id:
                        total_failed += 1
                        continue
                    try:
                        sat = self._page_last_edited(page)
                        ex  = existing.get(page_id)

                        # Quick timestamp skip
                        if ex and DatabaseManager._normalize_timestamp(ex.get("source_updated_at")) \
                               == DatabaseManager._normalize_timestamp(sat):
                            total_skipped += 1
                            continue

                        prepared, _ = normalize_workspace_page_json(copy.deepcopy(page))
                        prepared["_business_brain_links"] = DatabaseManager.build_page_link_index(prepared)
                        blocks  = self._fetch_blocks(page_id)
                        title, ai_summary, media_paths = self.extract_content(prepared, sat, blocks)
                        chsh    = self.db.compute_content_hash(title, ai_summary, prepared, media_paths)

                        if not self.db.page_requires_update(ex, sat, chsh):
                            total_skipped += 1
                            continue

                        payloads.append({
                            "notion_id":         page_id,
                            "title":             title,
                            "ai_summary":        ai_summary,
                            "raw_json":          prepared,
                            "media_paths":       media_paths,
                            "content_hash":      chsh,
                            "source_updated_at": sat,
                        })
                    except Exception as exc:
                        total_failed += 1
                        logger.warning(f"Skipped page {page_id}: {exc}")

                changed, _ = self.db.upsert_pages(payloads)
                total_changed += changed
                logger.info(f"DB {db_id}: {changed} changed, {total_skipped} skipped.")

            except Exception as exc:
                failed_dbs.append(db_id)
                logger.error(f"Pull failed for database {db_id}: {exc}")
                _healer_errors.append((
                    db_id,
                    type(exc).__name__,
                    str(exc),
                    _traceback.format_exc(),
                ))

        self.last_run_stats["pull"] = {
            "seen": total_seen, "changed": total_changed,
            "skipped": total_skipped, "failed": len(failed_dbs),
        }

        # Invoke self-healing agent for any new persistent DB-level errors
        if _healer_errors:
            try:
                from src.sync.healer import SyncHealer
                SyncHealer().heal(_healer_errors)
            except Exception as heal_exc:
                logger.warning(f"Healer invocation failed: {heal_exc}")

        if failed_dbs and total_changed == 0 and total_seen == 0:
            self.last_summary = "Pull failed for all databases."
            return False

        fail_note = f" ({len(failed_dbs)} DB errors)" if failed_dbs else ""
        self.last_summary = (
            f"Pulled {total_changed} page(s), skipped {total_skipped}{fail_note}."
        )
        logger.info(f"Pull complete — seen {total_seen}, changed {total_changed}, skipped {total_skipped}.")
        return True

    # ── Sync entry point ──────────────────────────────────────────────────────
    def perform_sync(self) -> bool:
        return self.pull_from_notion()
