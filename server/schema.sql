-- SQLite schema for the signal processor.
-- Applied idempotently at startup (CREATE ... IF NOT EXISTS).

PRAGMA journal_mode = WAL;

-- Every captured Discord message, deduped on the Discord snowflake.
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT PRIMARY KEY,          -- Discord snowflake
    channel_id      TEXT NOT NULL,
    author          TEXT,
    content         TEXT NOT NULL DEFAULT '',
    embeds          TEXT NOT NULL DEFAULT '[]', -- JSON array of embed texts
    links           TEXT NOT NULL DEFAULT '[]', -- JSON array of raw hrefs
    reply_parent_id TEXT,                       -- snowflake of replied-to message, if known
    reply_preview   TEXT,                       -- rendered preview text of the parent
    ts              TEXT,                       -- Discord timestamp (ISO 8601), null if not rendered
    ts_approx       INTEGER NOT NULL DEFAULT 0, -- 1 = ts missing, fall back to captured_at
    captured_at     TEXT NOT NULL,              -- when the extension saw it
    received_at     TEXT NOT NULL,              -- when the server ingested it
    edited          INTEGER NOT NULL DEFAULT 0, -- content changed after first capture
    tier            INTEGER,                    -- 1/2/3 classification, NULL = pending
    tier_reason     TEXT,                       -- why the classifier chose that tier
    classified_at   TEXT                        -- NULL = needs (re)classification
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_pending
    ON messages(received_at) WHERE classified_at IS NULL;

-- One row per distinct token we have seen mentioned, keyed chain:address once
-- resolved, or ticker:$FOO while unresolved (aliases merge via alias_of).
CREATE TABLE IF NOT EXISTS tokens (
    token_key        TEXT PRIMARY KEY,          -- e.g. "solana:BrPs..." / "evm:0xb359..." / "ticker:$FOO"
    chain            TEXT,                      -- solana | ethereum | base | bsc | ...
    address          TEXT,
    ticker           TEXT,
    name             TEXT,
    alias_of         TEXT REFERENCES tokens(token_key), -- set when a ticker resolves to a contract
    first_seen_at    TEXT NOT NULL,
    last_enriched_at TEXT,
    enrichment       TEXT                       -- JSON: latest dexscreener/rugcheck/gecko snapshot
);

-- Every (message, token) sighting — the raw material for consensus/velocity.
CREATE TABLE IF NOT EXISTS mentions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL REFERENCES messages(message_id),
    token_key    TEXT NOT NULL,
    kind         TEXT NOT NULL,                 -- contract | ticker | url
    raw          TEXT NOT NULL,                 -- exact matched text
    author       TEXT,                          -- denormalized for fast distinct-author windows
    mentioned_at TEXT NOT NULL,                 -- message ts, or captured_at when ts_approx
    UNIQUE(message_id, token_key, raw)
);
CREATE INDEX IF NOT EXISTS idx_mentions_token_time ON mentions(token_key, mentioned_at);

-- Everything pushed to Telegram, with a dedupe key so one event alerts once.
CREATE TABLE IF NOT EXISTS alerts_sent (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                   -- tier1 | consensus | warning | watchdog | digest
    token_key  TEXT,
    message_id TEXT,
    dedupe_key TEXT UNIQUE,
    sent_at    TEXT NOT NULL,
    payload    TEXT                             -- JSON of the rendered alert
);
CREATE INDEX IF NOT EXISTS idx_alerts_kind_time ON alerts_sent(kind, sent_at);
