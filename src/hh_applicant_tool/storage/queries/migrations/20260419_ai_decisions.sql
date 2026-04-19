BEGIN;
CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    vacancy_id INTEGER,
    negotiation_id INTEGER,
    prompt_hash TEXT,
    model TEXT,
    confidence REAL,
    escalated INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    is_sentinel INTEGER NOT NULL DEFAULT 0,
    iterations INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    result_preview TEXT,
    sample_for_review INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0,
    flag_reason TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_decisions_operation_created
    ON ai_decisions(operation, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_status_created
    ON ai_decisions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_is_sentinel
    ON ai_decisions(is_sentinel);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_sample_flagged
    ON ai_decisions(sample_for_review, flagged);
COMMIT;
