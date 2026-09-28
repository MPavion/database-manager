CREATE TABLE IF NOT EXISTS workspace_mirror (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    notion_id         TEXT    NOT NULL UNIQUE,
    title             TEXT    NOT NULL DEFAULT '',
    ai_summary        TEXT    NOT NULL DEFAULT '',
    raw_json          TEXT    NOT NULL DEFAULT '{}',
    media_local_paths TEXT    NOT NULL DEFAULT '[]',
    content_hash      TEXT    NOT NULL DEFAULT '',
    source_updated_at TEXT,
    pulled_at         TEXT    DEFAULT (datetime('now')),
    updated_at        TEXT    DEFAULT (datetime('now')),
    is_active         INTEGER NOT NULL DEFAULT 1,
    needs_push        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wm_active    ON workspace_mirror(is_active);
CREATE INDEX IF NOT EXISTS idx_wm_pulled    ON workspace_mirror(pulled_at DESC);
CREATE INDEX IF NOT EXISTS idx_wm_notion    ON workspace_mirror(notion_id, is_active);

CREATE TABLE IF NOT EXISTS page_index (
    notion_id        TEXT    PRIMARY KEY,
    title            TEXT    NOT NULL DEFAULT '',
    keywords         TEXT    NOT NULL DEFAULT '[]',
    summary          TEXT    NOT NULL DEFAULT '',
    page_type        TEXT    NOT NULL DEFAULT 'page',
    content_preview  TEXT    NOT NULL DEFAULT '',
    token_estimate   INTEGER NOT NULL DEFAULT 0,
    indexed_at       TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pi_indexed ON page_index(indexed_at DESC);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    message    TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
