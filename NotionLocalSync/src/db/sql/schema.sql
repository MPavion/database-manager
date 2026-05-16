CREATE SCHEMA IF NOT EXISTS n8n;

CREATE TABLE IF NOT EXISTS n8n.n8n_workflows (
    id BIGSERIAL PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    name TEXT,
    active BOOLEAN DEFAULT FALSE,
    workflow_json JSONB DEFAULT '{}'::jsonb,
    valid_from TIMESTAMP WITH TIME ZONE DEFAULT now(),
    valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z',
    is_active BOOLEAN DEFAULT TRUE
);

ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT FALSE;
ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS workflow_json JSONB DEFAULT '{}'::jsonb;
ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP WITH TIME ZONE DEFAULT now();
ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z';
ALTER TABLE n8n.n8n_workflows ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

UPDATE n8n.n8n_workflows SET workflow_json = '{}'::jsonb WHERE workflow_json IS NULL;
UPDATE n8n.n8n_workflows SET is_active = TRUE WHERE is_active IS NULL;
UPDATE n8n.n8n_workflows SET active = FALSE WHERE active IS NULL;

CREATE INDEX IF NOT EXISTS idx_n8n_workflows_workflow_id ON n8n.n8n_workflows(workflow_id);
CREATE INDEX IF NOT EXISTS idx_n8n_workflows_valid_from ON n8n.n8n_workflows(valid_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_n8n_workflows_single_active
    ON n8n.n8n_workflows(workflow_id)
    WHERE is_active = TRUE;

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
    due_date TIMESTAMP WITH TIME ZONE,
    date_created TIMESTAMP WITH TIME ZONE,
    date_updated TIMESTAMP WITH TIME ZONE,
    synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    raw_json JSONB DEFAULT '{}'::jsonb,
    valid_from TIMESTAMP WITH TIME ZONE DEFAULT now(),
    valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z',
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
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS due_date TIMESTAMP WITH TIME ZONE;
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS date_created TIMESTAMP WITH TIME ZONE;
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS date_updated TIMESTAMP WITH TIME ZONE;
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS synced_at TIMESTAMP WITH TIME ZONE DEFAULT now();
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb;
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP WITH TIME ZONE DEFAULT now();
ALTER TABLE clickup.clickup_tasks ADD COLUMN IF NOT EXISTS valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z';
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
    date_created TIMESTAMP WITH TIME ZONE,
    date_updated TIMESTAMP WITH TIME ZONE,
    synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    valid_from TIMESTAMP WITH TIME ZONE DEFAULT now(),
    valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z',
    is_active BOOLEAN DEFAULT TRUE
);

ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS comment_id TEXT;
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS task_id TEXT;
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS comment_text TEXT DEFAULT '';
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS user_json JSONB DEFAULT '{}'::jsonb;
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb;
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS date_created TIMESTAMP WITH TIME ZONE;
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS date_updated TIMESTAMP WITH TIME ZONE;
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS synced_at TIMESTAMP WITH TIME ZONE DEFAULT now();
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP WITH TIME ZONE DEFAULT now();
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS valid_to TIMESTAMP WITH TIME ZONE DEFAULT '9999-12-31 23:59:59Z';
ALTER TABLE clickup.clickup_comments ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

UPDATE clickup.clickup_comments SET user_json = '{}'::jsonb WHERE user_json IS NULL;
UPDATE clickup.clickup_comments SET raw_json = '{}'::jsonb WHERE raw_json IS NULL;
UPDATE clickup.clickup_comments SET is_active = TRUE WHERE is_active IS NULL;

CREATE INDEX IF NOT EXISTS idx_clickup_comments_task_id ON clickup.clickup_comments(task_id);
CREATE INDEX IF NOT EXISTS idx_clickup_comments_valid_from ON clickup.clickup_comments(valid_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clickup_comments_single_active
    ON clickup.clickup_comments(comment_id)
    WHERE is_active = TRUE;

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
CREATE INDEX IF NOT EXISTS idx_workspace_mirror_needs_push ON workspace_mirror(needs_push) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_workspace_mirror_source_updated_at ON workspace_mirror(source_updated_at);
CREATE INDEX IF NOT EXISTS idx_workspace_mirror_updated_at ON workspace_mirror(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_workspace_mirror_catalog_search
    ON workspace_mirror
    USING GIN (
        to_tsvector(
            'simple',
            COALESCE(title, '') || ' ' || COALESCE(ai_summary, '') || ' ' || COALESCE(raw_json::text, '')
        )
    )
    WHERE is_active = TRUE;

-- Keep these helpers replaceable so schema refreshes do not fail while dependent views exist.
CREATE OR REPLACE FUNCTION jsonb_safe_array(input_value JSONB)
RETURNS JSONB AS $$
    SELECT CASE
        WHEN jsonb_typeof(input_value) = 'array' THEN input_value
        ELSE '[]'::jsonb
    END;
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION jsonb_safe_object(input_value JSONB)
RETURNS JSONB AS $$
    SELECT CASE
        WHEN jsonb_typeof(input_value) = 'object' THEN input_value
        ELSE '{}'::jsonb
    END;
$$ LANGUAGE sql IMMUTABLE;

DROP VIEW IF EXISTS workspace_catalog CASCADE;
CREATE OR REPLACE VIEW workspace_catalog AS
SELECT
    wm.notion_id,
    COALESCE(NULLIF(BTRIM(wm.title), ''), 'Untitled') AS title,
    LEFT(REGEXP_REPLACE(COALESCE(wm.ai_summary, ''), '[[:space:]]+', ' ', 'g'), 280) AS summary_preview,
    LENGTH(REGEXP_REPLACE(COALESCE(wm.ai_summary, ''), '[[:space:]]+', ' ', 'g'))::INT AS summary_chars,
    CEIL(
        (
            CHAR_LENGTH(COALESCE(wm.title, ''))
            + CHAR_LENGTH(COALESCE(wm.ai_summary, ''))
        ) / 4.0
    )::INT AS approx_prompt_tokens,
    COALESCE(jsonb_array_length(jsonb_safe_array(wm.media_local_paths)), 0)::INT AS media_count,
    (COALESCE(jsonb_array_length(jsonb_safe_array(wm.media_local_paths)), 0) > 0) AS has_media,
    (
        SELECT COUNT(*)::INT
        FROM jsonb_object_keys(jsonb_safe_object(wm.raw_json -> 'properties')) AS key
    ) AS property_count,
    ARRAY(
        SELECT key
        FROM jsonb_object_keys(jsonb_safe_object(wm.raw_json -> 'properties')) AS key
        ORDER BY key
        LIMIT 12
    ) AS property_keys,
    COALESCE(
        wm.raw_json -> '_business_brain_links' ->> 'internal_resource_uri',
        CASE WHEN COALESCE(NULLIF(BTRIM(wm.notion_id), ''), '') <> '' THEN 'bb://page/' || wm.notion_id ELSE '' END
    ) AS internal_resource_uri,
    COALESCE(
        wm.raw_json -> '_business_brain_links' ->> 'user_notion_url',
        COALESCE(wm.raw_json ->> 'url', '')
    ) AS user_notion_url,
    COALESCE(jsonb_array_length(jsonb_safe_array(wm.raw_json -> '_business_brain_links' -> 'linked_notion_ids')), 0)::INT AS linked_page_count,
    COALESCE(NULLIF(wm.raw_json ->> 'object', ''), 'page') AS source_object,
    wm.source_updated_at,
    wm.pulled_at,
    wm.updated_at,
    wm.needs_push
FROM workspace_mirror wm
WHERE wm.is_active = TRUE;

DROP VIEW IF EXISTS workspace_catalog_stats CASCADE;
CREATE OR REPLACE VIEW workspace_catalog_stats AS
SELECT
    COUNT(*)::INT AS active_pages,
    COALESCE(SUM(CASE WHEN needs_push THEN 1 ELSE 0 END), 0)::INT AS pending_push_pages,
    COALESCE(SUM(CASE WHEN COALESCE(jsonb_array_length(jsonb_safe_array(media_local_paths)), 0) > 0 THEN 1 ELSE 0 END), 0)::INT AS pages_with_media,
    MAX(updated_at) AS latest_local_update,
    MAX(source_updated_at) AS latest_source_update
FROM workspace_mirror
WHERE is_active = TRUE;

DROP VIEW IF EXISTS workspace_page_context CASCADE;
CREATE OR REPLACE VIEW workspace_page_context AS
SELECT
    wm.notion_id,
    COALESCE(NULLIF(BTRIM(wm.title), ''), 'Untitled') AS title,
    COALESCE(wm.ai_summary, '') AS ai_summary,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                prop.key,
                to_jsonb(
                    LEFT(
                        REGEXP_REPLACE(
                            COALESCE(
                                CASE prop.value ->> 'type'
                                    WHEN 'title' THEN (
                                        SELECT string_agg(COALESCE(item ->> 'plain_text', item -> 'text' ->> 'content', ''), '')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'title')) AS item
                                    )
                                    WHEN 'rich_text' THEN (
                                        SELECT string_agg(COALESCE(item ->> 'plain_text', item -> 'text' ->> 'content', ''), '')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'rich_text')) AS item
                                    )
                                    WHEN 'status' THEN COALESCE(prop.value -> 'status' ->> 'name', '')
                                    WHEN 'select' THEN COALESCE(prop.value -> 'select' ->> 'name', '')
                                    WHEN 'multi_select' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'name', ''), ', ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'multi_select')) AS item
                                    ), '')
                                    WHEN 'number' THEN COALESCE(prop.value ->> 'number', '')
                                    WHEN 'checkbox' THEN CASE
                                        WHEN prop.value ->> 'checkbox' IN ('true', 'false')
                                            THEN CASE WHEN (prop.value ->> 'checkbox')::BOOLEAN THEN 'Checked' ELSE 'Unchecked' END
                                        ELSE 'Unchecked'
                                    END
                                    WHEN 'url' THEN COALESCE(prop.value ->> 'url', '')
                                    WHEN 'email' THEN COALESCE(prop.value ->> 'email', '')
                                    WHEN 'phone_number' THEN COALESCE(prop.value ->> 'phone_number', '')
                                    WHEN 'people' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'name', ''), ', ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'people')) AS item
                                    ), '')
                                    WHEN 'relation' THEN COALESCE((
                                        SELECT string_agg(
                                            COALESCE(NULLIF(BTRIM(target.title), ''), item ->> 'id', ''),
                                            ', '
                                            ORDER BY COALESCE(NULLIF(BTRIM(target.title), ''), item ->> 'id', '')
                                        )
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'relation')) AS item
                                        LEFT JOIN workspace_mirror AS target
                                            ON target.is_active = TRUE
                                           AND target.notion_id = item ->> 'id'
                                    ), '')
                                    WHEN 'date' THEN CASE
                                        WHEN COALESCE(prop.value -> 'date' ->> 'start', '') <> '' AND COALESCE(prop.value -> 'date' ->> 'end', '') <> ''
                                            THEN (prop.value -> 'date' ->> 'start') || ' → ' || (prop.value -> 'date' ->> 'end')
                                        ELSE COALESCE(prop.value -> 'date' ->> 'start', '')
                                    END
                                    ELSE COALESCE(prop.value::TEXT, '')
                                END,
                                ''
                            ),
                            '[[:space:]]+',
                            ' ',
                            'g'
                        ),
                        160
                    )
                )
            )
            FROM jsonb_each(jsonb_safe_object(wm.raw_json -> 'properties')) AS prop(key, value)
        ),
        '{}'::jsonb
    ) AS property_preview,
    COALESCE(
        wm.raw_json -> '_business_brain_links' ->> 'internal_resource_uri',
        CASE WHEN COALESCE(NULLIF(BTRIM(wm.notion_id), ''), '') <> '' THEN 'bb://page/' || wm.notion_id ELSE '' END
    ) AS internal_resource_uri,
    COALESCE(
        wm.raw_json -> '_business_brain_links' ->> 'user_notion_url',
        COALESCE(wm.raw_json ->> 'url', '')
    ) AS user_notion_url,
    jsonb_safe_array(wm.raw_json -> '_business_brain_links' -> 'linked_notion_ids') AS linked_notion_ids,
    COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'notion_id', COALESCE(link_item ->> 'notion_id', ''),
                    'title', COALESCE(NULLIF(BTRIM(link_target.title), ''), COALESCE(link_item ->> 'notion_id', '')),
                    'internal_resource_uri', COALESCE(link_item ->> 'internal_resource_uri', ''),
                    'user_notion_url', COALESCE(link_item ->> 'user_notion_url', COALESCE(link_target.raw_json ->> 'url', '')),
                    'source', COALESCE(link_item ->> 'source', 'page_reference')
                )
                ORDER BY COALESCE(NULLIF(BTRIM(link_target.title), ''), COALESCE(link_item ->> 'notion_id', ''))
            )
            FROM jsonb_array_elements(jsonb_safe_array(wm.raw_json -> '_business_brain_links' -> 'linked_resources')) AS link_item
            LEFT JOIN workspace_mirror AS link_target
                ON link_target.is_active = TRUE
               AND link_target.notion_id = COALESCE(link_item ->> 'notion_id', '')
        ),
        '[]'::jsonb
    ) AS internal_links,
    wm.media_local_paths,
    COALESCE(jsonb_array_length(jsonb_safe_array(wm.media_local_paths)), 0)::INT AS media_count,
    (COALESCE(jsonb_array_length(jsonb_safe_array(wm.media_local_paths)), 0) > 0) AS has_media,
    CEIL((CHAR_LENGTH(COALESCE(wm.title, '')) + CHAR_LENGTH(COALESCE(wm.ai_summary, ''))) / 4.0)::INT AS approx_prompt_tokens,
    wm.source_updated_at,
    wm.pulled_at,
    wm.updated_at,
    wm.needs_push
FROM workspace_mirror wm
WHERE wm.is_active = TRUE;

DROP FUNCTION IF EXISTS get_workspace_page_context(TEXT);
CREATE OR REPLACE FUNCTION get_workspace_page_context(page_notion_id TEXT)
RETURNS TABLE (
    notion_id TEXT,
    title TEXT,
    ai_summary TEXT,
    property_preview JSONB,
    internal_resource_uri TEXT,
    user_notion_url TEXT,
    linked_notion_ids JSONB,
    internal_links JSONB,
    media_local_paths JSONB,
    media_count INT,
    has_media BOOLEAN,
    approx_prompt_tokens INT,
    source_updated_at TIMESTAMPTZ,
    pulled_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    needs_push BOOLEAN
) AS $$
    SELECT
        wpc.notion_id,
        wpc.title,
        wpc.ai_summary,
        wpc.property_preview,
        wpc.internal_resource_uri,
        wpc.user_notion_url,
        wpc.linked_notion_ids,
        wpc.internal_links,
        wpc.media_local_paths,
        wpc.media_count,
        wpc.has_media,
        wpc.approx_prompt_tokens,
        wpc.source_updated_at,
        wpc.pulled_at,
        wpc.updated_at,
        wpc.needs_push
    FROM workspace_page_context wpc
    WHERE wpc.notion_id = page_notion_id
    LIMIT 1;
$$ LANGUAGE sql STABLE;

DROP FUNCTION IF EXISTS search_workspace_catalog(TEXT, INT);
CREATE OR REPLACE FUNCTION search_workspace_catalog(search_text TEXT, result_limit INT DEFAULT 10)
RETURNS TABLE (
    notion_id TEXT,
    title TEXT,
    summary_preview TEXT,
    summary_chars INT,
    approx_prompt_tokens INT,
    media_count INT,
    has_media BOOLEAN,
    property_count INT,
    property_keys TEXT[],
    internal_resource_uri TEXT,
    user_notion_url TEXT,
    linked_page_count INT,
    source_object TEXT,
    source_updated_at TIMESTAMPTZ,
    pulled_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    needs_push BOOLEAN,
    match_rank REAL
) AS $$
BEGIN
    RETURN QUERY
    WITH search_candidates AS (
        SELECT
            wm.*,
            COALESCE(NULLIF(BTRIM(wm.title), ''), 'Untitled') AS safe_title,
            COALESCE(wm.ai_summary, '') AS safe_summary,
            COALESCE(
                (
                    SELECT string_agg(
                        NULLIF(
                            BTRIM(
                                prop.key || ' ' ||
                                CASE prop.value ->> 'type'
                                    WHEN 'title' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'plain_text', item -> 'text' ->> 'content', ''), ' ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'title')) AS item
                                    ), '')
                                    WHEN 'rich_text' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'plain_text', item -> 'text' ->> 'content', ''), ' ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'rich_text')) AS item
                                    ), '')
                                    WHEN 'status' THEN COALESCE(prop.value -> 'status' ->> 'name', '')
                                    WHEN 'select' THEN COALESCE(prop.value -> 'select' ->> 'name', '')
                                    WHEN 'multi_select' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'name', ''), ', ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'multi_select')) AS item
                                    ), '')
                                    WHEN 'number' THEN COALESCE(prop.value ->> 'number', '')
                                    WHEN 'checkbox' THEN CASE
                                        WHEN prop.value ->> 'checkbox' IN ('true', 'false')
                                            THEN CASE WHEN (prop.value ->> 'checkbox')::BOOLEAN THEN 'Checked' ELSE 'Unchecked' END
                                        ELSE 'Unchecked'
                                    END
                                    WHEN 'url' THEN COALESCE(prop.value ->> 'url', '')
                                    WHEN 'email' THEN COALESCE(prop.value ->> 'email', '')
                                    WHEN 'phone_number' THEN COALESCE(prop.value ->> 'phone_number', '')
                                    WHEN 'people' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'name', item ->> 'id', ''), ', ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'people')) AS item
                                    ), '')
                                    WHEN 'relation' THEN COALESCE((
                                        SELECT string_agg(COALESCE(item ->> 'id', ''), ', ')
                                        FROM jsonb_array_elements(jsonb_safe_array(prop.value -> 'relation')) AS item
                                    ), '')
                                    WHEN 'date' THEN CASE
                                        WHEN COALESCE(prop.value -> 'date' ->> 'start', '') <> '' AND COALESCE(prop.value -> 'date' ->> 'end', '') <> ''
                                            THEN (prop.value -> 'date' ->> 'start') || ' → ' || (prop.value -> 'date' ->> 'end')
                                        ELSE COALESCE(prop.value -> 'date' ->> 'start', '')
                                    END
                                    ELSE COALESCE(prop.value::TEXT, '')
                                END
                            ),
                            ''
                        ),
                        ' '
                        ORDER BY prop.key
                    )
                    FROM jsonb_each(jsonb_safe_object(wm.raw_json -> 'properties')) AS prop(key, value)
                ),
                ''
            ) AS property_search_text
        FROM workspace_mirror wm
        WHERE wm.is_active = TRUE
    )
    SELECT
        search_candidates.notion_id,
        search_candidates.safe_title AS title,
        LEFT(REGEXP_REPLACE(search_candidates.safe_summary, '[[:space:]]+', ' ', 'g'), 280) AS summary_preview,
        LENGTH(REGEXP_REPLACE(search_candidates.safe_summary, '[[:space:]]+', ' ', 'g'))::INT AS summary_chars,
        CEIL((CHAR_LENGTH(search_candidates.safe_title) + CHAR_LENGTH(search_candidates.safe_summary)) / 4.0)::INT AS approx_prompt_tokens,
        COALESCE(jsonb_array_length(jsonb_safe_array(search_candidates.media_local_paths)), 0)::INT AS media_count,
        (COALESCE(jsonb_array_length(jsonb_safe_array(search_candidates.media_local_paths)), 0) > 0) AS has_media,
        (
            SELECT COUNT(*)::INT
            FROM jsonb_object_keys(jsonb_safe_object(search_candidates.raw_json -> 'properties')) AS key
        ) AS property_count,
        ARRAY(
            SELECT key
            FROM jsonb_object_keys(jsonb_safe_object(search_candidates.raw_json -> 'properties')) AS key
            ORDER BY key
            LIMIT 12
        ) AS property_keys,
        COALESCE(
            search_candidates.raw_json -> '_business_brain_links' ->> 'internal_resource_uri',
            CASE WHEN COALESCE(NULLIF(BTRIM(search_candidates.notion_id), ''), '') <> '' THEN 'bb://page/' || search_candidates.notion_id ELSE '' END
        ) AS internal_resource_uri,
        COALESCE(
            search_candidates.raw_json -> '_business_brain_links' ->> 'user_notion_url',
            COALESCE(search_candidates.raw_json ->> 'url', '')
        ) AS user_notion_url,
        COALESCE(jsonb_array_length(jsonb_safe_array(search_candidates.raw_json -> '_business_brain_links' -> 'linked_notion_ids')), 0)::INT AS linked_page_count,
        COALESCE(NULLIF(search_candidates.raw_json ->> 'object', ''), 'page') AS source_object,
        search_candidates.source_updated_at,
        search_candidates.pulled_at,
        search_candidates.updated_at,
        search_candidates.needs_push,
        CASE
            WHEN COALESCE(BTRIM(search_text), '') = '' THEN 0::REAL
            ELSE (
                CASE WHEN LOWER(search_candidates.safe_title) = LOWER(BTRIM(search_text)) THEN 8.0 ELSE 0 END
                + CASE WHEN LOWER(search_candidates.safe_title) LIKE LOWER(BTRIM(search_text)) || '%' THEN 4.0 ELSE 0 END
                + CASE WHEN LOWER(search_candidates.safe_title) LIKE '%' || LOWER(BTRIM(search_text)) || '%' THEN 2.0 ELSE 0 END
                + CASE WHEN LOWER(search_candidates.safe_summary) LIKE '%' || LOWER(BTRIM(search_text)) || '%' THEN 1.25 ELSE 0 END
                + CASE WHEN LOWER(search_candidates.property_search_text) LIKE '%' || LOWER(BTRIM(search_text)) || '%' THEN 0.9 ELSE 0 END
                + COALESCE(
                    ts_rank_cd(
                        to_tsvector(
                            'simple',
                            search_candidates.safe_title || ' ' || search_candidates.safe_summary || ' ' || search_candidates.property_search_text
                        ),
                        plainto_tsquery('simple', BTRIM(search_text))
                    ),
                    0
                )
            )::REAL
        END AS match_rank
    FROM search_candidates
    WHERE COALESCE(BTRIM(search_text), '') = ''
       OR LOWER(search_candidates.safe_title) LIKE '%' || LOWER(BTRIM(search_text)) || '%'
       OR LOWER(search_candidates.safe_summary) LIKE '%' || LOWER(BTRIM(search_text)) || '%'
       OR LOWER(search_candidates.property_search_text) LIKE '%' || LOWER(BTRIM(search_text)) || '%'
       OR to_tsvector(
            'simple',
            search_candidates.safe_title || ' ' || search_candidates.safe_summary || ' ' || search_candidates.property_search_text
          ) @@ plainto_tsquery('simple', BTRIM(search_text))
    ORDER BY match_rank DESC, search_candidates.updated_at DESC NULLS LAST
    LIMIT GREATEST(COALESCE(result_limit, 10), 1);
END;
$$ LANGUAGE plpgsql STABLE;

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
