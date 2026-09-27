-- Local mirror of Notion pages/databases
CREATE TABLE IF NOT EXISTS workspace_mirror (
    id                BIGSERIAL PRIMARY KEY,
    notion_id         TEXT NOT NULL,
    title             TEXT,
    ai_summary        TEXT,
    raw_json          JSONB DEFAULT '{}'::jsonb,
    media_local_paths JSONB DEFAULT '[]'::jsonb,
    content_hash      TEXT DEFAULT '',
    source_updated_at TIMESTAMPTZ,
    pulled_at         TIMESTAMPTZ DEFAULT now(),
    last_pushed_at    TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ DEFAULT now(),
    is_active         BOOLEAN DEFAULT TRUE,
    needs_push        BOOLEAN DEFAULT FALSE
);

-- Idempotent migrations for columns added after initial create
ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT '';
ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS pulled_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS last_pushed_at TIMESTAMPTZ;
ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE workspace_mirror ADD COLUMN IF NOT EXISTS needs_push BOOLEAN DEFAULT FALSE;
ALTER TABLE workspace_mirror ALTER COLUMN media_local_paths SET DEFAULT '[]'::jsonb;
ALTER TABLE workspace_mirror ALTER COLUMN raw_json SET DEFAULT '{}'::jsonb;

UPDATE workspace_mirror SET media_local_paths = '[]'::jsonb WHERE media_local_paths IS NULL;
UPDATE workspace_mirror SET raw_json         = '{}'::jsonb WHERE raw_json IS NULL;
UPDATE workspace_mirror SET content_hash     = ''          WHERE content_hash IS NULL;
UPDATE workspace_mirror SET needs_push       = FALSE       WHERE needs_push IS NULL;
UPDATE workspace_mirror SET updated_at = COALESCE(updated_at, pulled_at, now()) WHERE updated_at IS NULL;
UPDATE workspace_mirror SET pulled_at  = COALESCE(pulled_at, now())             WHERE pulled_at IS NULL;

-- One active row per notion_id
CREATE UNIQUE INDEX IF NOT EXISTS idx_notion_id_active
    ON workspace_mirror(notion_id) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_workspace_mirror_updated_at
    ON workspace_mirror(updated_at DESC);

-- Full-text search index (English stemming)
CREATE INDEX IF NOT EXISTS idx_workspace_mirror_fts
    ON workspace_mirror
    USING GIN (to_tsvector('english',
        COALESCE(title, '') || ' ' || COALESCE(ai_summary, '')
    ))
    WHERE is_active = TRUE;

-- ─── Claude-optimised keyword index ──────────────────────────────────────────
-- Rebuilt after every sync by src/db/indexer.py.
-- Claude calls get_index() to load this whole table in one round-trip, giving
-- it a compact map of all workspace content before deciding what to fetch in
-- full.
CREATE TABLE IF NOT EXISTS page_index (
    notion_id       TEXT PRIMARY KEY,
    keywords        TEXT[]  DEFAULT '{}',
    summary         TEXT    DEFAULT '',
    content_preview TEXT    DEFAULT '',
    page_type       TEXT    DEFAULT 'page',
    token_estimate  INT     DEFAULT 0,
    indexed_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_page_index_keywords
    ON page_index USING GIN(keywords);

-- ─── Clean up views/functions from previous schema versions (run FIRST) ───────
DROP VIEW     IF EXISTS workspace_catalog         CASCADE;
DROP VIEW     IF EXISTS workspace_catalog_stats   CASCADE;
DROP VIEW     IF EXISTS workspace_page_context    CASCADE;
DROP FUNCTION IF EXISTS search_workspace_catalog(TEXT, INT);
DROP FUNCTION IF EXISTS get_workspace_page_context(TEXT);

-- ─── Helpers ─────────────────────────────────────────────────────────────────
-- Drop before recreating so PostgreSQL allows parameter renames
DROP FUNCTION IF EXISTS jsonb_safe_array(JSONB);
DROP FUNCTION IF EXISTS jsonb_safe_object(JSONB);

CREATE OR REPLACE FUNCTION jsonb_safe_array(v JSONB)
RETURNS JSONB LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(v) = 'array' THEN v ELSE '[]'::jsonb END;
$$;

CREATE OR REPLACE FUNCTION jsonb_safe_object(v JSONB)
RETURNS JSONB LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(v) = 'object' THEN v ELSE '{}'::jsonb END;
$$;

-- ─── Trigger: stamp updated_at on every row update ───────────────────────────
CREATE OR REPLACE FUNCTION workspace_mirror_before_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workspace_mirror_before_update ON workspace_mirror;
CREATE TRIGGER trg_workspace_mirror_before_update
    BEFORE UPDATE ON workspace_mirror
    FOR EACH ROW EXECUTE FUNCTION workspace_mirror_before_update();

-- ─── Search function used by MCP server ──────────────────────────────────────
DROP FUNCTION IF EXISTS search_workspace(TEXT, INT);
CREATE OR REPLACE FUNCTION search_workspace(p_query TEXT, p_limit INT DEFAULT 10)
RETURNS TABLE (
    notion_id       TEXT,
    title           TEXT,
    summary_preview TEXT,
    keywords        TEXT[],
    page_type       TEXT,
    token_estimate  INT,
    has_media       BOOLEAN,
    user_notion_url TEXT,
    updated_at      TIMESTAMPTZ,
    match_rank      REAL
) LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_query TEXT := BTRIM(COALESCE(p_query, ''));
BEGIN
    RETURN QUERY
    SELECT
        wm.notion_id,
        COALESCE(NULLIF(BTRIM(wm.title), ''), 'Untitled'),
        LEFT(COALESCE(pi.summary, wm.ai_summary, ''), 300),
        COALESCE(pi.keywords, '{}'::TEXT[]),
        COALESCE(pi.page_type, 'page'),
        COALESCE(pi.token_estimate, 0),
        (COALESCE(jsonb_array_length(jsonb_safe_array(wm.media_local_paths)), 0) > 0),
        COALESCE(wm.raw_json ->> 'url', ''),
        wm.updated_at,
        (
            CASE WHEN LOWER(COALESCE(wm.title,'')) = LOWER(v_query)                        THEN 8.0 ELSE 0 END
          + CASE WHEN LOWER(COALESCE(wm.title,'')) LIKE '%'||LOWER(v_query)||'%'           THEN 3.0 ELSE 0 END
          + CASE WHEN LOWER(COALESCE(wm.ai_summary,'')) LIKE '%'||LOWER(v_query)||'%'      THEN 1.5 ELSE 0 END
          + CASE WHEN v_query = ANY(COALESCE(pi.keywords, '{}'::TEXT[]))                   THEN 2.0 ELSE 0 END
          + COALESCE(ts_rank(
                to_tsvector('english', COALESCE(wm.title,'') || ' ' || COALESCE(wm.ai_summary,'')),
                plainto_tsquery('english', v_query)
            ), 0)
        )::REAL AS match_rank
    FROM workspace_mirror wm
    LEFT JOIN page_index pi ON pi.notion_id = wm.notion_id
    WHERE wm.is_active = TRUE
      AND v_query <> ''
      AND (
             LOWER(COALESCE(wm.title,''))      LIKE '%'||LOWER(v_query)||'%'
          OR LOWER(COALESCE(wm.ai_summary,'')) LIKE '%'||LOWER(v_query)||'%'
          OR v_query = ANY(COALESCE(pi.keywords, '{}'::TEXT[]))
          OR to_tsvector('english', COALESCE(wm.title,'') || ' ' || COALESCE(wm.ai_summary,''))
             @@ plainto_tsquery('english', v_query)
      )
    ORDER BY match_rank DESC, wm.updated_at DESC NULLS LAST
    LIMIT GREATEST(COALESCE(p_limit, 10), 1);
END;
$$;
