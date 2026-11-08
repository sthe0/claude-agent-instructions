-- Migration 1: the R1 tables of the telemetry record. Later stages add numbered files
-- and never edit this one. The artifacts text index (FTS5, or a plain table when the
-- probe fails) is created by store.py beside this file, at record creation.

CREATE TABLE tasks (
    origin       TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    epoch        TEXT NOT NULL,
    status       TEXT,
    weight_class TEXT,
    synthetic    INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0, 1)),
    opened_at    TEXT,
    ended_at     TEXT,
    PRIMARY KEY (origin, task_id, epoch)
);

CREATE TABLE sessions (
    session_id         TEXT PRIMARY KEY,
    origin             TEXT,
    parent_session     TEXT,
    role               TEXT,
    role_table_version INTEGER,
    depth              INTEGER,
    core_head          TEXT,
    dirty              INTEGER,
    harness_version    TEXT,
    task_origin        TEXT,
    task_id            TEXT,
    task_epoch         TEXT,
    started_at         TEXT
);
CREATE INDEX sessions_task ON sessions (task_origin, task_id, task_epoch);

CREATE TABLE contours (
    contour_id         TEXT PRIMARY KEY,
    parent_session     TEXT,
    role               TEXT,
    role_table_version INTEGER,
    weight_class       TEXT,
    task_origin        TEXT,
    task_id            TEXT,
    task_epoch         TEXT
);
CREATE INDEX contours_task ON contours (task_origin, task_id, task_epoch);

CREATE TABLE events (
    event_id         TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    kind             TEXT NOT NULL,
    source_key       TEXT,
    provenance       TEXT NOT NULL,
    time             TEXT NOT NULL,
    backref_id       TEXT,
    child_session_id TEXT,
    fields           TEXT
);
CREATE INDEX events_session ON events (session_id, time);
CREATE INDEX events_backref ON events (backref_id);
CREATE INDEX events_child_session ON events (child_session_id);

CREATE TABLE stages (
    task_origin      TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    task_epoch       TEXT NOT NULL,
    stage_index      INTEGER NOT NULL,
    status           TEXT,
    opened_event_id  TEXT,
    opened_at        TEXT,
    closed_at        TEXT,
    PRIMARY KEY (task_origin, task_id, task_epoch, stage_index)
);

CREATE TABLE difficulties (
    difficulty_id TEXT PRIMARY KEY,
    task_origin   TEXT,
    task_id       TEXT,
    task_epoch    TEXT,
    session_id    TEXT,
    opened_at     TEXT,
    closed_at     TEXT
);

CREATE TABLE reviews (
    review_id  TEXT PRIMARY KEY,
    task_origin TEXT,
    task_id     TEXT,
    task_epoch  TEXT,
    session_id  TEXT,
    verdict     TEXT,
    reviewed_at TEXT
);

CREATE TABLE spawns (
    spawn_id         TEXT PRIMARY KEY,
    parent_session   TEXT,
    child_session_id TEXT,
    role             TEXT,
    role_table_version INTEGER,
    spawned_at       TEXT
);
CREATE INDEX spawns_child ON spawns (child_session_id);

CREATE TABLE token_usage (
    message_id          TEXT PRIMARY KEY,
    session_id          TEXT,
    role                TEXT,
    role_table_version  INTEGER,
    phase               TEXT,
    phase_table_version INTEGER,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    cache_write_tokens  INTEGER,
    ts                  TEXT
);
CREATE INDEX token_usage_session ON token_usage (session_id);

CREATE TABLE compactions (
    compaction_id TEXT PRIMARY KEY,
    session_id    TEXT,
    compacted_at  TEXT,
    pre_tokens    INTEGER,
    post_tokens   INTEGER
);

CREATE TABLE artifacts (
    artifact_id INTEGER PRIMARY KEY,
    sha256     TEXT NOT NULL UNIQUE,
    kind       TEXT,
    task_origin TEXT,
    task_id     TEXT,
    task_epoch  TEXT,
    text       TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE losses (
    session_id              TEXT NOT NULL,
    loss_mode               TEXT NOT NULL,
    loss_mode_table_version INTEGER,
    count                   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, loss_mode)
);

-- Machine-local replay bookkeeping; never exported.
CREATE TABLE spool_offsets (
    spool      TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    byte_offset INTEGER NOT NULL
);
