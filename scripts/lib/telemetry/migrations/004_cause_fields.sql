-- Migration 4: the structured cause of a difficulty. One row per critique: the key is the
-- critique event's id for a live row (a fixture row of the importer carries its own id), so two
-- critiques of one difficulty are two rows. Each enumerated field stores the version of its table
-- it was validated against; `origin` is the record origin the row was written under.
CREATE TABLE causes (
    cause_id                       TEXT PRIMARY KEY,
    session                        TEXT NOT NULL,
    difficulty_index               INTEGER,
    cause_category                 TEXT NOT NULL,
    cause_category_table_version   INTEGER NOT NULL,
    machinery_induced              INTEGER NOT NULL CHECK (machinery_induced IN (0, 1)),
    mechanism                      TEXT NOT NULL,
    mechanism_table_version        INTEGER NOT NULL,
    functional_place               TEXT NOT NULL,
    functional_place_table_version INTEGER NOT NULL,
    cause_note                     TEXT,
    origin                         TEXT NOT NULL
);
