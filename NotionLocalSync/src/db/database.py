import copy
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from src.core.config import ACTIVITY_LOG_FILE, get_env, get_secret, logger
from src.db.change_tracking import (
    build_bulk_replace_payload,
    build_local_edit_payload,
    build_notion_text_objects,
    build_page_link_index,
    coerce_snapshot_datetime,
    compute_content_hash,
    empty_property_value,
    extract_notion_ids_from_url,
    format_compare_value,
    normalize_notion_id,
    normalize_timestamp,
    normalize_workspace_page_json,
    page_requires_update,
    plain_text_from_fragments,
    property_preview_value,
    property_push_supported,
    sanitize_for_hash,
)
from src.db.schema_loader import load_schema_sql


class DatabaseManager:
    def __init__(self):
        self.conn = None
        self.last_error = ""
        self._stats_cache: dict | None = None
        self._stats_cache_checked_at = 0.0
        self._catalog_stats_cache: dict | None = None
        self._catalog_stats_cache_checked_at = 0.0

    def connect(self, initialize: bool = True, ensure_database: bool = True) -> bool:
        self.last_error = ""
        self.invalidate_runtime_caches()

        # Reuse an existing healthy connection instead of opening a new client slot.
        if self.conn and getattr(self.conn, "closed", 1) == 0:
            return True

        # If the handle exists but is no longer usable, close it before reconnecting.
        if self.conn and getattr(self.conn, "closed", 1) != 0:
            try:
                self.conn.close()
            except Exception:
                pass
            finally:
                self.conn = None

        try:
            connect_timeout = int(get_env("PG_CONNECT_TIMEOUT", "5"))  # seconds
            if ensure_database:
                bootstrap_conn = psycopg2.connect(
                    host=get_env("PG_HOST", "localhost"),
                    port=get_env("PG_PORT", "5432"),
                    user=get_env("PG_USER", "postgres"),
                    password=get_secret("PG_PASSWORD", ""),
                    dbname="postgres",
                    connect_timeout=connect_timeout,
                )
                bootstrap_conn.autocommit = True
                # Keep bootstrap_conn local — never assign it to self.conn.
                # _ensure_database needs a connection, so pass it explicitly.
                self._ensure_database(bootstrap_conn)
                bootstrap_conn.close()

            final_conn = psycopg2.connect(
                host=get_env("PG_HOST", "localhost"),
                port=get_env("PG_PORT", "5432"),
                user=get_env("PG_USER", "postgres"),
                password=get_secret("PG_PASSWORD", ""),
                dbname=get_env("PG_DBNAME", "notion_mirror"),
                connect_timeout=connect_timeout,
            )
            final_conn.autocommit = False

            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass

            # Now assign the fully-ready connection
            self.conn = final_conn
            logger.info("Connected to PostgreSQL database.")
            if initialize:
                self.ensure_app_foundation()
                self.init_schema()
                try:
                    self.check_workspace_json_health(sample_limit=25, log_results=True)
                except Exception as health_exc:
                    logger.warning(f"Workspace JSON health check could not run yet: {health_exc}")
            return True
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Database connection failed: {e}")
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
            finally:
                self.conn = None
        self.invalidate_runtime_caches()

    def invalidate_runtime_caches(self):
        self._stats_cache = None
        self._stats_cache_checked_at = 0.0
        self._catalog_stats_cache = None
        self._catalog_stats_cache_checked_at = 0.0

    @staticmethod
    def _cache_is_fresh(checked_at: float, max_age_seconds: float) -> bool:
        try:
            safe_age = max(0.0, float(max_age_seconds or 0.0))
        except (TypeError, ValueError):
            safe_age = 0.0
        return bool(checked_at) and (time.monotonic() - checked_at) <= safe_age

    def get_cached_stats_snapshot(self) -> dict:
        default = {
            "connected": False,
            "active_pages": 0,
            "pending_push": 0,
            "total_versions": 0,
            "pages_with_media": 0,
            "database_size_text": "Not available yet",
            "last_pull_at": None,
            "last_push_at": None,
            "latest_local_update": None,
            "latest_source_update": None,
        }
        snapshot = {**default, **(self._stats_cache or {})}
        snapshot["connected"] = bool(self.conn) and bool(snapshot.get("connected", True))
        return snapshot

    def get_cached_catalog_stats_snapshot(self) -> dict:
        default = {
            "active_pages": 0,
            "pending_push_pages": 0,
            "pages_with_media": 0,
            "latest_local_update": None,
            "latest_source_update": None,
        }
        return {**default, **(self._catalog_stats_cache or {})}

    def ensure_app_foundation(self):
        if not self.conn:
            return

        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE SCHEMA IF NOT EXISTS app_core;
                CREATE SCHEMA IF NOT EXISTS notion;
                CREATE SCHEMA IF NOT EXISTS n8n;
                CREATE SCHEMA IF NOT EXISTS clickup;
                CREATE SCHEMA IF NOT EXISTS mautic;
                CREATE SCHEMA IF NOT EXISTS recovery;

                CREATE TABLE IF NOT EXISTS app_core.service_modules (
                    service_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    enabled BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS app_core.mcp_servers (
                    server_name TEXT PRIMARY KEY,
                    command_text TEXT DEFAULT '',
                    config_path TEXT DEFAULT '',
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                """
            )
        self.conn.commit()
        self.ensure_service_schema(
            "notion",
            display_name="Notion",
            description="Workspace sync history for Notion runs.",
        )

    def ensure_service_schema(self, service_key: str, display_name: str = "", description: str = "") -> bool:
        if not self.conn:
            return False

        normalized_key = re.sub(r"[^a-z0-9_]+", "_", str(service_key or "service").strip().lower()).strip("_") or "service"
        friendly_name = (display_name or normalized_key.replace("_", " ").title()).strip()

        try:
            with self.conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(normalized_key)))
                cur.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.sync_runs (
                            id BIGSERIAL PRIMARY KEY,
                            run_started_at TIMESTAMPTZ DEFAULT now(),
                            run_finished_at TIMESTAMPTZ,
                            status TEXT DEFAULT 'planned',
                            summary TEXT DEFAULT '',
                            details JSONB DEFAULT '{{}}'::jsonb
                        )
                        """
                    ).format(sql.Identifier(normalized_key))
                )
                cur.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}.records (
                            id BIGSERIAL PRIMARY KEY,
                            external_id TEXT,
                            title TEXT,
                            payload JSONB DEFAULT '{{}}'::jsonb,
                            created_at TIMESTAMPTZ DEFAULT now(),
                            updated_at TIMESTAMPTZ DEFAULT now()
                        )
                        """
                    ).format(sql.Identifier(normalized_key))
                )
                cur.execute(
                    """
                    INSERT INTO app_core.service_modules (service_key, display_name, schema_name, description, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (service_key)
                    DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        schema_name = EXCLUDED.schema_name,
                        description = EXCLUDED.description,
                        updated_at = now()
                    """,
                    (normalized_key, friendly_name, normalized_key, description or ""),
                )
            self.conn.commit()
            return True
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not prepare service schema '{normalized_key}': {exc}")
            return False

    def _ensure_database(self, conn=None):
        """Create the target database if it doesn't exist.
        Accepts an explicit connection so that connect() can pass the
        bootstrap connection without ever assigning it to self.conn."""
        use_conn = conn or self.conn
        dbname = get_env("PG_DBNAME", "notion_mirror")
        with use_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (dbname,))
            exists = cur.fetchone()
            if not exists:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
                logger.info(f"Created database: {dbname}")

    def init_schema(self):
        if not self.conn:
            return

        query = """
        CREATE TABLE IF NOT EXISTS app_core.schema_migrations (
            id BIGSERIAL PRIMARY KEY,
            migration_name TEXT NOT NULL,
            schema_hash TEXT NOT NULL,
            applied_at TIMESTAMPTZ DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS idx_schema_migrations_applied_at
        ON app_core.schema_migrations(applied_at DESC);

        CREATE TABLE IF NOT EXISTS workspace_mirror (
            id BIGSERIAL PRIMARY KEY,
            notion_id TEXT NOT NULL,
            title TEXT,
            ai_summary TEXT,
            media_local_paths JSONB DEFAULT '[]'::jsonb,
            raw_json JSONB DEFAULT '{}'::jsonb,
            content_hash TEXT DEFAULT '',
            source_updated_at TIMESTAMP WITH TIME ZONE,
            pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            last_pushed_at TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            valid_from TIMESTAMP WITH TIME ZONE DEFAULT now(),
            valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z',
            is_active BOOLEAN DEFAULT TRUE,
            needs_push BOOLEAN DEFAULT FALSE
        );

        ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT '';
        ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMP WITH TIME ZONE;
        ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now();
        ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS last_pushed_at TIMESTAMP WITH TIME ZONE;
        ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now();
        ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS needs_push BOOLEAN DEFAULT FALSE;
        ALTER TABLE workspace_mirror ALTER COLUMN media_local_paths SET DEFAULT '[]'::jsonb;
        ALTER TABLE workspace_mirror ALTER COLUMN raw_json SET DEFAULT '{}'::jsonb;

        UPDATE workspace_mirror SET media_local_paths = '[]'::jsonb WHERE media_local_paths IS NULL;
        UPDATE workspace_mirror SET raw_json = '{}'::jsonb WHERE raw_json IS NULL;
        UPDATE workspace_mirror SET content_hash = '' WHERE content_hash IS NULL;
        UPDATE workspace_mirror SET needs_push = FALSE WHERE needs_push IS NULL;
        UPDATE workspace_mirror SET updated_at = COALESCE(updated_at, valid_from, now()) WHERE updated_at IS NULL;
        UPDATE workspace_mirror SET pulled_at = COALESCE(pulled_at, valid_from, now()) WHERE pulled_at IS NULL;

        CREATE INDEX IF NOT EXISTS idx_notion_id_active ON workspace_mirror(notion_id) WHERE is_active = TRUE;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_mirror_single_active ON workspace_mirror(notion_id) WHERE is_active = TRUE;
        CREATE INDEX IF NOT EXISTS idx_workspace_mirror_needs_push ON workspace_mirror(needs_push) WHERE is_active = TRUE;
        CREATE INDEX IF NOT EXISTS idx_workspace_mirror_source_updated_at ON workspace_mirror(source_updated_at);

        CREATE TABLE IF NOT EXISTS activity_log (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(),
            category TEXT NOT NULL DEFAULT 'general',
            action TEXT NOT NULL DEFAULT 'event',
            status TEXT NOT NULL DEFAULT 'info',
            summary TEXT NOT NULL DEFAULT '',
            details JSONB DEFAULT '{}'::jsonb,
            notion_id TEXT,
            backup_path TEXT,
            snapshot_at TIMESTAMPTZ,
            is_checked BOOLEAN DEFAULT FALSE,
            checked_at TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_activity_log_category ON activity_log(category, created_at DESC);

        DROP FUNCTION IF EXISTS count_literal_occurrences(TEXT, TEXT, BOOLEAN);
        CREATE OR REPLACE FUNCTION count_literal_occurrences(input_text TEXT, find_text TEXT, case_sensitive BOOLEAN DEFAULT FALSE)
        RETURNS INT AS $$
        DECLARE
            source_text TEXT := COALESCE(input_text, '');
            needle TEXT := COALESCE(find_text, '');
            search_source TEXT;
            search_needle TEXT;
            offset_value INT := 1;
            found_pos INT;
            hit_count INT := 0;
            needle_len INT;
        BEGIN
            IF needle = '' THEN
                RETURN 0;
            END IF;

            search_source := CASE WHEN case_sensitive THEN source_text ELSE lower(source_text) END;
            search_needle := CASE WHEN case_sensitive THEN needle ELSE lower(needle) END;
            needle_len := char_length(search_needle);

            LOOP
                found_pos := strpos(substr(search_source, offset_value), search_needle);
                EXIT WHEN found_pos = 0;
                hit_count := hit_count + 1;
                offset_value := offset_value + found_pos + needle_len - 1;
            END LOOP;

            RETURN hit_count;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;

        DROP FUNCTION IF EXISTS bulk_replace_literal(TEXT, TEXT, TEXT, BOOLEAN);
        CREATE OR REPLACE FUNCTION bulk_replace_literal(input_text TEXT, find_text TEXT, replace_text TEXT, case_sensitive BOOLEAN DEFAULT FALSE)
        RETURNS TEXT AS $$
        DECLARE
            source_text TEXT := COALESCE(input_text, '');
            needle TEXT := COALESCE(find_text, '');
            replacement_text TEXT := COALESCE(replace_text, '');
            search_source TEXT;
            search_needle TEXT;
            output_text TEXT := '';
            offset_value INT := 1;
            found_pos INT;
            absolute_pos INT;
            needle_len INT;
        BEGIN
            IF needle = '' THEN
                RETURN source_text;
            END IF;

            search_source := CASE WHEN case_sensitive THEN source_text ELSE lower(source_text) END;
            search_needle := CASE WHEN case_sensitive THEN needle ELSE lower(needle) END;
            needle_len := char_length(needle);

            LOOP
                found_pos := strpos(substr(search_source, offset_value), search_needle);
                EXIT WHEN found_pos = 0;
                absolute_pos := offset_value + found_pos - 1;
                output_text := output_text || substr(source_text, offset_value, absolute_pos - offset_value) || replacement_text;
                offset_value := absolute_pos + needle_len;
            END LOOP;

            RETURN output_text || substr(source_text, offset_value);
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;

        DROP FUNCTION IF EXISTS preview_workspace_bulk_replace(TEXT, TEXT, BOOLEAN, INT);
        CREATE OR REPLACE FUNCTION preview_workspace_bulk_replace(
            search_text TEXT,
            field_scope TEXT DEFAULT 'title,ai_summary',
            case_sensitive BOOLEAN DEFAULT FALSE,
            result_limit INT DEFAULT 20
        )
        RETURNS TABLE (
            notion_id TEXT,
            title TEXT,
            title_matches INT,
            summary_matches INT,
            total_matches INT,
            needs_push BOOLEAN,
            updated_at TIMESTAMPTZ
        ) AS $$
        DECLARE
            scope_value TEXT := lower(COALESCE(field_scope, 'title,ai_summary'));
            check_title BOOLEAN := position('title' in scope_value) > 0;
            check_summary BOOLEAN := position('summary' in scope_value) > 0 OR position('note' in scope_value) > 0 OR position('ai_summary' in scope_value) > 0;
        BEGIN
            IF COALESCE(BTRIM(search_text), '') = '' THEN
                RETURN;
            END IF;

            IF NOT check_title AND NOT check_summary THEN
                check_title := TRUE;
                check_summary := TRUE;
            END IF;

            RETURN QUERY
            SELECT *
            FROM (
                SELECT
                    wm.notion_id,
                    COALESCE(NULLIF(BTRIM(wm.title), ''), 'Untitled') AS title,
                    CASE WHEN check_title THEN count_literal_occurrences(wm.title, search_text, case_sensitive) ELSE 0 END AS title_matches,
                    CASE WHEN check_summary THEN count_literal_occurrences(wm.ai_summary, search_text, case_sensitive) ELSE 0 END AS summary_matches,
                    (
                        CASE WHEN check_title THEN count_literal_occurrences(wm.title, search_text, case_sensitive) ELSE 0 END
                        + CASE WHEN check_summary THEN count_literal_occurrences(wm.ai_summary, search_text, case_sensitive) ELSE 0 END
                    )::INT AS total_matches,
                    wm.needs_push,
                    wm.updated_at
                FROM workspace_mirror wm
                WHERE wm.is_active = TRUE
            ) AS matches
            WHERE matches.total_matches > 0
            ORDER BY matches.total_matches DESC, matches.updated_at DESC NULLS LAST
            LIMIT GREATEST(COALESCE(result_limit, 20), 1);
        END;
        $$ LANGUAGE plpgsql STABLE;

        DROP FUNCTION IF EXISTS apply_workspace_bulk_replace(TEXT, TEXT, TEXT, BOOLEAN);
        CREATE OR REPLACE FUNCTION apply_workspace_bulk_replace(
            search_text TEXT,
            replace_text TEXT,
            field_scope TEXT DEFAULT 'title,ai_summary',
            case_sensitive BOOLEAN DEFAULT FALSE
        )
        RETURNS TABLE (
            affected_pages INT,
            total_matches INT,
            changed_titles INT,
            changed_summaries INT,
            message TEXT
        ) AS $$
        DECLARE
            scope_value TEXT := lower(COALESCE(field_scope, 'title,ai_summary'));
            do_title BOOLEAN := position('title' in scope_value) > 0;
            do_summary BOOLEAN := position('summary' in scope_value) > 0 OR position('note' in scope_value) > 0 OR position('ai_summary' in scope_value) > 0;
            row_record RECORD;
            page_count INT := 0;
            match_count INT := 0;
            title_page_count INT := 0;
            summary_page_count INT := 0;
        BEGIN
            IF COALESCE(BTRIM(search_text), '') = '' THEN
                RETURN QUERY SELECT 0::INT, 0::INT, 0::INT, 0::INT, 'Search text is empty, so nothing was changed.'::TEXT;
                RETURN;
            END IF;

            IF NOT do_title AND NOT do_summary THEN
                do_title := TRUE;
                do_summary := TRUE;
            END IF;

            FOR row_record IN
                WITH matches AS (
                    SELECT
                        id,
                        notion_id,
                        CASE WHEN do_title THEN count_literal_occurrences(title, search_text, case_sensitive) ELSE 0 END AS title_hits,
                        CASE WHEN do_summary THEN count_literal_occurrences(ai_summary, search_text, case_sensitive) ELSE 0 END AS summary_hits
                    FROM workspace_mirror
                    WHERE is_active = TRUE
                ),
                updated AS (
                    UPDATE workspace_mirror wm
                    SET title = CASE WHEN matches.title_hits > 0 THEN bulk_replace_literal(wm.title, search_text, replace_text, case_sensitive) ELSE wm.title END,
                        ai_summary = CASE WHEN matches.summary_hits > 0 THEN bulk_replace_literal(wm.ai_summary, search_text, replace_text, case_sensitive) ELSE wm.ai_summary END,
                        needs_push = TRUE,
                        updated_at = now()
                    FROM matches
                    WHERE wm.id = matches.id
                      AND (matches.title_hits + matches.summary_hits) > 0
                    RETURNING matches.notion_id, matches.title_hits, matches.summary_hits
                )
                SELECT * FROM updated
            LOOP
                page_count := page_count + 1;
                match_count := match_count + COALESCE(row_record.title_hits, 0) + COALESCE(row_record.summary_hits, 0);
                IF COALESCE(row_record.title_hits, 0) > 0 THEN
                    title_page_count := title_page_count + 1;
                END IF;
                IF COALESCE(row_record.summary_hits, 0) > 0 THEN
                    summary_page_count := summary_page_count + 1;
                END IF;
            END LOOP;

            RETURN QUERY
            SELECT
                page_count,
                match_count,
                title_page_count,
                summary_page_count,
                CASE
                    WHEN page_count = 0 THEN 'No matching pages were found for that search text.'
                    ELSE 'Queued local bulk replace for ' || page_count || ' page(s) with ' || match_count || ' change(s).'
                END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION workspace_mirror_before_update()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();

            IF OLD.is_active = TRUE
               AND (
                    OLD.title IS DISTINCT FROM NEW.title
                    OR OLD.ai_summary IS DISTINCT FROM NEW.ai_summary
                    OR OLD.media_local_paths IS DISTINCT FROM NEW.media_local_paths
                    OR OLD.raw_json IS DISTINCT FROM NEW.raw_json
               )
               AND COALESCE(current_setting('app.sync_origin', true), '') NOT IN ('notion', 'push') THEN
                NEW.needs_push = TRUE;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_workspace_mirror_before_update ON workspace_mirror;
        CREATE TRIGGER trg_workspace_mirror_before_update
        BEFORE UPDATE ON workspace_mirror
        FOR EACH ROW
        EXECUTE FUNCTION workspace_mirror_before_update();
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(query)
                schema_sql = load_schema_sql()
                if schema_sql.strip():
                    cur.execute(schema_sql)

                schema_hash = hashlib.sha256(f"{query}\n{schema_sql}".encode("utf-8")).hexdigest()
                cur.execute(
                    """
                    INSERT INTO app_core.schema_migrations (migration_name, schema_hash)
                    SELECT %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM app_core.schema_migrations
                        WHERE migration_name = %s AND schema_hash = %s
                    )
                    """,
                    ("embedded_schema", schema_hash, "embedded_schema", schema_hash),
                )
            self.conn.commit()
            logger.info("Database schema validated.")
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Schema initialization failed: {e}")

    @staticmethod
    def _normalize_timestamp(value) -> str:
        if value is None:
            return ""

        if hasattr(value, "isoformat"):
            dt_value = value
        else:
            text = str(value).strip()
            if not text:
                return ""
            try:
                dt_value = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return text

        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        else:
            dt_value = dt_value.astimezone(timezone.utc)

        normalized = dt_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
        normalized = re.sub(r"\.0+Z$", "Z", normalized)
        normalized = re.sub(r"(\.\d*?[1-9])0+Z$", r"\1Z", normalized)
        return normalized

    @classmethod
    def _sanitize_for_hash(cls, value):
        volatile_keys = {"url", "expiry_time", "request_id", "public_url", "download_url"}

        if isinstance(value, dict):
            return {
                key: cls._sanitize_for_hash(item)
                for key, item in value.items()
                if key not in volatile_keys
            }
        if isinstance(value, list):
            return [cls._sanitize_for_hash(item) for item in value]
        return value

    @classmethod
    def compute_content_hash(cls, title: str, ai_summary: str, raw_json: dict, media_paths: list[str]) -> str:
        payload = {
            "title": (title or "").strip(),
            "ai_summary": (ai_summary or "").strip(),
            "raw_json": cls._sanitize_for_hash(raw_json or {}),
            "media_local_paths": sorted(str(path) for path in (media_paths or [])),
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def page_requires_update(cls, existing_record: dict | None, source_updated_at: str, content_hash: str) -> bool:
        if not existing_record:
            return True

        existing_ts = cls._normalize_timestamp(existing_record.get("source_updated_at"))
        new_ts = cls._normalize_timestamp(source_updated_at)
        existing_hash = (existing_record.get("content_hash") or "").strip()
        new_hash = (content_hash or "").strip()

        if existing_record.get("needs_push") and existing_ts == new_ts:
            return False

        if existing_hash and new_hash and existing_hash == new_hash:
            return False

        return existing_ts != new_ts or existing_hash != new_hash

    @staticmethod
    def _build_notion_text_objects(text: str) -> list[dict]:
        clean_text = (text or "").strip()
        if not clean_text:
            return []
        return [{"type": "text", "text": {"content": clean_text}, "plain_text": clean_text}]

    @staticmethod
    def _normalize_bulk_replace_fields(fields: list[str] | None = None) -> list[str]:
        requested_fields = []
        for field in (fields or ["title", "ai_summary"]):
            field_name = str(field or "").strip().lower()
            if field_name in {"title", "titles"}:
                normalized = "title"
            elif field_name in {"ai_summary", "summary", "summaries", "notes", "note"}:
                normalized = "ai_summary"
            else:
                continue
            if normalized not in requested_fields:
                requested_fields.append(normalized)
        return requested_fields or ["title", "ai_summary"]

    @staticmethod
    def _replace_literal_text(value: str | None, search_text: str, replace_text: str, case_sensitive: bool = False) -> tuple[str, int]:
        source_text = str(value or "")
        target_text = str(search_text or "")
        replacement_text = str(replace_text or "")

        if not target_text:
            return source_text, 0

        if case_sensitive:
            match_count = source_text.count(target_text)
            return source_text.replace(target_text, replacement_text), match_count

        pattern = re.compile(re.escape(target_text), re.IGNORECASE)
        matches = pattern.findall(source_text)
        return pattern.sub(lambda _: replacement_text, source_text), len(matches)

    @staticmethod
    def _normalize_notion_id(value: str | None) -> str:
        text = str(value or "").strip()
        hex_only = re.sub(r"[^0-9a-fA-F]", "", text)
        if len(hex_only) != 32:
            return text
        return (
            f"{hex_only[0:8]}-{hex_only[8:12]}-{hex_only[12:16]}-"
            f"{hex_only[16:20]}-{hex_only[20:32]}"
        ).lower()

    @classmethod
    def _extract_notion_id_from_url(cls, value: str | None) -> str:
        text = str(value or "").strip()
        if "notion.so" not in text.lower():
            return ""

        matches = re.findall(
            r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
            text,
        )
        return cls._normalize_notion_id(matches[-1]) if matches else ""

    @classmethod
    def build_page_link_index(cls, page: dict) -> dict:
        source_page = copy.deepcopy(page or {})
        page_id = cls._normalize_notion_id(source_page.get("id"))
        user_notion_url = str(source_page.get("url") or "").strip()
        object_type = str(source_page.get("object") or "").strip().lower()
        resource_kind = "database" if object_type in {"database", "data_source"} else "page"

        linked_notion_ids: list[str] = []
        linked_resources: list[dict] = []
        linked_database_ids: list[str] = []
        linked_database_resources: list[dict] = []
        seen_page_ids: set[str] = set()
        seen_database_ids: set[str] = set()

        def add_page_reference(notion_id: str | None, source: str, url: str = ""):
            normalized_id = cls._normalize_notion_id(notion_id)
            if not normalized_id or normalized_id == page_id or normalized_id in seen_page_ids:
                return

            seen_page_ids.add(normalized_id)
            linked_notion_ids.append(normalized_id)
            resource = {
                "notion_id": normalized_id,
                "internal_resource_uri": f"bb://page/{normalized_id}",
                "source": source or "page_reference",
            }
            if url:
                resource["user_notion_url"] = str(url)
            linked_resources.append(resource)

        def add_database_reference(notion_id: str | None, source: str, url: str = ""):
            normalized_id = cls._normalize_notion_id(notion_id)
            if not normalized_id or normalized_id in seen_database_ids:
                return

            seen_database_ids.add(normalized_id)
            linked_database_ids.append(normalized_id)
            resource = {
                "notion_id": normalized_id,
                "internal_resource_uri": f"bb://database/{normalized_id}",
                "source": source or "database_reference",
            }
            if url:
                resource["user_notion_url"] = str(url)
            linked_database_resources.append(resource)

        def walk(value, source: str = "page_reference"):
            if isinstance(value, dict):
                mention = value.get("mention")
                if isinstance(mention, dict):
                    mention_type = str(mention.get("type") or "").strip().lower()
                    if mention_type == "page":
                        add_page_reference((mention.get("page") or {}).get("id"), "mention")
                    elif mention_type in {"database", "data_source"}:
                        add_database_reference((mention.get(mention_type) or {}).get("id"), "database_mention")

                relation_items = value.get("relation")
                if isinstance(relation_items, list):
                    for item in relation_items:
                        add_page_reference((item or {}).get("id"), "relation")

                if "data_source_id" in value:
                    add_database_reference(value.get("data_source_id"), source or "database_reference")
                if "database_id" in value:
                    add_database_reference(value.get("database_id"), source or "database_reference")

                for key in ("href", "url"):
                    url_value = value.get(key)
                    url_notion_id = cls._extract_notion_id_from_url(url_value)
                    if not url_notion_id:
                        continue
                    if source == "database_reference":
                        add_database_reference(url_notion_id, source or "database_reference", str(url_value or ""))
                    else:
                        add_page_reference(url_notion_id, source or "url", str(url_value or ""))

                for key, item in value.items():
                    child_source = source
                    if key == "relation":
                        child_source = "relation"
                    elif key == "mention":
                        child_source = "mention"
                    elif key in {"data_source_id", "database_id"}:
                        child_source = "database_reference"
                    elif key in {"href", "url"}:
                        child_source = "url"
                    walk(item, child_source)
                return

            if isinstance(value, list):
                for item in value:
                    walk(item, source)
                return

            if isinstance(value, str):
                url_notion_id = cls._extract_notion_id_from_url(value)
                if not url_notion_id:
                    return
                if source == "database_reference":
                    add_database_reference(url_notion_id, source or "database_reference", value)
                else:
                    add_page_reference(url_notion_id, source or "text_link", value)

        walk(source_page)
        return {
            "internal_resource_uri": f"bb://{resource_kind}/{page_id}" if page_id else "",
            "user_notion_url": user_notion_url,
            "linked_notion_ids": linked_notion_ids,
            "linked_resources": linked_resources,
            "linked_database_ids": linked_database_ids,
            "linked_database_resources": linked_database_resources,
        }

    @classmethod
    def _display_property_value(cls, prop: dict | None) -> str:
        return property_preview_value(prop)

    @classmethod
    def build_local_edit_payload(cls, existing_record: dict, title: str | None = None, ai_summary: str | None = None) -> dict:
        return build_local_edit_payload(existing_record, title=title, ai_summary=ai_summary)

    @classmethod
    def build_bulk_replace_payload(
        cls,
        existing_record: dict,
        find_text: str,
        replace_text: str,
        fields: list[str] | None = None,
        case_sensitive: bool = False,
    ) -> dict | None:
        if not existing_record:
            raise ValueError("Existing record is required for bulk search and replace.")

        search_text = str(find_text or "").strip()
        if not search_text:
            raise ValueError("Search text is required before running bulk search and replace.")

        selected_fields = cls._normalize_bulk_replace_fields(fields)
        counts = {"title": 0, "ai_summary": 0}
        updated_title = existing_record.get("title") or "Untitled"
        updated_summary = existing_record.get("ai_summary") or ""

        if "title" in selected_fields:
            updated_title, counts["title"] = cls._replace_literal_text(
                updated_title,
                search_text,
                replace_text,
                case_sensitive=case_sensitive,
            )

        if "ai_summary" in selected_fields:
            updated_summary, counts["ai_summary"] = cls._replace_literal_text(
                updated_summary,
                search_text,
                replace_text,
                case_sensitive=case_sensitive,
            )

        changed_fields = [field_name for field_name, hit_count in counts.items() if hit_count > 0]
        if not changed_fields:
            return None

        payload = cls.build_local_edit_payload(
            existing_record,
            title=updated_title if "title" in selected_fields else None,
            ai_summary=updated_summary if "ai_summary" in selected_fields else None,
        )
        payload["match_count"] = counts["title"] + counts["ai_summary"]
        payload["changed_fields"] = changed_fields
        payload["counts"] = counts
        return payload

    @classmethod
    def build_time_machine_diff(cls, snapshot: dict, current: dict) -> list[dict]:
        snapshot_record = snapshot or {}
        current_record = current or {}
        changes: list[dict] = []
        push_supported_types = {
            "title",
            "rich_text",
            "status",
            "select",
            "multi_select",
            "number",
            "checkbox",
            "date",
            "url",
            "email",
            "phone_number",
            "relation",
        }

        def add_change(field_key: str, label: str, current_value, snapshot_value, push_supported: bool):
            if current_value == snapshot_value:
                return

            if current_value in (None, "", [], {}):
                status = "Only in snapshot"
            elif snapshot_value in (None, "", [], {}):
                status = "Only in current"
            else:
                status = "Changed"

            changes.append(
                {
                    "field_key": field_key,
                    "label": label,
                    "status": status,
                    "push_supported": push_supported,
                    "current_value": current_value,
                    "snapshot_value": snapshot_value,
                }
            )

        add_change(
            "title",
            "Title",
            (current_record.get("title") or "Untitled").strip() or "Untitled",
            (snapshot_record.get("title") or "Untitled").strip() or "Untitled",
            True,
        )
        add_change(
            "ai_summary",
            "Summary",
            str(current_record.get("ai_summary") or ""),
            str(snapshot_record.get("ai_summary") or ""),
            True,
        )
        add_change(
            "media_local_paths",
            "Saved media files",
            list(current_record.get("media_local_paths") or []),
            list(snapshot_record.get("media_local_paths") or []),
            False,
        )

        current_raw_json, _ = normalize_workspace_page_json(current_record.get("raw_json") or {})
        snapshot_raw_json, _ = normalize_workspace_page_json(snapshot_record.get("raw_json") or {})
        current_props = current_raw_json.get("properties", {}) if isinstance(current_raw_json.get("properties"), dict) else {}
        snapshot_props = snapshot_raw_json.get("properties", {}) if isinstance(snapshot_raw_json.get("properties"), dict) else {}

        for property_name in sorted(set(current_props) | set(snapshot_props)):
            current_prop = current_props.get(property_name)
            snapshot_prop = snapshot_props.get(property_name)
            if cls._sanitize_for_hash(current_prop) == cls._sanitize_for_hash(snapshot_prop):
                continue

            prop_type = str((snapshot_prop or current_prop or {}).get("type") or "").strip().lower()
            add_change(
                f"property::{property_name}",
                f"Property — {property_name}",
                cls._display_property_value(current_prop),
                cls._display_property_value(snapshot_prop),
                prop_type in push_supported_types,
            )

        return changes

    @classmethod
    def build_restore_payload(
        cls,
        current_record: dict,
        snapshot_record: dict,
        selected_fields: list[str],
        queue_for_push: bool = True,
    ) -> dict:
        if not current_record or not snapshot_record:
            raise ValueError("Both the current record and the snapshot record are required for a restore.")

        selected = {str(field or "").strip() for field in (selected_fields or []) if str(field or "").strip()}
        updated_title = snapshot_record.get("title") if "title" in selected else current_record.get("title")
        updated_summary = snapshot_record.get("ai_summary") if "ai_summary" in selected else current_record.get("ai_summary")

        payload = cls.build_local_edit_payload(
            current_record,
            title=updated_title,
            ai_summary=updated_summary,
        )
        payload["raw_json"], _ = normalize_workspace_page_json(
            payload.get("raw_json") or current_record.get("raw_json") or {}
        )
        payload["media_local_paths"] = copy.deepcopy(current_record.get("media_local_paths") or [])

        if "media_local_paths" in selected or "media" in selected:
            payload["media_local_paths"] = copy.deepcopy(snapshot_record.get("media_local_paths") or [])

        current_props = payload["raw_json"].setdefault("properties", {}) if isinstance(payload.get("raw_json"), dict) else {}
        snapshot_raw_json, _ = normalize_workspace_page_json(snapshot_record.get("raw_json") or {})
        snapshot_props = copy.deepcopy(snapshot_raw_json.get("properties") or {})

        for field_key in selected:
            if not field_key.startswith("property::"):
                continue

            property_name = field_key.split("::", 1)[1].strip()
            if not property_name:
                continue

            if property_name in snapshot_props:
                current_props[property_name] = copy.deepcopy(snapshot_props[property_name])
            else:
                current_props.pop(property_name, None)

        payload["needs_push"] = bool(queue_for_push)
        payload["content_hash"] = cls.compute_content_hash(
            payload.get("title", "Untitled"),
            payload.get("ai_summary", ""),
            payload.get("raw_json") or {},
            payload.get("media_local_paths") or [],
        )
        return payload

    def log_activity(
        self,
        category: str,
        action: str,
        summary: str,
        status: str = "info",
        details: dict | None = None,
        notion_id: str | None = None,
        backup_path: str | None = None,
        snapshot_at=None,
    ) -> dict:
        details_payload = details if isinstance(details, dict) else {"value": details} if details is not None else {}
        entry = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "category": str(category or "general").strip() or "general",
            "action": str(action or "event").strip() or "event",
            "status": str(status or "info").strip() or "info",
            "summary": str(summary or "Activity recorded.").strip() or "Activity recorded.",
            "details": details_payload,
            "notion_id": str(notion_id or "").strip() or None,
            "backup_path": str(backup_path or "").strip() or None,
            "snapshot_at": self._normalize_timestamp(snapshot_at) if snapshot_at else None,
            "is_checked": False,
        }

        try:
            ACTIVITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            logger.warning(f"Could not write the activity log file: {exc}")

        if not self.conn:
            return entry

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO activity_log (
                        category, action, status, summary, details, notion_id, backup_path, snapshot_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at, is_checked
                    """,
                    (
                        entry["category"],
                        entry["action"],
                        entry["status"],
                        entry["summary"],
                        Json(entry["details"]),
                        entry["notion_id"],
                        entry["backup_path"],
                        snapshot_at,
                    ),
                )
                stored = cur.fetchone()
            self.conn.commit()
            if stored:
                entry["id"] = stored.get("id")
                entry["created_at"] = self._normalize_timestamp(stored.get("created_at")) or entry["created_at"]
                entry["is_checked"] = bool(stored.get("is_checked"))
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.warning(f"Could not write the database activity log entry: {exc}")

        return entry

    def get_activity_log(self, limit: int = 200) -> list[dict]:
        try:
            safe_limit = max(1, min(int(limit or 200), 500))
        except (TypeError, ValueError):
            safe_limit = 200

        if self.conn:
            try:
                with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT id, created_at, category, action, status, summary, details,
                               notion_id, backup_path, snapshot_at, is_checked, checked_at
                        FROM activity_log
                        ORDER BY created_at DESC, id DESC
                        LIMIT %s
                        """,
                        (safe_limit,),
                    )
                    entries = [dict(row) for row in cur.fetchall()]
                for entry in entries:
                    entry["created_at"] = self._normalize_timestamp(entry.get("created_at"))
                    entry["snapshot_at"] = self._normalize_timestamp(entry.get("snapshot_at"))
                    entry["checked_at"] = self._normalize_timestamp(entry.get("checked_at"))
                    entry["action_label"] = str(entry.get("action") or "event").replace("_", " ").strip().title()
                return entries
            except Exception as exc:
                logger.warning(f"Could not read the database activity log yet: {exc}")

        entries: list[dict] = []
        if not ACTIVITY_LOG_FILE.exists():
            return entries

        try:
            with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-safe_limit:]
            for line in reversed(lines):
                text = line.strip()
                if not text:
                    continue
                row = json.loads(text)
                row["action_label"] = str(row.get("action") or "event").replace("_", " ").strip().title()
                entries.append(row)
        except Exception as exc:
            logger.warning(f"Could not read the activity log file: {exc}")

        return entries

    def set_activity_checked(self, entry_id: int, checked: bool = True) -> bool:
        if not self.conn:
            return False

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE activity_log
                    SET is_checked = %s,
                        checked_at = CASE WHEN %s THEN now() ELSE NULL END
                    WHERE id = %s
                    """,
                    (bool(checked), bool(checked), int(entry_id)),
                )
                updated = cur.rowcount > 0
            self.conn.commit()
            return updated
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.warning(f"Could not update the activity log checkbox state: {exc}")
            return False

    def get_time_machine_comparison(self, snapshot_at) -> list[dict]:
        if not self.conn:
            return []

        try:
            snapshot_dt = coerce_snapshot_datetime(snapshot_at)
        except ValueError:
            return []

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT notion_id, title, ai_summary, raw_json, media_local_paths,
                           content_hash, source_updated_at, pulled_at, last_pushed_at,
                           updated_at, valid_from, valid_to, is_active, needs_push
                    FROM workspace_mirror
                    WHERE is_active = TRUE
                    """
                )
                current_rows = {row["notion_id"]: dict(row) for row in cur.fetchall()}

                cur.execute(
                    """
                    SELECT notion_id, title, ai_summary, raw_json, media_local_paths,
                           content_hash, source_updated_at, pulled_at, last_pushed_at,
                           updated_at, valid_from, valid_to, is_active, needs_push
                    FROM workspace_mirror
                    WHERE valid_from <= %s AND valid_to > %s
                    ORDER BY notion_id ASC, valid_from DESC, id DESC
                    """,
                    (snapshot_dt, snapshot_dt),
                )
                snapshot_rows: dict[str, dict] = {}
                for row in cur.fetchall():
                    notion_id = row.get("notion_id")
                    if notion_id and notion_id not in snapshot_rows:
                        snapshot_rows[notion_id] = dict(row)

            changes: list[dict] = []
            for notion_id, snapshot_record in snapshot_rows.items():
                current_record = current_rows.get(notion_id) or {}
                changed_fields = self.build_time_machine_diff(snapshot_record, current_record)
                if not changed_fields:
                    continue

                if current_record:
                    status = "Changed"
                else:
                    status = "Only in snapshot"

                title = (
                    (current_record.get("title") if current_record else None)
                    or snapshot_record.get("title")
                    or "Untitled"
                )
                changes.append(
                    {
                        "notion_id": notion_id,
                        "title": str(title).strip() or "Untitled",
                        "status": status,
                        "snapshot_at": snapshot_dt.isoformat(),
                        "changed_fields": changed_fields,
                    }
                )

            changes.sort(key=lambda item: (item.get("title", "").lower(), item.get("notion_id", "")))
            return changes
        except Exception as exc:
            logger.error(f"Failed to build the Time Machine comparison: {exc}")
            return []

    def restore_from_time_machine(self, restore_requests: list[dict], push_to_notion: bool = False) -> tuple[bool, str]:
        if not self.conn:
            return False, "Database is not connected yet."

        restored_pages = 0
        issues: list[str] = []
        first_snapshot_at = None

        try:
            with self.conn:
                with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SET LOCAL app.sync_origin = 'restore'")

                    for request in restore_requests or []:
                        notion_id = str((request or {}).get("notion_id") or "").strip()
                        selected_fields = list((request or {}).get("selected_fields") or [])
                        try:
                            snapshot_dt = coerce_snapshot_datetime((request or {}).get("snapshot_at"))
                        except ValueError:
                            snapshot_dt = None
                        if first_snapshot_at is None:
                            first_snapshot_at = snapshot_dt

                        if not notion_id or not snapshot_dt or not selected_fields:
                            issues.append(f"Skipped an incomplete restore request for {notion_id or 'unknown page'}.")
                            continue

                        cur.execute(
                            """
                            SELECT notion_id, title, ai_summary, raw_json, media_local_paths,
                                   content_hash, source_updated_at, pulled_at, last_pushed_at,
                                   updated_at, valid_from, valid_to, is_active, needs_push
                            FROM workspace_mirror
                            WHERE is_active = TRUE AND notion_id = %s
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (notion_id,),
                        )
                        current_row = cur.fetchone()

                        cur.execute(
                            """
                            SELECT notion_id, title, ai_summary, raw_json, media_local_paths,
                                   content_hash, source_updated_at, pulled_at, last_pushed_at,
                                   updated_at, valid_from, valid_to, is_active, needs_push
                            FROM workspace_mirror
                            WHERE notion_id = %s AND valid_from <= %s AND valid_to > %s
                            ORDER BY valid_from DESC, id DESC
                            LIMIT 1
                            """,
                            (notion_id, snapshot_dt, snapshot_dt),
                        )
                        snapshot_row = cur.fetchone()

                        if not snapshot_row:
                            issues.append(f"No historical version was found for {notion_id} at that time.")
                            continue

                        current_record = dict(current_row) if current_row else dict(snapshot_row)
                        snapshot_record = dict(snapshot_row)
                        payload = self.build_restore_payload(
                            current_record,
                            snapshot_record,
                            selected_fields,
                            queue_for_push=push_to_notion,
                        )

                        cur.execute(
                            """
                            UPDATE workspace_mirror
                            SET valid_to = now(), is_active = FALSE
                            WHERE notion_id = %s AND is_active = TRUE
                            """,
                            (notion_id,),
                        )

                        cur.execute(
                            """
                            INSERT INTO workspace_mirror (
                                notion_id, title, ai_summary, raw_json, media_local_paths,
                                content_hash, source_updated_at, pulled_at, updated_at, needs_push
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s)
                            """,
                            (
                                notion_id,
                                payload.get("title", "Untitled"),
                                payload.get("ai_summary", ""),
                                Json(payload.get("raw_json") or {}),
                                Json(payload.get("media_local_paths") or []),
                                payload.get("content_hash") or "",
                                snapshot_record.get("source_updated_at"),
                                bool(payload.get("needs_push", False)),
                            ),
                        )
                        restored_pages += 1

            if restored_pages == 0:
                message = "No selected pages could be restored from that time."
                if issues:
                    message += " " + " ".join(issues[:3])
                self.log_activity(
                    category="restore",
                    action="time_machine_restore",
                    status="warning",
                    summary=message,
                    details={"issues": issues, "push_to_notion": bool(push_to_notion)},
                    snapshot_at=first_snapshot_at,
                )
                return False, message

            issue_note = f" {len(issues)} item(s) still need attention." if issues else ""
            message = f"Restored {restored_pages} page(s) from Time Machine.{issue_note}"
            self.log_activity(
                category="restore",
                action="time_machine_restore",
                status="success" if not issues else "warning",
                summary=message,
                details={"issues": issues, "push_to_notion": bool(push_to_notion), "request_count": len(restore_requests or [])},
                snapshot_at=first_snapshot_at,
            )
            return True, message
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to restore from Time Machine: {exc}")
            message = f"Time Machine restore failed: {exc}"
            self.log_activity(
                category="restore",
                action="time_machine_restore",
                status="error",
                summary=message,
                details={"push_to_notion": bool(push_to_notion)},
                snapshot_at=first_snapshot_at,
            )
            return False, message

    def get_existing_page_map(self, notion_ids: list[str]) -> dict[str, dict]:
        if not self.conn or not notion_ids:
            return {}

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (notion_id)
                           notion_id, content_hash, source_updated_at, needs_push, updated_at, last_pushed_at
                    FROM workspace_mirror
                    WHERE is_active = TRUE AND notion_id = ANY(%s)
                    ORDER BY notion_id, updated_at DESC NULLS LAST, source_updated_at DESC NULLS LAST, id DESC
                    """,
                    (notion_ids,),
                )
                return {row["notion_id"]: dict(row) for row in cur.fetchall()}
        except Exception as e:
            logger.error(f"Failed to fetch existing page metadata: {e}")
            return {}

    def get_active_pages(self, only_needs_push: bool = False) -> list[dict]:
        if not self.conn:
            return []

        query = """
            SELECT notion_id, title, ai_summary, raw_json, media_local_paths, needs_push, source_updated_at, content_hash
            FROM workspace_mirror
            WHERE is_active = TRUE
        """
        params = []

        if only_needs_push:
            query += " AND needs_push = TRUE"

        query += " ORDER BY updated_at DESC NULLS LAST, id DESC"

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch active pages from database: {e}")
            return []

    def list_editable_pages(self, limit: int = 200) -> list[dict]:
        if not self.conn:
            return []

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT notion_id, title, ai_summary, needs_push
                    FROM workspace_mirror
                    WHERE is_active = TRUE
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch editable page list: {e}")
            return []

    def queue_local_edit(self, notion_id: str, title: str | None = None, ai_summary: str | None = None) -> tuple[bool, str]:
        if not self.conn:
            return False, "Database is not connected yet."

        if not notion_id:
            return False, "A page ID is required before queuing a local edit."

        try:
            with self.conn:
                with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT notion_id, title, ai_summary, raw_json, media_local_paths
                        FROM workspace_mirror
                        WHERE is_active = TRUE AND notion_id = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (notion_id,),
                    )
                    record = cur.fetchone()
                    if not record:
                        return False, "That page is not in the local mirror yet. Run a pull first."

                    payload = self.build_local_edit_payload(dict(record), title=title, ai_summary=ai_summary)
                    cur.execute(
                        """
                        UPDATE workspace_mirror
                        SET title = %s,
                            ai_summary = %s,
                            raw_json = %s,
                            media_local_paths = %s,
                            content_hash = %s,
                            needs_push = TRUE,
                            updated_at = now()
                        WHERE is_active = TRUE AND notion_id = %s
                        """,
                        (
                            payload["title"],
                            payload["ai_summary"],
                            Json(payload["raw_json"]),
                            Json(payload["media_local_paths"]),
                            payload["content_hash"],
                            notion_id,
                        ),
                    )

            return True, f"Queued local changes for '{payload['title']}' and marked them for the next push."
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to queue local edit for page {notion_id}: {e}")
            return False, f"Could not queue the local edit: {e}"

    def preview_bulk_replace(
        self,
        search_text: str,
        replace_text: str = "",
        fields: list[str] | None = None,
        case_sensitive: bool = False,
        limit: int = 20,
    ) -> dict:
        selected_fields = self._normalize_bulk_replace_fields(fields)
        find_text = str(search_text or "").strip()

        try:
            safe_limit = max(1, min(int(limit or 20), 100))
        except (TypeError, ValueError):
            safe_limit = 20

        preview = {
            "search_text": find_text,
            "replace_text": str(replace_text or ""),
            "fields": selected_fields,
            "case_sensitive": bool(case_sensitive),
            "affected_pages": 0,
            "total_matches": 0,
            "matches": [],
        }

        if not self.conn or not find_text:
            return preview

        for record in self.get_active_pages():
            try:
                payload = self.build_bulk_replace_payload(
                    record,
                    find_text,
                    replace_text,
                    fields=selected_fields,
                    case_sensitive=case_sensitive,
                )
            except ValueError:
                return preview

            if not payload:
                continue

            preview["affected_pages"] += 1
            preview["total_matches"] += payload.get("match_count", 0)

            if len(preview["matches"]) < safe_limit:
                preview["matches"].append(
                    {
                        "notion_id": record.get("notion_id", ""),
                        "title": (record.get("title") or "Untitled").strip() or "Untitled",
                        "match_count": payload.get("match_count", 0),
                        "changed_fields": payload.get("changed_fields", []),
                        "needs_push": bool(record.get("needs_push")),
                    }
                )

        return preview

    def apply_bulk_replace(
        self,
        search_text: str,
        replace_text: str = "",
        fields: list[str] | None = None,
        case_sensitive: bool = False,
    ) -> tuple[bool, str, dict]:
        if not self.conn:
            return False, "Database is not connected yet.", {}

        find_text = str(search_text or "").strip()
        if not find_text:
            return False, "Type the text you want to find before running the bulk replace.", {}

        selected_fields = self._normalize_bulk_replace_fields(fields)
        updated_pages = 0
        total_matches = 0
        changed_titles = 0
        changed_summaries = 0

        try:
            with self.conn:
                with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                    for record in self.get_active_pages():
                        payload = self.build_bulk_replace_payload(
                            record,
                            find_text,
                            replace_text,
                            fields=selected_fields,
                            case_sensitive=case_sensitive,
                        )
                        if not payload:
                            continue

                        cur.execute(
                            """
                            UPDATE workspace_mirror
                            SET title = %s,
                                ai_summary = %s,
                                raw_json = %s,
                                media_local_paths = %s,
                                content_hash = %s,
                                needs_push = TRUE,
                                updated_at = now()
                            WHERE is_active = TRUE AND notion_id = %s
                            """,
                            (
                                payload["title"],
                                payload["ai_summary"],
                                Json(payload["raw_json"]),
                                Json(payload["media_local_paths"]),
                                payload["content_hash"],
                                record.get("notion_id", ""),
                            ),
                        )
                        updated_pages += 1
                        total_matches += int(payload.get("match_count", 0) or 0)
                        if payload.get("counts", {}).get("title", 0):
                            changed_titles += 1
                        if payload.get("counts", {}).get("ai_summary", 0):
                            changed_summaries += 1

            if updated_pages == 0:
                return True, "No matching pages were found, so nothing needed to change.", {
                    "affected_pages": 0,
                    "total_matches": 0,
                    "changed_titles": 0,
                    "changed_summaries": 0,
                    "fields": selected_fields,
                    "case_sensitive": bool(case_sensitive),
                }

            message = (
                f"Queued bulk replace for {updated_pages} page(s) with {total_matches} change(s). "
                "Use Push local changes when you are ready to send them to Notion."
            )
            logger.info(message)
            return True, message, {
                "affected_pages": updated_pages,
                "total_matches": total_matches,
                "changed_titles": changed_titles,
                "changed_summaries": changed_summaries,
                "fields": selected_fields,
                "case_sensitive": bool(case_sensitive),
            }
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to run bulk search and replace for '{find_text}': {e}")
            return False, f"Could not run the bulk replace: {e}", {}

    def upsert_pages(self, page_payloads: list[dict]) -> tuple[int, int]:
        if not self.conn or not page_payloads:
            return 0, 0

        changed_count = 0
        skipped_count = 0

        try:
            with self.conn:
                with self.conn.cursor() as cur:
                    cur.execute("SET LOCAL app.sync_origin = 'notion'")

                    for payload in page_payloads:
                        notion_id = payload["notion_id"]
                        source_updated_at = payload.get("source_updated_at") or None
                        normalized_raw_json, raw_issues = normalize_workspace_page_json(payload.get("raw_json") or {})
                        media_paths = payload.get("media_paths") or []
                        if not isinstance(media_paths, list):
                            media_paths = []
                        content_hash = payload.get("content_hash") or ""
                        if raw_issues:
                            logger.warning(
                                f"Normalized malformed workspace JSON for {notion_id}: {'; '.join(raw_issues[:4])}"
                            )
                            content_hash = self.compute_content_hash(
                                payload.get("title", "Untitled"),
                                payload.get("ai_summary", ""),
                                normalized_raw_json,
                                media_paths,
                            )

                        cur.execute(
                            """
                            SELECT content_hash, source_updated_at
                            FROM workspace_mirror
                            WHERE notion_id = %s AND is_active = TRUE
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (notion_id,),
                        )
                        existing = cur.fetchone()
                        existing_record = None
                        if existing:
                            existing_record = {
                                "content_hash": existing[0],
                                "source_updated_at": existing[1],
                            }

                        if not self.page_requires_update(existing_record, source_updated_at, content_hash):
                            skipped_count += 1
                            continue

                        cur.execute(
                            """
                            UPDATE workspace_mirror
                            SET valid_to = now(), is_active = FALSE
                            WHERE notion_id = %s AND is_active = TRUE
                            """,
                            (notion_id,),
                        )

                        cur.execute(
                            """
                            INSERT INTO workspace_mirror (
                                notion_id, title, ai_summary, raw_json, media_local_paths,
                                content_hash, source_updated_at, pulled_at, updated_at, needs_push
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s)
                            """,
                            (
                                notion_id,
                                payload.get("title", "Untitled"),
                                payload.get("ai_summary", ""),
                                Json(normalized_raw_json),
                                Json(media_paths),
                                content_hash,
                                source_updated_at,
                                bool(payload.get("needs_push", False)),
                            ),
                        )
                        changed_count += 1

            if changed_count or skipped_count:
                logger.info(f"Applied {changed_count} changed page(s) and skipped {skipped_count} unchanged page(s).")
            return changed_count, skipped_count
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to batch upsert pages: {e}")
            return 0, len(page_payloads)

    def mark_pages_pushed(self, notion_ids: list[str]) -> bool:
        if not self.conn or not notion_ids:
            return False

        try:
            with self.conn:
                with self.conn.cursor() as cur:
                    cur.execute("SET LOCAL app.sync_origin = 'push'")
                    cur.execute(
                        """
                        UPDATE workspace_mirror
                        SET needs_push = FALSE, last_pushed_at = now(), updated_at = now()
                        WHERE is_active = TRUE AND notion_id = ANY(%s)
                        """,
                        (notion_ids,),
                    )
            return True
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to clear pushed-page flags: {e}")
            return False

    def tombstone_missing_pages(self, notion_ids: list[str]) -> bool:
        """Deactivate local rows whose Notion pages no longer exist (deleted or
        archived). Clears needs_push and tombstones the row (is_active = FALSE)
        so the push loop stops retrying them on every cycle. Sets the 'push'
        sync origin so the before-update trigger does not re-arm needs_push."""
        if not self.conn or not notion_ids:
            return False

        try:
            with self.conn:
                with self.conn.cursor() as cur:
                    cur.execute("SET LOCAL app.sync_origin = 'push'")
                    cur.execute(
                        """
                        UPDATE workspace_mirror
                        SET needs_push = FALSE,
                            is_active = FALSE,
                            valid_to = now(),
                            updated_at = now()
                        WHERE is_active = TRUE AND notion_id = ANY(%s)
                        """,
                        (notion_ids,),
                    )
            return True
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to tombstone missing pages: {e}")
            return False

    def run_maintenance(self, retention_days: int = 45, min_versions_to_keep: int = 5) -> tuple[bool, str, dict]:
        if not self.conn:
            return False, "Local database clean-up is not available until PostgreSQL is connected.", {}

        try:
            retention_value = max(1, int(retention_days or 45))
        except (TypeError, ValueError):
            retention_value = 45

        try:
            min_versions = max(1, int(min_versions_to_keep or 5))
        except (TypeError, ValueError):
            min_versions = 5

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_value)
        details = {
            "retention_days": retention_value,
            "min_versions_to_keep": min_versions,
            "deduplicated_versions": 0,
            "pruned_versions": 0,
            "active_pages": 0,
            "total_versions": 0,
        }

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE is_active = TRUE)::INT AS active_pages,
                        COUNT(*)::INT AS total_versions
                    FROM workspace_mirror
                    """
                )
                before = dict(cur.fetchone() or {})
                details["active_pages"] = int(before.get("active_pages", 0) or 0)
                details["total_versions_before"] = int(before.get("total_versions", 0) or 0)

                cur.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            id,
                            ROW_NUMBER() OVER (
                                PARTITION BY notion_id,
                                             COALESCE(NULLIF(content_hash, ''), '__no_hash__'),
                                             COALESCE(source_updated_at, 'epoch'::timestamptz)
                                ORDER BY CASE WHEN is_active THEN 0 ELSE 1 END, valid_from DESC, id DESC
                            ) AS rn
                        FROM workspace_mirror
                    ),
                    deleted AS (
                        DELETE FROM workspace_mirror
                        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
                        RETURNING 1
                    )
                    SELECT COUNT(*)::INT AS removed FROM deleted
                    """
                )
                details["deduplicated_versions"] = int((cur.fetchone() or {}).get("removed", 0) or 0)

                cur.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            id,
                            notion_id,
                            is_active,
                            COALESCE(valid_to, valid_from) AS closed_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY notion_id
                                ORDER BY CASE WHEN is_active THEN 0 ELSE 1 END, valid_from DESC, id DESC
                            ) AS version_rank
                        FROM workspace_mirror
                    ),
                    deleted AS (
                        DELETE FROM workspace_mirror
                        WHERE id IN (
                            SELECT id
                            FROM ranked
                            WHERE is_active = FALSE
                              AND version_rank > %s
                              AND closed_at < %s
                        )
                        RETURNING 1
                    )
                    SELECT COUNT(*)::INT AS removed FROM deleted
                    """,
                    (min_versions, cutoff),
                )
                details["pruned_versions"] = int((cur.fetchone() or {}).get("removed", 0) or 0)

            self.conn.commit()

            removed_total = int(details["deduplicated_versions"] or 0) + int(details["pruned_versions"] or 0)
            previous_autocommit = self.conn.autocommit
            self.conn.autocommit = True
            try:
                with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if removed_total >= 1000:
                        cur.execute("VACUUM (FULL, ANALYZE) workspace_mirror")
                    else:
                        cur.execute("VACUUM (ANALYZE) workspace_mirror")
                    cur.execute("ANALYZE activity_log")
                    cur.execute(
                        """
                        SELECT
                            COUNT(*) FILTER (WHERE is_active = TRUE)::INT AS active_pages,
                            COUNT(*)::INT AS total_versions,
                            pg_size_pretty(pg_database_size(current_database())) AS database_size_text
                        FROM workspace_mirror
                        """
                    )
                    after = dict(cur.fetchone() or {})
            finally:
                self.conn.autocommit = previous_autocommit

            details["active_pages"] = int(after.get("active_pages", 0) or 0)
            details["total_versions"] = int(after.get("total_versions", 0) or 0)
            details["database_size_text"] = str(after.get("database_size_text") or "Not available yet")

            summary = (
                f"Optimised the local mirror: removed {removed_total} duplicate or older snapshot(s) "
                f"and refreshed PostgreSQL statistics for {details['active_pages']} active page(s)."
            )
            self.log_activity(
                category="maintenance",
                action="local_database_maintenance",
                status="success",
                summary=summary,
                details=details,
            )
            return True, summary, details
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            message = f"Local database clean-up failed: {exc}"
            logger.error(message)
            self.log_activity(
                category="maintenance",
                action="local_database_maintenance",
                status="error",
                summary=message,
                details=details,
            )
            return False, message, details

    @staticmethod
    def _collapse_preview_text(value: str | None, limit: int = 280) -> str:
        text = " ".join(str(value or "").split())
        return text[:limit]

    def check_workspace_json_health(
        self,
        sample_limit: int = 50,
        rows: list[dict] | None = None,
        log_results: bool = True,
    ) -> dict:
        summary = {
            "checked_pages": 0,
            "pages_with_issues": 0,
            "issue_count": 0,
            "sample_pages": [],
            "issue_examples": [],
        }

        sample_rows = rows
        if sample_rows is None:
            if not self.conn:
                return summary

            safe_limit = max(1, min(int(sample_limit or 50), 500))
            try:
                with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT notion_id, raw_json
                        FROM workspace_mirror
                        WHERE is_active = TRUE
                        ORDER BY updated_at DESC NULLS LAST, pulled_at DESC NULLS LAST, id DESC
                        LIMIT %s
                        """,
                        (safe_limit,),
                    )
                    sample_rows = [dict(row) for row in cur.fetchall()]
            except Exception as exc:
                if self.conn:
                    self.conn.rollback()
                logger.warning(f"Workspace JSON health check query failed: {exc}")
                return summary

        for row in sample_rows or []:
            notion_id = str((row or {}).get("notion_id") or "").strip() or "unknown"
            _, issues = normalize_workspace_page_json((row or {}).get("raw_json") or {})
            summary["checked_pages"] += 1
            if not issues:
                continue

            summary["pages_with_issues"] += 1
            summary["issue_count"] += len(issues)
            if notion_id not in summary["sample_pages"]:
                summary["sample_pages"].append(notion_id)
            for issue in issues[:3]:
                if len(summary["issue_examples"]) >= 5:
                    break
                summary["issue_examples"].append(f"{notion_id}: {issue}")

        if log_results and summary["pages_with_issues"]:
            message = (
                f"Workspace JSON health check found {summary['pages_with_issues']} page(s) with "
                f"{summary['issue_count']} shape issue(s) in the recent sample."
            )
            logger.warning(message)
            self.log_activity(
                category="database",
                action="workspace_json_health",
                summary=message,
                status="warning",
                details=summary,
            )

        return summary

    def get_catalog_stats(self, force_refresh: bool = False, max_age_seconds: float = 15.0) -> dict:
        if not self.conn:
            return self.get_cached_catalog_stats_snapshot()

        if not force_refresh and self._catalog_stats_cache is not None and self._cache_is_fresh(
            self._catalog_stats_cache_checked_at,
            max_age_seconds,
        ):
            return dict(self._catalog_stats_cache)

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*)::INT AS active_pages,
                        COALESCE(SUM(CASE WHEN needs_push THEN 1 ELSE 0 END), 0)::INT AS pending_push_pages,
                        COALESCE(
                            SUM(
                                CASE WHEN COALESCE(jsonb_array_length(jsonb_safe_array(media_local_paths)), 0) > 0
                                    THEN 1 ELSE 0 END
                            ),
                            0
                        )::INT AS pages_with_media,
                        MAX(updated_at) AS latest_local_update,
                        MAX(source_updated_at) AS latest_source_update
                    FROM workspace_mirror
                    WHERE is_active = TRUE
                    """
                )
                stats = dict(cur.fetchone() or {})
                self._catalog_stats_cache = dict(stats)
                self._catalog_stats_cache_checked_at = time.monotonic()
                return dict(self._catalog_stats_cache)
        except Exception as e:
            logger.error(f"Failed to fetch workspace catalog stats: {e}")
            return self.get_cached_catalog_stats_snapshot()

    def list_catalog_entries(self, search_text: str = "", limit: int = 25, filter_mode: str = "all") -> list[dict]:
        if not self.conn:
            return []

        try:
            safe_limit = max(1, min(int(limit or 25), 500))
        except (TypeError, ValueError):
            safe_limit = 25

        query = """
            SELECT notion_id, title, ai_summary, media_local_paths, raw_json,
                   source_updated_at, pulled_at, updated_at, needs_push
            FROM workspace_mirror
            WHERE is_active = TRUE
        """
        params: list = []
        filter_text = str(search_text or "").strip()
        mode = str(filter_mode or "all").strip().lower()

        if filter_text:
            like_value = f"%{filter_text}%"
            query += """
                AND (
                    COALESCE(title, '') ILIKE %s
                    OR COALESCE(ai_summary, '') ILIKE %s
                    OR COALESCE(raw_json::text, '') ILIKE %s
                )
            """
            params.extend([like_value, like_value, like_value])

        if mode == "pending":
            query += " AND needs_push = TRUE"
        elif mode == "media":
            query += " AND COALESCE(jsonb_array_length(jsonb_safe_array(media_local_paths)), 0) > 0"
        elif mode == "recent":
            query += " AND COALESCE(updated_at, pulled_at, source_updated_at) >= now() - interval '7 days'"

        query += " ORDER BY updated_at DESC NULLS LAST, source_updated_at DESC NULLS LAST LIMIT %s"
        params.append(safe_limit)

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                rows = [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch workspace catalog entries: {e}")
            return []

        entries: list[dict] = []
        for row in rows:
            raw_json, _ = normalize_workspace_page_json(row.get("raw_json") or {})
            properties = raw_json.get("properties", {}) if isinstance(raw_json.get("properties"), dict) else {}
            link_info = raw_json.get("_business_brain_links") if isinstance(raw_json.get("_business_brain_links"), dict) else {}

            media_paths = row.get("media_local_paths")
            if not isinstance(media_paths, list):
                media_paths = []
            summary_preview = self._collapse_preview_text(row.get("ai_summary"), 280)
            property_keys = sorted(str(key) for key in properties.keys())[:12]
            notion_id = row.get("notion_id", "")

            entries.append(
                {
                    "notion_id": notion_id,
                    "title": (row.get("title") or "Untitled").strip() or "Untitled",
                    "summary_preview": summary_preview,
                    "summary_chars": len(summary_preview),
                    "approx_prompt_tokens": int(
                        ((len(row.get("title") or "") + len(row.get("ai_summary") or "")) / 4.0) or 0
                    ),
                    "media_count": len(media_paths),
                    "has_media": bool(media_paths),
                    "property_count": len(properties),
                    "property_keys": property_keys,
                    "internal_resource_uri": link_info.get("internal_resource_uri") or (f"bb://page/{notion_id}" if notion_id else ""),
                    "user_notion_url": link_info.get("user_notion_url") or (raw_json.get("url", "") if isinstance(raw_json, dict) else ""),
                    "linked_page_count": len(link_info.get("linked_notion_ids") or []),
                    "source_object": (raw_json.get("object") or "page") if isinstance(raw_json, dict) else "page",
                    "source_updated_at": row.get("source_updated_at"),
                    "pulled_at": row.get("pulled_at"),
                    "updated_at": row.get("updated_at"),
                    "needs_push": bool(row.get("needs_push")),
                }
            )

        return entries

    def get_page_context(self, notion_id: str) -> dict:
        if not self.conn or not notion_id:
            return {}

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT notion_id, title, ai_summary, media_local_paths, raw_json,
                           source_updated_at, pulled_at, updated_at, needs_push
                    FROM workspace_mirror
                    WHERE is_active = TRUE AND notion_id = %s
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (notion_id,),
                )
                row = dict(cur.fetchone() or {})
        except Exception as e:
            logger.error(f"Failed to fetch workspace page context for {notion_id}: {e}")
            return {}

        if not row:
            return {}

        raw_json, _ = normalize_workspace_page_json(row.get("raw_json") or {})
        properties = raw_json.get("properties", {}) if isinstance(raw_json.get("properties"), dict) else {}
        link_info = raw_json.get("_business_brain_links") if isinstance(raw_json.get("_business_brain_links"), dict) else {}
        if not link_info:
            link_info = self.build_page_link_index(raw_json if isinstance(raw_json, dict) else {"id": notion_id})

        property_preview = {
            str(name): self._display_property_value(value)
            for name, value in properties.items()
        }

        media_paths = row.get("media_local_paths")
        if not isinstance(media_paths, list):
            media_paths = []
        return {
            "notion_id": row.get("notion_id", notion_id),
            "title": (row.get("title") or "Untitled").strip() or "Untitled",
            "ai_summary": row.get("ai_summary") or "",
            "property_preview": property_preview,
            "internal_resource_uri": link_info.get("internal_resource_uri") or f"bb://page/{notion_id}",
            "user_notion_url": link_info.get("user_notion_url") or (raw_json.get("url", "") if isinstance(raw_json, dict) else ""),
            "linked_notion_ids": link_info.get("linked_notion_ids") or [],
            "internal_links": link_info.get("linked_resources") or [],
            "media_local_paths": media_paths,
            "media_count": len(media_paths),
            "has_media": bool(media_paths),
            "approx_prompt_tokens": int(((len(row.get("title") or "") + len(row.get("ai_summary") or "")) / 4.0) or 0),
            "source_updated_at": row.get("source_updated_at"),
            "pulled_at": row.get("pulled_at"),
            "updated_at": row.get("updated_at"),
            "needs_push": bool(row.get("needs_push")),
        }

    def ensure_clickup_schema(self) -> bool:
        if not self.conn:
            return False

        try:
            self.ensure_service_schema(
                "clickup",
                display_name="ClickUp",
                description="Task sync and comment archive module.",
            )
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE SCHEMA IF NOT EXISTS clickup;

                    CREATE TABLE IF NOT EXISTS clickup.clickup_tasks (
                        id BIGSERIAL PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        name TEXT,
                        status TEXT,
                        assignees JSONB DEFAULT '[]'::jsonb,
                        custom_fields JSONB DEFAULT '{}'::jsonb,
                        markdown_description TEXT DEFAULT '',
                        description_html TEXT DEFAULT '',
                        task_url TEXT DEFAULT '',
                        list_id TEXT,
                        folder_id TEXT,
                        space_id TEXT,
                        parent_task_id TEXT,
                        priority TEXT,
                        due_date TIMESTAMPTZ,
                        date_created TIMESTAMPTZ,
                        date_updated TIMESTAMPTZ,
                        synced_at TIMESTAMPTZ DEFAULT now(),
                        raw_json JSONB DEFAULT '{}'::jsonb,
                        valid_from TIMESTAMPTZ DEFAULT now(),
                        valid_to TIMESTAMPTZ DEFAULT '9999-12-31 23:59:59Z',
                        is_active BOOLEAN DEFAULT TRUE
                    );

                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS task_id TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS name TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS status TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS assignees JSONB DEFAULT '[]'::jsonb;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS custom_fields JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS markdown_description TEXT DEFAULT '';
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS description_html TEXT DEFAULT '';
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS task_url TEXT DEFAULT '';
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS list_id TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS folder_id TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS space_id TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS parent_task_id TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS priority TEXT;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS due_date TIMESTAMPTZ;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS date_created TIMESTAMPTZ;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS date_updated TIMESTAMPTZ;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ DEFAULT '9999-12-31 23:59:59Z';
                    ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

                    UPDATE clickup.clickup_tasks SET assignees = '[]'::jsonb WHERE assignees IS NULL;
                    UPDATE clickup.clickup_tasks SET custom_fields = '{}'::jsonb WHERE custom_fields IS NULL;
                    UPDATE clickup.clickup_tasks SET raw_json = '{}'::jsonb WHERE raw_json IS NULL;
                    UPDATE clickup.clickup_tasks SET is_active = TRUE WHERE is_active IS NULL;

                    CREATE INDEX IF NOT EXISTS idx_clickup_tasks_task_id ON clickup.clickup_tasks(task_id);
                    CREATE INDEX IF NOT EXISTS idx_clickup_tasks_status ON clickup.clickup_tasks(status);
                    CREATE INDEX IF NOT EXISTS idx_clickup_tasks_valid_from ON clickup.clickup_tasks(valid_from DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_clickup_tasks_single_active
                        ON clickup.clickup_tasks(task_id)
                        WHERE is_active = TRUE;

                    CREATE TABLE IF NOT EXISTS clickup.clickup_comments (
                        id BIGSERIAL PRIMARY KEY,
                        comment_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        comment_text TEXT DEFAULT '',
                        user_json JSONB DEFAULT '{}'::jsonb,
                        raw_json JSONB DEFAULT '{}'::jsonb,
                        date_created TIMESTAMPTZ,
                        date_updated TIMESTAMPTZ,
                        synced_at TIMESTAMPTZ DEFAULT now(),
                        valid_from TIMESTAMPTZ DEFAULT now(),
                        valid_to TIMESTAMPTZ DEFAULT '9999-12-31 23:59:59Z',
                        is_active BOOLEAN DEFAULT TRUE
                    );

                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS comment_id TEXT;
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS task_id TEXT;
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS comment_text TEXT DEFAULT '';
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS user_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS date_created TIMESTAMPTZ;
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS date_updated TIMESTAMPTZ;
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ DEFAULT '9999-12-31 23:59:59Z';
                    ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

                    UPDATE clickup.clickup_comments SET user_json = '{}'::jsonb WHERE user_json IS NULL;
                    UPDATE clickup.clickup_comments SET raw_json = '{}'::jsonb WHERE raw_json IS NULL;
                    UPDATE clickup.clickup_comments SET is_active = TRUE WHERE is_active IS NULL;

                    CREATE INDEX IF NOT EXISTS idx_clickup_comments_task_id ON clickup.clickup_comments(task_id);
                    CREATE INDEX IF NOT EXISTS idx_clickup_comments_valid_from ON clickup.clickup_comments(valid_from DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_clickup_comments_single_active
                        ON clickup.clickup_comments(comment_id)
                        WHERE is_active = TRUE;
                    """
                )
            self.conn.commit()
            return True
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not prepare the ClickUp task tables: {exc}")
            return False

    def get_clickup_status(self) -> dict:
        default = {
            "connected": False,
            "current_tasks": 0,
            "open_tasks": 0,
            "current_comments": 0,
            "last_synced_at": None,
            "last_sync_status": "planned",
            "last_sync_summary": "No ClickUp sync has run yet.",
        }

        if not self.conn or not self.ensure_clickup_schema():
            return default

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE is_active = TRUE) AS current_tasks,
                        COUNT(*) FILTER (
                            WHERE is_active = TRUE
                              AND COALESCE(lower(status), '') NOT IN ('closed', 'complete', 'completed', 'done')
                        ) AS open_tasks,
                        (
                            SELECT COUNT(*)
                            FROM clickup.clickup_comments cc
                            WHERE cc.is_active = TRUE
                        ) AS current_comments,
                        MAX(synced_at) FILTER (WHERE is_active = TRUE) AS last_synced_at
                    FROM clickup.clickup_tasks
                    """
                )
                stats = dict(cur.fetchone() or {})
                cur.execute(
                    """
                    SELECT run_finished_at, status, summary
                    FROM clickup.sync_runs
                    ORDER BY COALESCE(run_finished_at, run_started_at) DESC, id DESC
                    LIMIT 1
                    """
                )
                last_run = dict(cur.fetchone() or {})
                return {
                    **default,
                    **stats,
                    "connected": True,
                    "last_synced_at": last_run.get("run_finished_at") or stats.get("last_synced_at"),
                    "last_sync_status": last_run.get("status") or default["last_sync_status"],
                    "last_sync_summary": last_run.get("summary") or default["last_sync_summary"],
                }
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.warning(f"Could not read the ClickUp module status yet: {exc}")
            return default

    def upsert_clickup_tasks(self, tasks: list[dict], retire_missing: bool = False) -> dict:
        results = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "retired": 0}
        if not self.conn or not self.ensure_clickup_schema():
            return results

        snapshot_time = datetime.now(timezone.utc)
        seen_ids: set[str] = set()

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                for task in tasks or []:
                    task_id = str((task or {}).get("task_id") or (task or {}).get("id") or "").strip()
                    if not task_id:
                        continue

                    seen_ids.add(task_id)
                    results["seen"] += 1
                    assignees = list((task or {}).get("assignees") or [])
                    custom_fields = dict((task or {}).get("custom_fields") or {})
                    raw_json = copy.deepcopy((task or {}).get("raw_json") or task or {})
                    incoming = {
                        "name": str((task or {}).get("name") or "Untitled task").strip() or "Untitled task",
                        "status": str((task or {}).get("status") or "").strip(),
                        "assignees": assignees,
                        "custom_fields": custom_fields,
                        "markdown_description": str((task or {}).get("markdown_description") or "").strip(),
                        "description_html": str((task or {}).get("description_html") or (task or {}).get("description") or "").strip(),
                        "task_url": str((task or {}).get("task_url") or (task or {}).get("url") or "").strip(),
                        "list_id": str((task or {}).get("list_id") or "").strip() or None,
                        "folder_id": str((task or {}).get("folder_id") or "").strip() or None,
                        "space_id": str((task or {}).get("space_id") or "").strip() or None,
                        "parent_task_id": str((task or {}).get("parent_task_id") or "").strip() or None,
                        "priority": str((task or {}).get("priority") or "").strip() or None,
                        "due_date": (task or {}).get("due_date"),
                        "date_created": (task or {}).get("date_created"),
                        "date_updated": (task or {}).get("date_updated"),
                        "raw_json": raw_json,
                    }

                    cur.execute(
                        """
                        SELECT id, name, status, assignees, custom_fields, markdown_description,
                               description_html, task_url, list_id, folder_id, space_id,
                               parent_task_id, priority, due_date, date_created, date_updated, raw_json
                        FROM clickup.clickup_tasks
                        WHERE task_id = %s AND is_active = TRUE
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (task_id,),
                    )
                    current = dict(cur.fetchone() or {})
                    comparable_current = {key: current.get(key) for key in incoming.keys()}
                    current_signature = json.dumps(comparable_current, sort_keys=True, default=str)
                    incoming_signature = json.dumps(incoming, sort_keys=True, default=str)

                    if not current:
                        cur.execute(
                            """
                            INSERT INTO clickup.clickup_tasks (
                                task_id, name, status, assignees, custom_fields, markdown_description,
                                description_html, task_url, list_id, folder_id, space_id,
                                parent_task_id, priority, due_date, date_created, date_updated,
                                synced_at, raw_json, valid_from, valid_to, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, TRUE)
                            """,
                            (
                                task_id,
                                incoming["name"],
                                incoming["status"],
                                Json(incoming["assignees"]),
                                Json(incoming["custom_fields"]),
                                incoming["markdown_description"],
                                incoming["description_html"],
                                incoming["task_url"],
                                incoming["list_id"],
                                incoming["folder_id"],
                                incoming["space_id"],
                                incoming["parent_task_id"],
                                incoming["priority"],
                                incoming["due_date"],
                                incoming["date_created"],
                                incoming["date_updated"],
                                Json(incoming["raw_json"]),
                                snapshot_time,
                                datetime.max.replace(tzinfo=timezone.utc),
                            ),
                        )
                        results["inserted"] += 1
                        continue

                    if current_signature != incoming_signature:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_tasks
                            SET valid_to = %s, is_active = FALSE
                            WHERE id = %s
                            """,
                            (snapshot_time, current.get("id")),
                        )
                        cur.execute(
                            """
                            INSERT INTO clickup.clickup_tasks (
                                task_id, name, status, assignees, custom_fields, markdown_description,
                                description_html, task_url, list_id, folder_id, space_id,
                                parent_task_id, priority, due_date, date_created, date_updated,
                                synced_at, raw_json, valid_from, valid_to, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, TRUE)
                            """,
                            (
                                task_id,
                                incoming["name"],
                                incoming["status"],
                                Json(incoming["assignees"]),
                                Json(incoming["custom_fields"]),
                                incoming["markdown_description"],
                                incoming["description_html"],
                                incoming["task_url"],
                                incoming["list_id"],
                                incoming["folder_id"],
                                incoming["space_id"],
                                incoming["parent_task_id"],
                                incoming["priority"],
                                incoming["due_date"],
                                incoming["date_created"],
                                incoming["date_updated"],
                                Json(incoming["raw_json"]),
                                snapshot_time,
                                datetime.max.replace(tzinfo=timezone.utc),
                            ),
                        )
                        results["updated"] += 1
                    else:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_tasks
                            SET synced_at = now(), date_updated = COALESCE(%s, date_updated)
                            WHERE id = %s
                            """,
                            (incoming["date_updated"], current.get("id")),
                        )
                        results["unchanged"] += 1

                if retire_missing:
                    if seen_ids:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_tasks
                            SET valid_to = %s, is_active = FALSE
                            WHERE is_active = TRUE AND NOT (task_id = ANY(%s))
                            """,
                            (snapshot_time, list(seen_ids)),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_tasks
                            SET valid_to = %s, is_active = FALSE
                            WHERE is_active = TRUE
                            """,
                            (snapshot_time,),
                        )
                    results["retired"] = int(cur.rowcount or 0)

            self.conn.commit()
            return results
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not mirror ClickUp tasks yet: {exc}")
            return results

    def upsert_clickup_comments(
        self,
        comments: list[dict],
        scope_task_ids: list[str] | None = None,
        retire_missing: bool = False,
    ) -> dict:
        results = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "retired": 0}
        if not self.conn or not self.ensure_clickup_schema():
            return results

        snapshot_time = datetime.now(timezone.utc)
        seen_ids: set[str] = set()
        scoped_tasks = [str(item).strip() for item in (scope_task_ids or []) if str(item).strip()]

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                for comment in comments or []:
                    comment_id = str((comment or {}).get("comment_id") or (comment or {}).get("id") or "").strip()
                    task_id = str((comment or {}).get("task_id") or "").strip()
                    if not comment_id or not task_id:
                        continue

                    seen_ids.add(comment_id)
                    results["seen"] += 1
                    incoming = {
                        "task_id": task_id,
                        "comment_text": str((comment or {}).get("comment_text") or "").strip(),
                        "user_json": copy.deepcopy((comment or {}).get("user_json") or {}),
                        "date_created": (comment or {}).get("date_created"),
                        "date_updated": (comment or {}).get("date_updated"),
                        "raw_json": copy.deepcopy((comment or {}).get("raw_json") or comment or {}),
                    }

                    cur.execute(
                        """
                        SELECT id, task_id, comment_text, user_json, date_created, date_updated, raw_json
                        FROM clickup.clickup_comments
                        WHERE comment_id = %s AND is_active = TRUE
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (comment_id,),
                    )
                    current = dict(cur.fetchone() or {})
                    comparable_current = {key: current.get(key) for key in incoming.keys()}
                    current_signature = json.dumps(comparable_current, sort_keys=True, default=str)
                    incoming_signature = json.dumps(incoming, sort_keys=True, default=str)

                    if not current:
                        cur.execute(
                            """
                            INSERT INTO clickup.clickup_comments (
                                comment_id, task_id, comment_text, user_json, raw_json,
                                date_created, date_updated, synced_at, valid_from, valid_to, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s, TRUE)
                            """,
                            (
                                comment_id,
                                task_id,
                                incoming["comment_text"],
                                Json(incoming["user_json"]),
                                Json(incoming["raw_json"]),
                                incoming["date_created"],
                                incoming["date_updated"],
                                snapshot_time,
                                datetime.max.replace(tzinfo=timezone.utc),
                            ),
                        )
                        results["inserted"] += 1
                        continue

                    if current_signature != incoming_signature:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_comments
                            SET valid_to = %s, is_active = FALSE
                            WHERE id = %s
                            """,
                            (snapshot_time, current.get("id")),
                        )
                        cur.execute(
                            """
                            INSERT INTO clickup.clickup_comments (
                                comment_id, task_id, comment_text, user_json, raw_json,
                                date_created, date_updated, synced_at, valid_from, valid_to, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s, TRUE)
                            """,
                            (
                                comment_id,
                                task_id,
                                incoming["comment_text"],
                                Json(incoming["user_json"]),
                                Json(incoming["raw_json"]),
                                incoming["date_created"],
                                incoming["date_updated"],
                                snapshot_time,
                                datetime.max.replace(tzinfo=timezone.utc),
                            ),
                        )
                        results["updated"] += 1
                    else:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_comments
                            SET synced_at = now(), date_updated = COALESCE(%s, date_updated)
                            WHERE id = %s
                            """,
                            (incoming["date_updated"], current.get("id")),
                        )
                        results["unchanged"] += 1

                if retire_missing:
                    if scoped_tasks and seen_ids:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_comments
                            SET valid_to = %s, is_active = FALSE
                            WHERE is_active = TRUE
                              AND task_id = ANY(%s)
                              AND NOT (comment_id = ANY(%s))
                            """,
                            (snapshot_time, scoped_tasks, list(seen_ids)),
                        )
                    elif scoped_tasks:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_comments
                            SET valid_to = %s, is_active = FALSE
                            WHERE is_active = TRUE AND task_id = ANY(%s)
                            """,
                            (snapshot_time, scoped_tasks),
                        )
                    elif seen_ids:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_comments
                            SET valid_to = %s, is_active = FALSE
                            WHERE is_active = TRUE AND NOT (comment_id = ANY(%s))
                            """,
                            (snapshot_time, list(seen_ids)),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE clickup.clickup_comments
                            SET valid_to = %s, is_active = FALSE
                            WHERE is_active = TRUE
                            """,
                            (snapshot_time,),
                        )
                    results["retired"] = int(cur.rowcount or 0)

            self.conn.commit()
            return results
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not mirror ClickUp comments yet: {exc}")
            return results

    def ensure_n8n_schema(self) -> bool:
        if not self.conn:
            return False

        try:
            self.ensure_service_schema(
                "n8n",
                display_name="n8n",
                description="Workflow backup and automation history module.",
            )
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE SCHEMA IF NOT EXISTS n8n;
                    CREATE TABLE IF NOT EXISTS n8n.n8n_workflows (
                        id BIGSERIAL PRIMARY KEY,
                        workflow_id TEXT NOT NULL,
                        name TEXT,
                        active BOOLEAN DEFAULT FALSE,
                        workflow_json JSONB DEFAULT '{}'::jsonb,
                        valid_from TIMESTAMPTZ DEFAULT now(),
                        valid_to TIMESTAMPTZ DEFAULT '9999-12-31 23:59:59Z',
                        is_active BOOLEAN DEFAULT TRUE
                    );
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS workflow_id TEXT;
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS name TEXT;
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT FALSE;
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS workflow_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ DEFAULT '9999-12-31 23:59:59Z';
                    ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                    CREATE INDEX IF NOT EXISTS idx_n8n_workflows_workflow_id ON n8n.n8n_workflows(workflow_id);
                    CREATE INDEX IF NOT EXISTS idx_n8n_workflows_valid_from ON n8n.n8n_workflows(valid_from DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_n8n_workflows_single_active
                        ON n8n.n8n_workflows(workflow_id)
                        WHERE is_active = TRUE;
                    """
                )
            self.conn.commit()
            return True
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not prepare the n8n workflow table: {exc}")
            return False

    def ensure_mautic_schema(self) -> bool:
        if not self.conn:
            return False

        try:
            self.ensure_service_schema(
                "mautic",
                display_name="Mautic",
                description="Marketing automation archive and campaign snapshot module.",
            )
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE SCHEMA IF NOT EXISTS mautic;

                    CREATE TABLE IF NOT EXISTS mautic.mautic_contacts (
                        id BIGSERIAL PRIMARY KEY,
                        contact_id TEXT NOT NULL,
                        email TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        company TEXT,
                        stage TEXT,
                        tags JSONB DEFAULT '[]'::jsonb,
                        campaign_ids JSONB DEFAULT '[]'::jsonb,
                        campaign_metrics JSONB DEFAULT '{}'::jsonb,
                        raw_json JSONB DEFAULT '{}'::jsonb,
                        last_active_at TIMESTAMPTZ,
                        date_added TIMESTAMPTZ,
                        date_modified TIMESTAMPTZ,
                        synced_at TIMESTAMPTZ DEFAULT now(),
                        updated_at TIMESTAMPTZ DEFAULT now(),
                        is_active BOOLEAN DEFAULT TRUE
                    );
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS contact_id TEXT;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS email TEXT;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS first_name TEXT;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS last_name TEXT;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS company TEXT;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS stage TEXT;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS campaign_ids JSONB DEFAULT '[]'::jsonb;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS campaign_metrics JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS date_added TIMESTAMPTZ;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS date_modified TIMESTAMPTZ;
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE mautic.mautic_contacts ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_mautic_contacts_contact_id ON mautic.mautic_contacts(contact_id);
                    CREATE INDEX IF NOT EXISTS idx_mautic_contacts_email ON mautic.mautic_contacts(email);
                    CREATE INDEX IF NOT EXISTS idx_mautic_contacts_updated_at ON mautic.mautic_contacts(updated_at DESC);

                    CREATE TABLE IF NOT EXISTS mautic.mautic_campaigns (
                        id BIGSERIAL PRIMARY KEY,
                        campaign_id TEXT NOT NULL,
                        name TEXT,
                        description TEXT,
                        category TEXT,
                        is_published BOOLEAN DEFAULT FALSE,
                        contact_count INTEGER DEFAULT 0,
                        list_count INTEGER DEFAULT 0,
                        event_count INTEGER DEFAULT 0,
                        stats_json JSONB DEFAULT '{}'::jsonb,
                        raw_json JSONB DEFAULT '{}'::jsonb,
                        date_added TIMESTAMPTZ,
                        date_modified TIMESTAMPTZ,
                        synced_at TIMESTAMPTZ DEFAULT now(),
                        updated_at TIMESTAMPTZ DEFAULT now()
                    );
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS campaign_id TEXT;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS name TEXT;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS description TEXT;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS category TEXT;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS is_published BOOLEAN DEFAULT FALSE;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS contact_count INTEGER DEFAULT 0;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS list_count INTEGER DEFAULT 0;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS event_count INTEGER DEFAULT 0;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS stats_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS date_added TIMESTAMPTZ;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS date_modified TIMESTAMPTZ;
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ DEFAULT now();
                    ALTER TABLE mautic.mautic_campaigns ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_mautic_campaigns_campaign_id ON mautic.mautic_campaigns(campaign_id);
                    CREATE INDEX IF NOT EXISTS idx_mautic_campaigns_name ON mautic.mautic_campaigns(name);
                    CREATE INDEX IF NOT EXISTS idx_mautic_campaigns_updated_at ON mautic.mautic_campaigns(updated_at DESC);
                    """
                )
            self.conn.commit()
            return True
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not prepare the Mautic tables: {exc}")
            return False

    def upsert_mautic_contacts(self, contacts: list[dict]) -> dict:
        results = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0}
        if not self.conn or not self.ensure_mautic_schema():
            return results

        snapshot_time = datetime.now(timezone.utc)

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                for contact in contacts or []:
                    contact_id = str((contact or {}).get("id") or (contact or {}).get("contactId") or "").strip()
                    if not contact_id:
                        continue

                    results["seen"] += 1
                    fields_container = (contact or {}).get("fields") or {}
                    fields = dict((fields_container or {}).get("all") or fields_container or {}) if isinstance(fields_container, dict) else {}

                    email = str(fields.get("email") or (contact or {}).get("email") or "").strip() or None
                    first_name = str(fields.get("firstname") or fields.get("first_name") or (contact or {}).get("firstname") or "").strip() or None
                    last_name = str(fields.get("lastname") or fields.get("last_name") or (contact or {}).get("lastname") or "").strip() or None
                    company = str(fields.get("company") or (contact or {}).get("company") or "").strip() or None
                    stage = str(fields.get("stage") or (contact or {}).get("stage") or "").strip() or None

                    tag_source = (contact or {}).get("tags") or []
                    if isinstance(tag_source, dict):
                        tag_source = list(tag_source.values())
                    tags = []
                    for item in tag_source:
                        label = item.get("tag") or item.get("label") or item.get("name") if isinstance(item, dict) else item
                        label = str(label or "").strip()
                        if label and label not in tags:
                            tags.append(label)

                    campaigns_source = (contact or {}).get("campaigns") or {}
                    if isinstance(campaigns_source, dict):
                        campaign_items = [item for item in campaigns_source.values() if isinstance(item, dict)]
                    elif isinstance(campaigns_source, list):
                        campaign_items = [item for item in campaigns_source if isinstance(item, dict)]
                    else:
                        campaign_items = []

                    campaign_ids = []
                    for item in campaign_items:
                        campaign_id = str(item.get("id") or item.get("campaignId") or "").strip()
                        if campaign_id and campaign_id not in campaign_ids:
                            campaign_ids.append(campaign_id)

                    try:
                        points = int(float(fields.get("points") or (contact or {}).get("points") or 0))
                    except (TypeError, ValueError):
                        points = 0

                    campaign_metrics = {
                        "campaign_count": len(campaign_ids),
                        "campaign_ids": campaign_ids,
                        "points": points,
                        "tag_count": len(tags),
                        "do_not_contact_entries": len((contact or {}).get("doNotContact") or []),
                    }

                    last_active_at = (contact or {}).get("lastActive") or (contact or {}).get("lastActiveAt") or fields.get("lastactive")
                    date_added = (contact or {}).get("dateAdded") or fields.get("dateAdded")
                    date_modified = (contact or {}).get("dateModified") or fields.get("dateModified")

                    cur.execute(
                        """
                        SELECT email, first_name, last_name, company, stage, tags, campaign_ids, campaign_metrics, raw_json
                        FROM mautic.mautic_contacts
                        WHERE contact_id = %s
                        """,
                        (contact_id,),
                    )
                    existing = dict(cur.fetchone() or {})
                    existing_signature = json.dumps(existing, sort_keys=True, default=str)
                    incoming_signature = json.dumps(
                        {
                            "email": email,
                            "first_name": first_name,
                            "last_name": last_name,
                            "company": company,
                            "stage": stage,
                            "tags": tags,
                            "campaign_ids": campaign_ids,
                            "campaign_metrics": campaign_metrics,
                            "raw_json": contact or {},
                        },
                        sort_keys=True,
                        default=str,
                    )

                    if not existing:
                        results["inserted"] += 1
                    elif existing_signature == incoming_signature:
                        results["unchanged"] += 1
                    else:
                        results["updated"] += 1

                    cur.execute(
                        """
                        INSERT INTO mautic.mautic_contacts (
                            contact_id, email, first_name, last_name, company, stage,
                            tags, campaign_ids, campaign_metrics, raw_json,
                            last_active_at, date_added, date_modified, synced_at, updated_at, is_active
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), TRUE)
                        ON CONFLICT (contact_id)
                        DO UPDATE SET
                            email = EXCLUDED.email,
                            first_name = EXCLUDED.first_name,
                            last_name = EXCLUDED.last_name,
                            company = EXCLUDED.company,
                            stage = EXCLUDED.stage,
                            tags = EXCLUDED.tags,
                            campaign_ids = EXCLUDED.campaign_ids,
                            campaign_metrics = EXCLUDED.campaign_metrics,
                            raw_json = EXCLUDED.raw_json,
                            last_active_at = EXCLUDED.last_active_at,
                            date_added = EXCLUDED.date_added,
                            date_modified = EXCLUDED.date_modified,
                            synced_at = EXCLUDED.synced_at,
                            updated_at = now(),
                            is_active = TRUE
                        """,
                        (
                            contact_id,
                            email,
                            first_name,
                            last_name,
                            company,
                            stage,
                            Json(tags),
                            Json(campaign_ids),
                            Json(campaign_metrics),
                            Json(contact or {}),
                            last_active_at,
                            date_added,
                            date_modified,
                            snapshot_time,
                        ),
                    )
            self.conn.commit()
            return results
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not mirror Mautic contacts yet: {exc}")
            return results

    def upsert_mautic_campaigns(self, campaigns: list[dict]) -> dict:
        results = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0}
        if not self.conn or not self.ensure_mautic_schema():
            return results

        snapshot_time = datetime.now(timezone.utc)

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                for campaign in campaigns or []:
                    campaign_id = str((campaign or {}).get("id") or (campaign or {}).get("campaignId") or "").strip()
                    if not campaign_id:
                        continue

                    results["seen"] += 1
                    category_value = (campaign or {}).get("category") or {}
                    if isinstance(category_value, dict):
                        category = str(category_value.get("title") or category_value.get("name") or "").strip() or None
                    else:
                        category = str(category_value or "").strip() or None

                    lists_value = (campaign or {}).get("lists") or []
                    events_value = (campaign or {}).get("events") or []
                    contacts_value = (campaign or {}).get("leads") or (campaign or {}).get("contacts") or []

                    list_count = len(lists_value) if isinstance(lists_value, (list, dict)) else int(lists_value or 0)
                    event_count = len(events_value) if isinstance(events_value, (list, dict)) else int(events_value or 0)
                    contact_count = len(contacts_value) if isinstance(contacts_value, (list, dict)) else int(contacts_value or 0)
                    is_published = bool((campaign or {}).get("isPublished") if (campaign or {}).get("isPublished") is not None else (campaign or {}).get("is_published"))

                    stats_json = {
                        "contacts": contact_count,
                        "lists": list_count,
                        "events": event_count,
                        "is_published": is_published,
                    }

                    cur.execute(
                        """
                        SELECT name, description, category, is_published, contact_count, list_count, event_count, stats_json, raw_json
                        FROM mautic.mautic_campaigns
                        WHERE campaign_id = %s
                        """,
                        (campaign_id,),
                    )
                    existing = dict(cur.fetchone() or {})
                    existing_signature = json.dumps(existing, sort_keys=True, default=str)
                    incoming_signature = json.dumps(
                        {
                            "name": str((campaign or {}).get("name") or "").strip() or None,
                            "description": str((campaign or {}).get("description") or "").strip() or None,
                            "category": category,
                            "is_published": is_published,
                            "contact_count": contact_count,
                            "list_count": list_count,
                            "event_count": event_count,
                            "stats_json": stats_json,
                            "raw_json": campaign or {},
                        },
                        sort_keys=True,
                        default=str,
                    )

                    if not existing:
                        results["inserted"] += 1
                    elif existing_signature == incoming_signature:
                        results["unchanged"] += 1
                    else:
                        results["updated"] += 1

                    cur.execute(
                        """
                        INSERT INTO mautic.mautic_campaigns (
                            campaign_id, name, description, category, is_published,
                            contact_count, list_count, event_count, stats_json, raw_json,
                            date_added, date_modified, synced_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (campaign_id)
                        DO UPDATE SET
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            category = EXCLUDED.category,
                            is_published = EXCLUDED.is_published,
                            contact_count = EXCLUDED.contact_count,
                            list_count = EXCLUDED.list_count,
                            event_count = EXCLUDED.event_count,
                            stats_json = EXCLUDED.stats_json,
                            raw_json = EXCLUDED.raw_json,
                            date_added = EXCLUDED.date_added,
                            date_modified = EXCLUDED.date_modified,
                            synced_at = EXCLUDED.synced_at,
                            updated_at = now()
                        """,
                        (
                            campaign_id,
                            str((campaign or {}).get("name") or "").strip() or None,
                            str((campaign or {}).get("description") or "").strip() or None,
                            category,
                            is_published,
                            contact_count,
                            list_count,
                            event_count,
                            Json(stats_json),
                            Json(campaign or {}),
                            (campaign or {}).get("dateAdded"),
                            (campaign or {}).get("dateModified"),
                            snapshot_time,
                        ),
                    )
            self.conn.commit()
            return results
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not mirror Mautic campaigns yet: {exc}")
            return results

    def get_mautic_stats(self) -> dict:
        stats = {
            "contact_count": 0,
            "campaign_count": 0,
            "last_sync_status": "planned",
            "last_sync_summary": "No Mautic sync has run yet.",
            "last_synced_at": None,
        }
        if not self.conn:
            return stats

        try:
            self.ensure_mautic_schema()
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT COUNT(*) AS count FROM mautic.mautic_contacts WHERE COALESCE(is_active, TRUE) = TRUE")
                stats["contact_count"] = int((cur.fetchone() or {}).get("count") or 0)

                cur.execute("SELECT COUNT(*) AS count FROM mautic.mautic_campaigns")
                stats["campaign_count"] = int((cur.fetchone() or {}).get("count") or 0)

                cur.execute(
                    """
                    SELECT status, summary, COALESCE(run_finished_at, run_started_at) AS finished_at
                    FROM mautic.sync_runs
                    ORDER BY COALESCE(run_finished_at, run_started_at) DESC NULLS LAST, id DESC
                    LIMIT 1
                    """
                )
                last_run = dict(cur.fetchone() or {})

            if last_run:
                stats["last_sync_status"] = str(last_run.get("status") or "planned")
                stats["last_sync_summary"] = str(last_run.get("summary") or stats["last_sync_summary"])
                stats["last_synced_at"] = self._normalize_timestamp(last_run.get("finished_at"))
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.warning(f"Could not read the Mautic stats yet: {exc}")

        return stats

    def log_service_sync_run(self, service_key: str, status: str, summary: str, details: dict | None = None) -> bool:
        if not self.conn:
            return False

        normalized_key = re.sub(r"[^a-z0-9_]+", "_", str(service_key or "service").strip().lower()).strip("_") or "service"
        self.ensure_service_schema(normalized_key, display_name=normalized_key.title(), description="Service sync history")

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}.sync_runs (run_started_at, run_finished_at, status, summary, details)
                        VALUES (now(), now(), %s, %s, %s)
                        """
                    ).format(sql.Identifier(normalized_key)),
                    (str(status or "info"), str(summary or ""), Json(details or {})),
                )
            self.conn.commit()
            return True
        except Exception as exc:
            self.conn.rollback()
            logger.warning(f"Could not write the {normalized_key} sync log: {exc}")
            return False

    def upsert_n8n_workflows(self, workflows: list[dict]) -> dict:
        results = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "retired": 0}
        if not self.conn or not self.ensure_n8n_schema():
            return results

        seen_ids: set[str] = set()
        snapshot_time = datetime.now(timezone.utc)

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                for workflow in workflows or []:
                    workflow_id = str((workflow or {}).get("id") or (workflow or {}).get("workflowId") or "").strip()
                    if not workflow_id:
                        continue

                    seen_ids.add(workflow_id)
                    results["seen"] += 1
                    name = str((workflow or {}).get("name") or "Untitled workflow").strip() or "Untitled workflow"
                    active = bool((workflow or {}).get("active"))
                    workflow_json = copy.deepcopy(workflow or {})

                    cur.execute(
                        """
                        SELECT id, name, active, workflow_json
                        FROM n8n.n8n_workflows
                        WHERE workflow_id = %s AND is_active = TRUE
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (workflow_id,),
                    )
                    current = dict(cur.fetchone() or {})

                    if not current:
                        cur.execute(
                            """
                            INSERT INTO n8n.n8n_workflows (
                                workflow_id, name, active, workflow_json, valid_from, valid_to, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                            """,
                            (workflow_id, name, active, Json(workflow_json), snapshot_time, datetime.max.replace(tzinfo=timezone.utc)),
                        )
                        results["inserted"] += 1
                        continue

                    changed = (
                        (current.get("name") or "") != name
                        or bool(current.get("active")) != active
                        or (current.get("workflow_json") or {}) != workflow_json
                    )
                    if changed:
                        cur.execute(
                            """
                            UPDATE n8n.n8n_workflows
                            SET valid_to = %s, is_active = FALSE
                            WHERE id = %s
                            """,
                            (snapshot_time, current.get("id")),
                        )
                        cur.execute(
                            """
                            INSERT INTO n8n.n8n_workflows (
                                workflow_id, name, active, workflow_json, valid_from, valid_to, is_active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                            """,
                            (workflow_id, name, active, Json(workflow_json), snapshot_time, datetime.max.replace(tzinfo=timezone.utc)),
                        )
                        results["updated"] += 1
                    else:
                        results["unchanged"] += 1

                if seen_ids:
                    cur.execute(
                        """
                        UPDATE n8n.n8n_workflows
                        SET valid_to = %s, is_active = FALSE
                        WHERE is_active = TRUE AND NOT (workflow_id = ANY(%s))
                        """,
                        (snapshot_time, list(seen_ids)),
                    )
                    results["retired"] = int(cur.rowcount or 0)
                elif workflows == []:
                    cur.execute(
                        """
                        UPDATE n8n.n8n_workflows
                        SET valid_to = %s, is_active = FALSE
                        WHERE is_active = TRUE
                        """,
                        (snapshot_time,),
                    )
                    results["retired"] = int(cur.rowcount or 0)

            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            logger.error(f"Failed to upsert n8n workflows: {exc}")

        return results

    def get_n8n_workflow_snapshot(self, workflow_id: str, snapshot_at=None) -> dict:
        if not self.conn or not workflow_id or not self.ensure_n8n_schema():
            return {}

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                if snapshot_at is None:
                    cur.execute(
                        """
                        SELECT workflow_id, name, active, workflow_json, valid_from, valid_to, is_active
                        FROM n8n.n8n_workflows
                        WHERE workflow_id = %s
                        ORDER BY is_active DESC, valid_from DESC, id DESC
                        LIMIT 1
                        """,
                        (workflow_id,),
                    )
                else:
                    snapshot_dt = coerce_snapshot_datetime(snapshot_at)
                    cur.execute(
                        """
                        SELECT workflow_id, name, active, workflow_json, valid_from, valid_to, is_active
                        FROM n8n.n8n_workflows
                        WHERE workflow_id = %s
                          AND valid_from <= %s
                          AND valid_to > %s
                        ORDER BY valid_from DESC, id DESC
                        LIMIT 1
                        """,
                        (workflow_id, snapshot_dt, snapshot_dt),
                    )
                return dict(cur.fetchone() or {})
        except Exception as exc:
            logger.error(f"Failed to load an n8n workflow snapshot for {workflow_id}: {exc}")
            return {}

    def get_n8n_status(self) -> dict:
        default = {
            "connected": False,
            "current_workflows": 0,
            "enabled_workflows": 0,
            "total_versions": 0,
            "last_synced_at": None,
            "last_sync_status": "planned",
            "last_sync_summary": "No n8n sync has run yet.",
        }

        if not self.conn or not self.ensure_n8n_schema():
            return default

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE is_active = TRUE) AS current_workflows,
                        COUNT(*) FILTER (WHERE is_active = TRUE AND active = TRUE) AS enabled_workflows,
                        COUNT(*) AS total_versions
                    FROM n8n.n8n_workflows
                    """
                )
                stats = dict(cur.fetchone() or {})
                cur.execute(
                    """
                    SELECT run_finished_at, status, summary
                    FROM n8n.sync_runs
                    ORDER BY COALESCE(run_finished_at, run_started_at) DESC, id DESC
                    LIMIT 1
                    """
                )
                last_run = dict(cur.fetchone() or {})
                return {
                    **default,
                    **stats,
                    "connected": True,
                    "last_synced_at": last_run.get("run_finished_at"),
                    "last_sync_status": last_run.get("status") or default["last_sync_status"],
                    "last_sync_summary": last_run.get("summary") or default["last_sync_summary"],
                }
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to fetch n8n module stats: {exc}")
            return default

    def get_stats(self, force_refresh: bool = False, max_age_seconds: float = 15.0) -> dict:
        default = {
            "connected": False,
            "active_pages": 0,
            "pending_push": 0,
            "total_versions": 0,
            "pages_with_media": 0,
            "database_size_text": "Not available yet",
            "last_pull_at": None,
            "last_push_at": None,
            "latest_local_update": None,
            "latest_source_update": None,
        }

        if not self.conn:
            snapshot = {**default, **(self._stats_cache or {})}
            snapshot["connected"] = False
            return snapshot

        if not force_refresh and self._stats_cache is not None and self._cache_is_fresh(
            self._stats_cache_checked_at,
            max_age_seconds,
        ):
            return dict(self._stats_cache)

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE wm.is_active = TRUE) AS active_pages,
                        COUNT(*) FILTER (WHERE wm.is_active = TRUE AND wm.needs_push = TRUE) AS pending_push,
                        COUNT(*) AS total_versions,
                        COUNT(*) FILTER (
                            WHERE wm.is_active = TRUE
                              AND COALESCE(jsonb_array_length(jsonb_safe_array(wm.media_local_paths)), 0) > 0
                        ) AS pages_with_media,
                        NULLIF(
                            GREATEST(
                                COALESCE(MAX(wm.pulled_at) FILTER (WHERE wm.is_active = TRUE), '-infinity'::timestamptz),
                                COALESCE(notion_runs.last_pull_run_at, '-infinity'::timestamptz)
                            ),
                            '-infinity'::timestamptz
                        ) AS last_pull_at,
                        NULLIF(
                            GREATEST(
                                COALESCE(MAX(wm.last_pushed_at) FILTER (WHERE wm.is_active = TRUE), '-infinity'::timestamptz),
                                COALESCE(notion_runs.last_push_run_at, '-infinity'::timestamptz)
                            ),
                            '-infinity'::timestamptz
                        ) AS last_push_at,
                        MAX(wm.updated_at) FILTER (WHERE wm.is_active = TRUE) AS latest_local_update,
                        MAX(wm.source_updated_at) FILTER (WHERE wm.is_active = TRUE) AS latest_source_update,
                        pg_size_pretty(pg_database_size(current_database())) AS database_size_text
                    FROM workspace_mirror AS wm
                    CROSS JOIN (
                        SELECT
                            MAX(COALESCE(run_finished_at, run_started_at)) FILTER (
                                WHERE COALESCE(details ->> 'mode', '') = 'pull'
                            ) AS last_pull_run_at,
                            MAX(COALESCE(run_finished_at, run_started_at)) FILTER (
                                WHERE COALESCE(details ->> 'mode', '') = 'push'
                            ) AS last_push_run_at
                        FROM notion.sync_runs
                    ) AS notion_runs
                    """
                )
                stats = {**default, **dict(cur.fetchone() or {}), "connected": True}
                self._stats_cache = dict(stats)
                self._stats_cache_checked_at = time.monotonic()
                return dict(self._stats_cache)
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Failed to fetch dashboard stats: {e}")
            return {**default, **self.get_cached_stats_snapshot()}

    def get_db_connection_diagnostics(self) -> dict:
        """Return a lightweight snapshot of PostgreSQL connection usage."""
        default = {
            "connected": bool(self.conn),
            "database_name": "",
            "max_connections": 0,
            "reserved_connections": 0,
            "usable_connections": 0,
            "total_connections": 0,
            "current_db_connections": 0,
            "active_connections": 0,
            "idle_connections": 0,
            "usage_percent": 0,
            "high_usage": False,
            "error": "",
        }

        if not self.conn:
            default["error"] = "Database is not connected."
            return default

        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        current_database() AS database_name,
                        current_setting('max_connections')::INT AS max_connections,
                        current_setting('superuser_reserved_connections')::INT AS reserved_connections
                    """
                )
                settings = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT
                        COUNT(*)::INT AS total_connections,
                        COUNT(*) FILTER (WHERE datname = current_database())::INT AS current_db_connections,
                        COUNT(*) FILTER (WHERE state = 'active')::INT AS active_connections,
                        COUNT(*) FILTER (WHERE state = 'idle')::INT AS idle_connections
                    FROM pg_stat_activity
                    """
                )
                counts = dict(cur.fetchone() or {})

            max_connections = int(settings.get("max_connections", 0) or 0)
            reserved_connections = int(settings.get("reserved_connections", 0) or 0)
            usable_connections = max(0, max_connections - reserved_connections)
            total_connections = int(counts.get("total_connections", 0) or 0)
            usage_percent = int((total_connections * 100) / usable_connections) if usable_connections > 0 else 0

            diagnostics = {
                **default,
                **settings,
                **counts,
                "connected": True,
                "usable_connections": usable_connections,
                "usage_percent": usage_percent,
                "high_usage": usage_percent >= 80,
            }
            return diagnostics
        except Exception as exc:
            if self.conn:
                self.conn.rollback()
            logger.warning(f"Could not read PostgreSQL connection diagnostics: {exc}")
            return {
                **default,
                "error": str(exc),
            }
