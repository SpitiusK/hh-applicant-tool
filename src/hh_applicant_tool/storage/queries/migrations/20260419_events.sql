BEGIN;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    negotiation_id INTEGER,
    vacancy_id INTEGER,
    type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    when_ts DATETIME,
    source_msg_id TEXT,
    raw_text TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'detected',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_type_created
    ON events(type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_negotiation_id
    ON events(negotiation_id);
CREATE INDEX IF NOT EXISTS idx_events_status_when
    ON events(status, when_ts);
COMMIT;
