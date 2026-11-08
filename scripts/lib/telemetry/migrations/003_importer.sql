-- Migration 3 (migration 2 is reserved by the identity stage): the importer's bookkeeping.
-- Both tables are machine-local and never exported.

-- Where each consumer stopped in each source file. The generation is the sha256 of the file's
-- first newline-terminated line, never the inode, so a rewrite with the same content keeps its
-- offsets; the last line's byte span and sha256 tell a resumed read the bytes before the offset
-- are still the ones it read.
CREATE TABLE import_offsets (
    source      TEXT NOT NULL,
    generation  TEXT NOT NULL,
    consumer    TEXT NOT NULL,
    byte_offset INTEGER NOT NULL,
    last_start  INTEGER NOT NULL,
    last_sha256 TEXT NOT NULL,
    PRIMARY KEY (source, generation, consumer)
);

-- Whether an imported session's HEAD came from the commit log (derived) rather than the
-- quality ledger's own stamp.
CREATE TABLE session_head_marks (
    session_id TEXT PRIMARY KEY,
    derived    INTEGER NOT NULL CHECK (derived IN (0, 1))
);
