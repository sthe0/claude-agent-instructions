-- Migration 9: the merge watermark of each origin (lib/telemetry/portability.py). A row holds the
-- taken_at of the newest export of that origin merged here; an export taken no later replaces none
-- of that origin's rows. Machine-local, never exported.
CREATE TABLE merge_state (
    origin   TEXT PRIMARY KEY,
    taken_at TEXT NOT NULL
);
