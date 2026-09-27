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
    title           TEXT    DEFAULT '',
    keywords        TEXT[]  DEFAULT '{}',
    summary         TEXT    DEFAULT '',
    content_preview TEXT    DEFAULT '',
    page_type       TEXT    DEFAULT 'page',
    token_estimate  INT     DEFAULT 0,
    indexed_at      TIMESTAMPTZ DEFAULT now()
);

-- Idempotent migration for title column added after initial create
ALTER TABLE page_index ADD COLUMN IF NOT EXISTS title TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_page_index_keywords
    ON page_index USING GIN(keywords);

-- GIN full-text index over page_index (title + summary + content_preview)
-- Used by the optimised search_workspace function below
CREATE INDEX IF NOT EXISTS idx_page_index_fts
    ON page_index
    USING GIN (to_tsvector('english',
        COALESCE(title, '') || ' ' || COALESCE(summary, '') || ' ' || COALESCE(content_preview, '')
    ));

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
-- Optimised: uses UNION CTE so each branch can use its own GIN index independently.
-- Branch 1: FTS via idx_page_index_fts (sub-millisecond)
-- Branch 2: keyword exact match via idx_page_index_keywords (sub-millisecond)
-- Branch 3: title LIKE (short column only, small seq scan)
-- Scoring runs only over matched candidates, not the full table.
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
    v_query TEXT    := BTRIM(LOWER(COALESCE(p_query, '')));
    v_tsq   tsquery;
BEGIN
    BEGIN
        v_tsq := plainto_tsquery('english', v_query);
    EXCEPTION WHEN OTHERS THEN
        v_tsq := NULL;
    END;

    IF v_query = '' THEN RETURN; END IF;

    RETURN QUERY
    WITH candidates AS (
        -- Branch 1: FTS (uses idx_page_index_fts GIN)
        SELECT fts.notion_id FROM page_index fts
        WHERE v_tsq IS NOT NULL
          AND to_tsvector('english',
                  COALESCE(fts.title,'') || ' ' ||
                  COALESCE(fts.summary,'') || ' ' ||
                  COALESCE(fts.content_preview,'')
              ) @@ v_tsq

        UNION

        -- Branch 2: keyword exact match (uses idx_page_index_keywords GIN)
        SELECT kw.notion_id FROM page_index kw
        WHERE kw.keywords @> ARRAY[v_query]::TEXT[]

        UNION

        -- Branch 3: title substring (short column, fast seq scan)
        SELECT tl.notion_id FROM page_index tl
        WHERE LOWER(COALESCE(tl.title,'')) LIKE '%'||v_query||'%'
    )
    SELECT
        wm.notion_id,
        COALESCE(NULLIF(BTRIM(pi.title),''), NULLIF(BTRIM(wm.title),''), 'Untitled'),
        LEFT(COALESCE(pi.summary,''), 300),
        COALESCE(pi.keywords, '{}'::TEXT[]),
        COALESCE(pi.page_type, 'page'),
        COALESCE(pi.token_estimate, 0),
        (COALESCE(jsonb_array_length(
            CASE WHEN jsonb_typeof(wm.media_local_paths) = 'array'
                 THEN wm.media_local_paths ELSE '[]'::jsonb END
        ), 0) > 0),
        COALESCE(wm.raw_json ->> 'url', ''),
        wm.updated_at,
        (
            CASE WHEN LOWER(COALESCE(pi.title, wm.title,'')) = v_query              THEN 8.0 ELSE 0 END
          + CASE WHEN LOWER(COALESCE(pi.title, wm.title,'')) LIKE '%'||v_query||'%' THEN 3.0 ELSE 0 END
          + CASE WHEN LOWER(COALESCE(pi.summary,'')) LIKE '%'||v_query||'%'         THEN 1.5 ELSE 0 END
          + CASE WHEN pi.keywords @> ARRAY[v_query]::TEXT[]                         THEN 2.0 ELSE 0 END
          + CASE WHEN v_tsq IS NOT NULL
                  AND to_tsvector('english',
                          COALESCE(pi.title,'') || ' ' ||
                          COALESCE(pi.summary,'') || ' ' ||
                          COALESCE(pi.content_preview,'')
                      ) @@ v_tsq                                                    THEN 1.0 ELSE 0 END
        )::REAL AS match_rank
    FROM candidates c
    JOIN page_index pi ON pi.notion_id = c.notion_id
    JOIN workspace_mirror wm ON wm.notion_id = c.notion_id AND wm.is_active = TRUE
    ORDER BY match_rank DESC, wm.updated_at DESC NULLS LAST
    LIMIT GREATEST(COALESCE(p_limit, 10), 1);
END;
$$;
