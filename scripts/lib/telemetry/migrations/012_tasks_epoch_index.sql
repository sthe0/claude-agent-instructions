-- Migration 12: an index on tasks (epoch). The sync looks task rows up by epoch alone, once per
-- spawn and judge event of a session (_task_key_for, put_contour) and once per command event
-- (_file_task_rows's UPDATEs). The primary key (origin, task_id, epoch) cannot serve a filter on
-- epoch alone, so without this index every such statement scans the whole tasks table, and a
-- session with a thousand spawn and judge events held the write lock for tens of seconds.

CREATE INDEX tasks_epoch ON tasks (epoch);
