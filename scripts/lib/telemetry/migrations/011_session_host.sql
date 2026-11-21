-- Migration 11: the coordination host a session ran on (claude, cursor, opencode). A session
-- recorded before this migration keeps host NULL, which reads as an unknown host.

ALTER TABLE sessions ADD COLUMN host TEXT;
