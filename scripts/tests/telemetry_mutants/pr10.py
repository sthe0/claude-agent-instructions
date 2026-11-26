"""Mutants of PR 10: the index on tasks (epoch) (migration 012) and the session's task rows read
before the write transaction that files its contours (lib/telemetry/sync.py).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_MIGRATION = "scripts/lib/telemetry/migrations/012_tasks_epoch_index.sql"
_SYNC = "scripts/lib/telemetry/sync.py"
_S = "scripts/tests/test_telemetry_sync_scale.py::"
_NO_TASKS = _S + "test_file_session_runs_no_tasks_query_inside_write_transaction"
_AT_SCALE = _S + "test_file_session_at_scale_runs_no_tasks_query_inside_write_transaction"
_KIN = _S + "test_child_session_and_subagents_filed_without_tasks_query_inside_write_transaction"

MUTANTS = [
    {"name": "tasks_epoch_index_dropped", "file": _MIGRATION,
     "anchor": "CREATE INDEX tasks_epoch ON tasks (epoch);",
     "replacement": "SELECT 1;",
     "tests": [_S + "test_tasks_epoch_lookups_use_the_epoch_index",
               _S + "test_file_task_rows_updates_use_the_epoch_index"]},
    {"name": "file_session_task_key_looked_up_inside_transaction", "file": _SYNC,
     "anchor": 'key = _task_key_for(conn, session, moment, fields.get("task_epoch"), opens, task_keys)',
     "replacement": 'key = _task_key_for(conn, session, moment, fields.get("task_epoch"), opens)',
     "tests": [_NO_TASKS, _AT_SCALE]},
    {"name": "put_contour_weight_looked_up_inside_transaction", "file": _SYNC,
     "anchor": "row = _task_row(conn, key[2], task_keys)",
     "replacement": "row = _task_row(conn, key[2])",
     "tests": [_NO_TASKS, _AT_SCALE]},
    {"name": "file_session_outside_miss_unresolved", "file": _SYNC,
     "anchor": "    if task_keys and epoch in task_keys:\n        return task_keys[epoch]\n",
     "replacement": "    if task_keys:\n        return task_keys.get(epoch)\n",
     "tests": [_S + "test_file_session_falls_back_inside_transaction_on_outside_miss"]},
    {"name": "child_session_key_looked_up_inside_transaction", "file": _SYNC,
     "anchor": '(spawn or {}).get("task_epoch"), parent_opens, task_keys)',
     "replacement": '(spawn or {}).get("task_epoch"))',
     "tests": [_KIN]},
    {"name": "subagent_contour_weight_looked_up_inside_transaction", "file": _SYNC,
     "anchor": 'put_contour(conn, contour_id, session, "agent-tool", session_key, task_keys)',
     "replacement": 'put_contour(conn, contour_id, session, "agent-tool", session_key)',
     "tests": [_KIN]},
]
