-- Migration 5: the fields of the historical causes fixture. A fixture row is an imported cause:
-- `imported` is 1, `provenance` names the fixture (`fixture:<stem>`), `fixture_sha256` the content
-- the row was read from and `fixture_row` its index (the row id is derived from both). The basis
-- of the mechanism and of the place is `observed` (named by the aligned critique text) or
-- `category_default` (assigned from the hand category). `alignment_session` and
-- `alignment_difficulty_index` reference the aligned replan, both NULL when the row is unaligned;
-- `interval_minutes` is the rework interval of that replan, NULL (and `interval_unknown` 1)
-- when none is known.
ALTER TABLE causes ADD COLUMN imported INTEGER NOT NULL DEFAULT 0 CHECK (imported IN (0, 1));
ALTER TABLE causes ADD COLUMN provenance TEXT;
ALTER TABLE causes ADD COLUMN fixture_sha256 TEXT;
ALTER TABLE causes ADD COLUMN fixture_row INTEGER;
ALTER TABLE causes ADD COLUMN mechanism_basis TEXT;
ALTER TABLE causes ADD COLUMN place_basis TEXT;
ALTER TABLE causes ADD COLUMN alignment_session TEXT;
ALTER TABLE causes ADD COLUMN alignment_difficulty_index INTEGER;
ALTER TABLE causes ADD COLUMN interval_minutes REAL;
ALTER TABLE causes ADD COLUMN interval_unknown INTEGER NOT NULL DEFAULT 0 CHECK (interval_unknown IN (0, 1));
CREATE INDEX causes_fixture ON causes (provenance, fixture_sha256);
