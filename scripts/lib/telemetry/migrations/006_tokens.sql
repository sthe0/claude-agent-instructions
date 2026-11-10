-- Token accounting (lib/telemetry/tokens.py). A token row is one message id; the columns after
-- the task key hold the winning line's rank (timestamp, copy or original, session start,
-- transcript path, byte offset), so a better-ranked line read later replaces its attribution.
ALTER TABLE token_usage ADD COLUMN task_origin TEXT;
ALTER TABLE token_usage ADD COLUMN task_id TEXT;
ALTER TABLE token_usage ADD COLUMN task_epoch TEXT;
ALTER TABLE token_usage ADD COLUMN model TEXT;
ALTER TABLE token_usage ADD COLUMN ts_ms INTEGER;
ALTER TABLE token_usage ADD COLUMN copy_rank INTEGER;
ALTER TABLE token_usage ADD COLUMN session_start_ms INTEGER;
ALTER TABLE token_usage ADD COLUMN transcript TEXT;
ALTER TABLE token_usage ADD COLUMN byte_offset INTEGER;
CREATE INDEX token_usage_task ON token_usage (task_epoch);
CREATE INDEX token_usage_transcript ON token_usage (transcript);

ALTER TABLE compactions ADD COLUMN transcript TEXT;

-- One row per new context: a spawned child or Agent-tool subagent, a compaction, a resume.
CREATE TABLE context_births (
    birth_id    TEXT PRIMARY KEY,
    session_id  TEXT,
    kind        TEXT NOT NULL CHECK (kind IN ('spawn', 'compaction', 'resume')),
    born_at     TEXT,
    transcript  TEXT,
    task_origin TEXT,
    task_id     TEXT,
    task_epoch  TEXT
);
CREATE INDEX context_births_session ON context_births (session_id);

-- One row per judge-ledger call line; ledger_row_id is the id the line's judge event names.
CREATE TABLE judge_usage (
    usage_id            TEXT PRIMARY KEY,
    ledger_row_id       TEXT,
    session_id          TEXT,
    judge               TEXT,
    ts                  TEXT,
    has_usage           INTEGER NOT NULL CHECK (has_usage IN (0, 1)),
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    phase               TEXT,
    task_origin         TEXT,
    task_id             TEXT,
    task_epoch          TEXT
);
CREATE INDEX judge_usage_ledger_row ON judge_usage (ledger_row_id);

CREATE TABLE standing_surface (
    origin       TEXT NOT NULL,
    core_head    TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    static_bytes INTEGER NOT NULL,
    hook_bytes   INTEGER NOT NULL,
    session_id   TEXT,
    measured_at  TEXT,
    UNIQUE (origin, core_head)
);

-- The end of a consumer's last read attempt and how it ended (pending, committed, deferred,
-- failed), and what the tokens consumer carries from one batch to the next.
ALTER TABLE import_offsets ADD COLUMN read_size INTEGER;
ALTER TABLE import_offsets ADD COLUMN read_outcome TEXT;
ALTER TABLE import_offsets ADD COLUMN carry TEXT;
