import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from src.core.config import BASE_DIR, get_env, get_secret, logger
from src.db.change_tracking import (
    build_page_link_index,
    compute_content_hash,
    normalize_notion_id,
    normalize_workspace_page_json,
    page_requires_update,
)
from src.db.schema_loader import load_schema_sql


class DatabaseManager:
    def __init__(self):
        self.conn       = None
        self.last_error = ""
        self._backend   = get_env("DB_BACKEND", "sqlite").strip().lower()

    # ── Connection ────────────────────────────────────────────────────────────
    def connect(self, initialize: bool = True) -> bool:
        self.last_error = ""
        if self._backend == "sqlite":
            return self._connect_sqlite(initialize=initialize)
        return self._connect_postgres(initialize=initialize)

    def _connect_sqlite(self, initialize: bool = True) -> bool:
        # Already open?
        if self.conn is not None:
            try:
                self.conn.execute("SELECT 1")
                return True
            except Exception:
                self.conn = None

        try:
            db_path_str = get_env("SQLITE_PATH", "").strip()
            db_path = Path(db_path_str) if db_path_str else BASE_DIR / "data" / "notion_mirror.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)

            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self.conn = conn
            logger.info(f"Connected to SQLite database at {db_path}.")

            if initialize:
                self._init_schema()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"SQLite connection failed: {exc}")
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
            return False

    def _connect_postgres(self, initialize: bool = True) -> bool:
        if self.conn and getattr(self.conn, "closed", 1) == 0:
            return True

        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

        try:
            timeout = int(get_env("PG_CONNECT_TIMEOUT", "5"))
            host    = get_env("PG_HOST", "localhost")
            port    = get_env("PG_PORT", "5432")
            user    = get_env("PG_USER", "postgres")
            passwd  = get_secret("PG_PASSWORD", "")
            dbname  = get_env("PG_DBNAME", "notion_mirror")

            # Bootstrap: create the database if it doesn't exist
            boot = psycopg2.connect(
                host=host, port=port, user=user, password=passwd,
                dbname="postgres", connect_timeout=timeout,
            )
            boot.autocommit = True
            with boot.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{dbname}"')
                    logger.info(f"Created database '{dbname}'.")
            boot.close()

            conn = psycopg2.connect(
                host=host, port=port, user=user, password=passwd,
                dbname=dbname, connect_timeout=timeout,
            )
            conn.autocommit = False
            self.conn = conn
            logger.info("Connected to PostgreSQL database.")

            if initialize:
                self._init_schema()
            return True

        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"Database connection failed: {exc}")
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
            return False

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def is_connected(self) -> bool:
        if self.conn is None:
            return False
        try:
            if self._backend == "sqlite":
                self.conn.execute("SELECT 1")
                return True
            else:
                if getattr(self.conn, "closed", 1) != 0:
                    return False
                with self.conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
        except Exception:
            return False

    # ── Schema ────────────────────────────────────────────────────────────────
    def _init_schema(self):
        sql = load_schema_sql(backend=self._backend)
        if self._backend == "sqlite":
            self.conn.executescript(sql)
        else:
            with self.conn.cursor() as cur:
                cur.execute(sql)
            self.conn.commit()
        logger.info("Database schema validated.")

    # ── SQLite row normalisation ──────────────────────────────────────────────
    def _normalize_sqlite_row(self, row: dict) -> dict:
        """Parse JSON text columns and normalise booleans from a SQLite row."""
        result = dict(row)
        for col in ("raw_json",):
            if col in result and isinstance(result[col], str):
                try:
                    result[col] = json.loads(result[col])
                except (json.JSONDecodeError, TypeError):
                    result[col] = {}
        for col in ("media_local_paths",):
            if col in result and isinstance(result[col], str):
                try:
                    result[col] = json.loads(result[col])
                except (json.JSONDecodeError, TypeError):
                    result[col] = []
        for col in ("keywords",):
            if col in result and isinstance(result[col], str):
                try:
                    result[col] = json.loads(result[col])
                except (json.JSONDecodeError, TypeError):
                    result[col] = []
        for col in ("is_active", "needs_push"):
            if col in result:
                result[col] = bool(result[col])
        return result

    # ── Low-level execute helpers ─────────────────────────────────────────────
    def _execute(self, sql: str, params=None, fetch: str = "none"):
        if self._backend == "sqlite":
            return self._execute_sqlite(sql, params, fetch)
        # PostgreSQL path (unchanged)
        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall() or []
            return None

    def _execute_sqlite(self, sql: str, params=None, fetch: str = "none"):
        # Replace PostgreSQL-style %s placeholders with SQLite ?
        sqlite_sql = sql.replace("%s", "?")
        # Flatten list/tuple params: SQLite doesn't accept list for IN queries here
        # (IN queries with lists are handled per-method via _build_sqlite_in)
        flat_params = tuple(params) if params is not None else ()
        cur = self.conn.execute(sqlite_sql, flat_params)
        if fetch == "one":
            row = cur.fetchone()
            return self._normalize_sqlite_row(dict(row)) if row else None
        if fetch == "all":
            rows = cur.fetchall()
            return [self._normalize_sqlite_row(dict(r)) for r in rows]
        return None

    def _execute_sqlite_raw(self, sql: str, params=(), fetch: str = "all"):
        """Execute raw SQLite SQL (no %s substitution). Used for hand-crafted queries."""
        cur = self.conn.execute(sql, params)
        if fetch == "one":
            row = cur.fetchone()
            return self._normalize_sqlite_row(dict(row)) if row else None
        rows = cur.fetchall()
        return [self._normalize_sqlite_row(dict(r)) for r in rows]

    # ── Write operations ──────────────────────────────────────────────────────
    def upsert_page(self, notion_id: str, title: str, ai_summary: str,
                    raw_json: dict, media_paths: list, content_hash: str,
                    source_updated_at: str, needs_push: bool = False) -> bool:
        nid = normalize_notion_id(notion_id) or notion_id
        try:
            if self._backend == "sqlite":
                self.conn.execute(
                    """
                    INSERT INTO workspace_mirror
                        (notion_id, title, ai_summary, raw_json, media_local_paths,
                         content_hash, source_updated_at, pulled_at, is_active, needs_push)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 1, ?)
                    ON CONFLICT(notion_id) DO UPDATE SET
                        title             = excluded.title,
                        ai_summary        = excluded.ai_summary,
                        raw_json          = excluded.raw_json,
                        media_local_paths = excluded.media_local_paths,
                        content_hash      = excluded.content_hash,
                        source_updated_at = excluded.source_updated_at,
                        pulled_at         = datetime('now'),
                        needs_push        = excluded.needs_push
                    """,
                    (nid, title, ai_summary,
                     json.dumps(raw_json), json.dumps(media_paths),
                     content_hash, source_updated_at,
                     1 if needs_push else 0),
                )
                self.conn.commit()
            else:
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO workspace_mirror
                            (notion_id, title, ai_summary, raw_json, media_local_paths,
                             content_hash, source_updated_at, pulled_at, is_active, needs_push)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, now(), TRUE, %s)
                        ON CONFLICT (notion_id) WHERE is_active = TRUE DO UPDATE SET
                            title             = EXCLUDED.title,
                            ai_summary        = EXCLUDED.ai_summary,
                            raw_json          = EXCLUDED.raw_json,
                            media_local_paths = EXCLUDED.media_local_paths,
                            content_hash      = EXCLUDED.content_hash,
                            source_updated_at = EXCLUDED.source_updated_at,
                            pulled_at         = now(),
                            needs_push        = EXCLUDED.needs_push
                        """,
                        (nid, title, ai_summary, Json(raw_json), Json(media_paths),
                         content_hash, source_updated_at, needs_push),
                    )
                self.conn.commit()
            return True
        except Exception as exc:
            if self._backend != "sqlite":
                self.conn.rollback()
            logger.error(f"upsert_page failed for {nid}: {exc}")
            return False

    def upsert_pages(self, payloads: list[dict]) -> tuple[int, int]:
        """Bulk upsert. Returns (changed_count, skipped_count)."""
        if not payloads:
            return 0, 0
        changed = 0
        try:
            if self._backend == "sqlite":
                for p in payloads:
                    nid = normalize_notion_id(p.get("notion_id", "")) or p.get("notion_id", "")
                    cur = self.conn.execute(
                        """
                        INSERT INTO workspace_mirror
                            (notion_id, title, ai_summary, raw_json, media_local_paths,
                             content_hash, source_updated_at, pulled_at, is_active, needs_push)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 1, 0)
                        ON CONFLICT(notion_id) DO UPDATE SET
                            title             = excluded.title,
                            ai_summary        = excluded.ai_summary,
                            raw_json          = excluded.raw_json,
                            media_local_paths = excluded.media_local_paths,
                            content_hash      = excluded.content_hash,
                            source_updated_at = excluded.source_updated_at,
                            pulled_at         = datetime('now'),
                            needs_push        = 0
                        """,
                        (
                            nid,
                            p.get("title", ""),
                            p.get("ai_summary", ""),
                            json.dumps(p.get("raw_json") or {}),
                            json.dumps(p.get("media_paths") or []),
                            p.get("content_hash", ""),
                            p.get("source_updated_at"),
                        ),
                    )
                    changed += cur.rowcount
                self.conn.commit()
            else:
                with self.conn.cursor() as cur:
                    for p in payloads:
                        nid = normalize_notion_id(p.get("notion_id", "")) or p.get("notion_id", "")
                        cur.execute(
                            """
                            INSERT INTO workspace_mirror
                                (notion_id, title, ai_summary, raw_json, media_local_paths,
                                 content_hash, source_updated_at, pulled_at, is_active, needs_push)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), TRUE, FALSE)
                            ON CONFLICT (notion_id) WHERE is_active = TRUE DO UPDATE SET
                                title             = EXCLUDED.title,
                                ai_summary        = EXCLUDED.ai_summary,
                                raw_json          = EXCLUDED.raw_json,
                                media_local_paths = EXCLUDED.media_local_paths,
                                content_hash      = EXCLUDED.content_hash,
                                source_updated_at = EXCLUDED.source_updated_at,
                                pulled_at         = now(),
                                needs_push        = FALSE
                            """,
                            (
                                nid,
                                p.get("title", ""),
                                p.get("ai_summary", ""),
                                Json(p.get("raw_json") or {}),
                                Json(p.get("media_paths") or []),
                                p.get("content_hash", ""),
                                p.get("source_updated_at"),
                            ),
                        )
                        changed += cur.rowcount
                self.conn.commit()
        except Exception as exc:
            if self._backend != "sqlite":
                self.conn.rollback()
            logger.error(f"upsert_pages batch failed: {exc}")
        return changed, 0

    def deactivate_page(self, notion_id: str) -> bool:
        try:
            if self._backend == "sqlite":
                self.conn.execute(
                    "UPDATE workspace_mirror SET is_active = 0 WHERE notion_id = ?",
                    (notion_id,),
                )
                self.conn.commit()
            else:
                self._execute(
                    "UPDATE workspace_mirror SET is_active = FALSE WHERE notion_id = %s",
                    (notion_id,),
                )
                self.conn.commit()
            return True
        except Exception as exc:
            if self._backend != "sqlite":
                self.conn.rollback()
            logger.error(f"deactivate_page failed for {notion_id}: {exc}")
            return False

    # ── Read operations ───────────────────────────────────────────────────────
    def get_page(self, notion_id: str) -> dict | None:
        if self._backend == "sqlite":
            row = self._execute_sqlite(
                "SELECT * FROM workspace_mirror WHERE notion_id = ? AND is_active = 1",
                (notion_id,), fetch="one",
            )
        else:
            row = self._execute(
                "SELECT * FROM workspace_mirror WHERE notion_id = %s AND is_active = TRUE",
                (notion_id,), fetch="one",
            )
        return dict(row) if row else None

    def get_existing_record(self, notion_id: str) -> dict | None:
        if self._backend == "sqlite":
            row = self._execute_sqlite(
                """
                SELECT notion_id, content_hash, source_updated_at
                  FROM workspace_mirror
                 WHERE notion_id = ? AND is_active = 1
                """,
                (notion_id,), fetch="one",
            )
        else:
            row = self._execute(
                """
                SELECT notion_id, content_hash, source_updated_at
                  FROM workspace_mirror
                 WHERE notion_id = %s AND is_active = TRUE
                """,
                (notion_id,), fetch="one",
            )
        return dict(row) if row else None

    def get_existing_page_map(self, notion_ids: list[str]) -> dict[str, dict]:
        if not notion_ids:
            return {}
        if self._backend == "sqlite":
            placeholders = ",".join("?" * len(notion_ids))
            rows = self._execute_sqlite_raw(
                f"""
                SELECT notion_id, content_hash, source_updated_at
                  FROM workspace_mirror
                 WHERE notion_id IN ({placeholders}) AND is_active = 1
                """,
                tuple(notion_ids),
            )
        else:
            rows = self._execute(
                """
                SELECT notion_id, content_hash, source_updated_at
                  FROM workspace_mirror
                 WHERE notion_id = ANY(%s) AND is_active = TRUE
                """,
                (notion_ids,), fetch="all",
            )
        return {row["notion_id"]: dict(row) for row in (rows or [])}

    def get_active_pages(self) -> list[dict]:
        if self._backend == "sqlite":
            rows = self._execute_sqlite(
                "SELECT * FROM workspace_mirror WHERE is_active = 1 ORDER BY updated_at DESC",
                fetch="all",
            )
        else:
            rows = self._execute(
                "SELECT * FROM workspace_mirror WHERE is_active = TRUE ORDER BY updated_at DESC",
                fetch="all",
            )
        return [dict(r) for r in (rows or [])]

    def get_stats(self) -> dict:
        if self._backend == "sqlite":
            row = self._execute_sqlite_raw(
                """
                SELECT
                    COUNT(*)                                                                        AS active_pages,
                    SUM(CASE WHEN json_array_length(COALESCE(media_local_paths,'[]')) > 0
                             THEN 1 ELSE 0 END)                                                    AS pages_with_media,
                    MAX(pulled_at)                                                                  AS last_pull
                FROM workspace_mirror WHERE is_active = 1
                """,
                fetch="one",
            )
        else:
            row = self._execute(
                """
                SELECT
                    COUNT(*)                                                              AS active_pages,
                    SUM(CASE WHEN jsonb_array_length(COALESCE(media_local_paths,'[]'::jsonb)) > 0
                             THEN 1 ELSE 0 END)                                          AS pages_with_media,
                    MAX(pulled_at)                                                        AS last_pull
                FROM workspace_mirror WHERE is_active = TRUE
                """,
                fetch="one",
            )
        return dict(row) if row else {}

    def get_last_pull_time(self) -> datetime | None:
        if self._backend == "sqlite":
            row = self._execute_sqlite(
                "SELECT MAX(pulled_at) AS t FROM workspace_mirror WHERE is_active = 1",
                fetch="one",
            )
        else:
            row = self._execute(
                "SELECT MAX(pulled_at) AS t FROM workspace_mirror WHERE is_active = TRUE",
                fetch="one",
            )
        return row["t"] if row and row["t"] else None

    # ── Search ────────────────────────────────────────────────────────────────
    def search_pages(self, query: str, limit: int = 10) -> list[dict]:
        if self._backend == "sqlite":
            return self._search_pages_sqlite(query, limit)
        rows = self._execute(
            "SELECT * FROM search_workspace(%s, %s)",
            (query, limit), fetch="all",
        )
        return [dict(r) for r in (rows or [])]

    def _search_pages_sqlite(self, query: str, limit: int) -> list[dict]:
        q = query.lower()
        rows = self._execute_sqlite_raw(
            """
            SELECT pi.notion_id, pi.title, pi.summary AS summary_preview,
                   pi.keywords, pi.page_type, pi.content_preview,
                   wm.updated_at, json_extract(wm.raw_json, '$.url') AS user_notion_url,
                   0 AS has_media, 1.0 AS match_rank
              FROM page_index pi
              JOIN workspace_mirror wm ON wm.notion_id = pi.notion_id AND wm.is_active = 1
             WHERE LOWER(pi.title) LIKE ?
                OR LOWER(pi.summary) LIKE ?
                OR LOWER(pi.content_preview) LIKE ?
                OR LOWER(pi.keywords) LIKE ?
             ORDER BY CASE WHEN LOWER(pi.title) LIKE ? THEN 0 ELSE 1 END
             LIMIT ?
            """,
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", limit),
        )
        return rows

    # ── Claude keyword index ──────────────────────────────────────────────────
    def get_full_index(self) -> list[dict]:
        if self._backend == "sqlite":
            rows = self._execute_sqlite_raw(
                """
                SELECT pi.notion_id, wm.title, pi.summary, pi.keywords,
                       pi.page_type, pi.token_estimate, pi.content_preview,
                       wm.updated_at,
                       COALESCE(json_extract(wm.raw_json, '$.url'), '') AS notion_url
                  FROM page_index pi
                  JOIN workspace_mirror wm ON wm.notion_id = pi.notion_id AND wm.is_active = 1
                 ORDER BY wm.updated_at DESC
                """,
            )
        else:
            rows = self._execute(
                """
                SELECT pi.notion_id, wm.title, pi.summary, pi.keywords,
                       pi.page_type, pi.token_estimate, pi.content_preview,
                       wm.updated_at,
                       COALESCE(wm.raw_json ->> 'url', '') AS notion_url
                  FROM page_index pi
                  JOIN workspace_mirror wm ON wm.notion_id = pi.notion_id AND wm.is_active = TRUE
                 ORDER BY wm.updated_at DESC NULLS LAST
                """,
                fetch="all",
            )
        return [dict(r) for r in (rows or [])]

    def upsert_index_entry(self, notion_id: str, title: str, keywords: list, summary: str,
                            page_type: str, content_preview: str, token_estimate: int):
        if self._backend == "sqlite":
            self.conn.execute(
                """
                INSERT INTO page_index
                    (notion_id, title, keywords, summary, page_type, content_preview, token_estimate, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(notion_id) DO UPDATE SET
                    title           = excluded.title,
                    keywords        = excluded.keywords,
                    summary         = excluded.summary,
                    page_type       = excluded.page_type,
                    content_preview = excluded.content_preview,
                    token_estimate  = excluded.token_estimate,
                    indexed_at      = datetime('now')
                """,
                (notion_id, title, json.dumps(keywords), summary,
                 page_type, content_preview, token_estimate),
            )
            self.conn.commit()
        else:
            self._execute(
                """
                INSERT INTO page_index
                    (notion_id, title, keywords, summary, page_type, content_preview, token_estimate, indexed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (notion_id) DO UPDATE SET
                    title           = EXCLUDED.title,
                    keywords        = EXCLUDED.keywords,
                    summary         = EXCLUDED.summary,
                    page_type       = EXCLUDED.page_type,
                    content_preview = EXCLUDED.content_preview,
                    token_estimate  = EXCLUDED.token_estimate,
                    indexed_at      = now()
                """,
                (notion_id, title, keywords, summary, page_type, content_preview, token_estimate),
            )
            self.conn.commit()

    def clear_stale_index(self, active_notion_ids: list[str]):
        if not active_notion_ids:
            return
        if self._backend == "sqlite":
            placeholders = ",".join("?" * len(active_notion_ids))
            self.conn.execute(
                f"DELETE FROM page_index WHERE notion_id NOT IN ({placeholders})",
                tuple(active_notion_ids),
            )
            self.conn.commit()
        else:
            self._execute(
                "DELETE FROM page_index WHERE notion_id <> ALL(%s)",
                (active_notion_ids,),
            )
            self.conn.commit()

    # ── list_recent_pages (backend-agnostic) ──────────────────────────────────
    def list_recent_pages(self, limit: int = 20) -> list[dict]:
        if self._backend == "sqlite":
            sql = """SELECT wm.notion_id, wm.title, wm.updated_at, wm.source_updated_at,
                            wm.needs_push, pi.page_type, pi.keywords,
                            COALESCE(json_extract(wm.raw_json, '$.url'), '') AS notion_url
                       FROM workspace_mirror wm
                       LEFT JOIN page_index pi ON pi.notion_id = wm.notion_id
                      WHERE wm.is_active = 1
                      ORDER BY wm.updated_at DESC
                      LIMIT ?"""
            rows = self._execute_sqlite_raw(sql, (limit,))
        else:
            sql = """SELECT wm.notion_id, wm.title, wm.updated_at, wm.source_updated_at,
                            wm.needs_push, pi.page_type, pi.keywords,
                            COALESCE(wm.raw_json ->> 'url', '') AS notion_url
                       FROM workspace_mirror wm
                       LEFT JOIN page_index pi ON pi.notion_id = wm.notion_id
                      WHERE wm.is_active = TRUE
                      ORDER BY wm.updated_at DESC NULLS LAST
                      LIMIT %s"""
            rows = self._execute(sql, (limit,), fetch="all")
        return [dict(r) for r in (rows or [])]

    # ── Utilities (used by SyncEngine) ───────────────────────────────────────
    @staticmethod
    def build_page_link_index(page: dict) -> dict:
        return build_page_link_index(page)

    @staticmethod
    def compute_content_hash(title, ai_summary, raw_json, media_paths) -> str:
        return compute_content_hash(title, ai_summary, raw_json, media_paths)

    @staticmethod
    def page_requires_update(existing: dict | None, source_updated_at: str, content_hash: str) -> bool:
        return page_requires_update(existing, source_updated_at, content_hash)

    @staticmethod
    def _normalize_notion_id(value) -> str:
        return normalize_notion_id(value)

    @staticmethod
    def _normalize_timestamp(value) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat().replace("+00:00", "Z")
        return str(value).strip()

    @staticmethod
    def _display_property_value(prop: dict) -> str:
        from src.db.change_tracking import property_preview_value
        return property_preview_value(prop)

    def check_workspace_json_health(self, sample_limit: int = 25, log_results: bool = False) -> dict:
        if self._backend == "sqlite":
            rows = self._execute_sqlite(
                "SELECT notion_id, raw_json FROM workspace_mirror WHERE is_active = 1 LIMIT ?",
                (sample_limit,), fetch="all",
            )
        else:
            rows = self._execute(
                "SELECT notion_id, raw_json FROM workspace_mirror WHERE is_active = TRUE LIMIT %s",
                (sample_limit,), fetch="all",
            )
        issues = 0
        for row in (rows or []):
            _, row_issues = normalize_workspace_page_json(row.get("raw_json") or {})
            if row_issues:
                issues += 1
                if log_results:
                    logger.debug(f"Health check issues for {row['notion_id']}: {row_issues}")
        if log_results and issues:
            logger.warning(f"JSON health check: {issues}/{len(rows or [])} pages had normalisation issues.")
        return {"checked": len(rows or []), "with_issues": issues}
