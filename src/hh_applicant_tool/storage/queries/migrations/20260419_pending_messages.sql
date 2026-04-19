BEGIN;
CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    messenger_type TEXT NOT NULL,
    messenger_message_id TEXT,
    action_type TEXT NOT NULL,
    draft_payload TEXT NOT NULL,
    draft_history TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    question_for_user TEXT,
    context_summary TEXT,
    confidence REAL,
    escalation_reason TEXT,
    iterations INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dispatched_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_pending_messages_status_created
    ON pending_messages(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_messages_messenger_message_id
    ON pending_messages(messenger_message_id);

CREATE TRIGGER IF NOT EXISTS trg_pending_messages_updated
AFTER UPDATE ON pending_messages
BEGIN
    UPDATE pending_messages
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = OLD.id;
END;
COMMIT;
