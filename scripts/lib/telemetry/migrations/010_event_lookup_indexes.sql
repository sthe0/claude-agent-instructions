-- Migration 10 (004-009 are reserved by later stages): indexes for the reads the sync makes at
-- every session end. Without them, latency_marks and events_of_task scan the whole events table
-- (5.3M rows on the reference machine) inside or right before the sync's write transaction.
-- The expressions are spelled exactly as in sync.py, or SQLite will not use the index.
-- A row whose fields is not valid JSON fails the expression-index build; the record is checked
-- with json_valid before this migration is applied to it.

CREATE INDEX events_kind_time ON events (kind, time);
CREATE INDEX events_fields_session ON events (json_extract(fields, '$.session'));
CREATE INDEX events_fields_task_epoch ON events (json_extract(fields, '$.task_epoch'));
