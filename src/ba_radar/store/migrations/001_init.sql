-- Initial schema.
--
-- Excerpts are deliberately absent: they live in memory for the duration of a run and
-- are never persisted (answers doc §0). That keeps the committed SQLite file small
-- enough to live in git and means no copyrighted source text is retained at rest.

CREATE TABLE runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                  TEXT    NOT NULL,
    started_at            TEXT    NOT NULL,
    finished_at           TEXT,
    status                TEXT    NOT NULL,
    collected_count       INTEGER NOT NULL DEFAULT 0,
    delivered_count       INTEGER NOT NULL DEFAULT 0,
    -- Set only after Telegram confirms the send. A run with items marked delivered
    -- but this column NULL is the crash window catchup.yml looks for (Q14).
    telegram_confirmed_at TEXT,
    log                   TEXT    NOT NULL DEFAULT '[]'
);

CREATE INDEX idx_runs_kind_started ON runs (kind, started_at);

CREATE TABLE items (
    id                 TEXT    PRIMARY KEY,           -- sha256 of the canonical URL
    url                TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    title_key          TEXT    NOT NULL,              -- normalised, for the 14-day title match
    category           TEXT    NOT NULL,
    indicator          TEXT    NOT NULL,
    published_at       TEXT    NOT NULL,
    collected_at       TEXT    NOT NULL,
    source_count       INTEGER NOT NULL DEFAULT 1,
    -- PRD req. 2.1.6 says to add "the source weight", but an item can come from
    -- several sources. We keep the highest weight of any source it was seen in.
    -- See DECISIONS.md D-04.
    max_source_weight  INTEGER NOT NULL DEFAULT 1,
    relevance_score    INTEGER,
    tags               TEXT    NOT NULL DEFAULT '[]',
    priority           TEXT,
    suggested_priority TEXT,                          -- model's advisory value (Q8)
    summary            TEXT,
    ba_insight         TEXT,
    action             TEXT,
    status             TEXT    NOT NULL,
    verbatim_flag      INTEGER NOT NULL DEFAULT 0,
    delivered_run_id   INTEGER REFERENCES runs (id),
    delivered_at       TEXT,
    pruned             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_items_title_key   ON items (title_key, collected_at);
CREATE INDEX idx_items_status      ON items (status, collected_at);
CREATE INDEX idx_items_delivered   ON items (delivered_run_id);
CREATE INDEX idx_items_collected   ON items (collected_at);

-- An item can be seen in several sources; source_count drives the multi-source bonus
-- applied during scoring in Stage 2 (Q7 step 5, Q13).
CREATE TABLE item_sources (
    item_id       TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (item_id, source_id)
);

CREATE INDEX idx_item_sources_source ON item_sources (source_id);

CREATE TABLE source_state (
    source_id            TEXT PRIMARY KEY,
    last_success_at      TEXT,
    last_seen_at         TEXT,       -- newest published_at seen; the incremental cursor
    etag                 TEXT,
    last_modified        TEXT,
    cursor               TEXT,       -- method-private (release id, commit sha, content hash)
    block_text           TEXT,       -- html_diff previous block text, Stage 3
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);
