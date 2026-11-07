"""Mutants of the telemetry store, emitter, tables and their test harness.

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_STORE_FILE = "scripts/lib/telemetry/store.py"
_EMIT_FILE = "scripts/lib/telemetry/emit.py"
_TABLES_FILE = "scripts/lib/telemetry/tables.py"
_SQL_FILE = "scripts/lib/telemetry/migrations/001_r1_tables.sql"
_STORE_TESTS = "scripts/tests/test_telemetry_store.py::"
_TABLES_TESTS = "scripts/tests/test_telemetry_tables.py::"
_HOOK_TESTS = "scripts/tests/test_telemetry_hook_isolation.py::"
_CATALOGUE_TESTS = "scripts/tests/test_telemetry_mutation_catalogue.py::"

_EXACTLY_ONCE = _STORE_TESTS + "test_replay_is_exactly_once_under_retried_concurrent_and_interrupted_delivery"
_LOCK_DEADLINE = _STORE_TESTS + "test_lock_wait_and_busy_wait_share_one_deadline_then_deferred"
_MIGRATION_1 = _STORE_TESTS + "test_migration_1_creates_the_r1_tables_and_columns"
_LIVE_WINS = _STORE_TESTS + "test_live_event_replayed_after_import_deletes_the_imported_row"
_ONE_ID = _STORE_TESTS + "test_imported_row_id_is_the_one_id_function_of_replay_and_importer"

MUTANTS = [
    {"name": "replay_plain_insert", "file": _STORE_FILE,
     "anchor": '"INSERT OR IGNORE INTO events (event_id, session_id, kind, source_key, provenance, time, "',
     "replacement": '"INSERT INTO events (event_id, session_id, kind, source_key, provenance, time, "',
     "tests": [_EXACTLY_ONCE]},
    {"name": "hook_imports_store", "file": "scripts/hook-engine-start.py",
     "anchor": "from lib import config_root, hook_telemetry  # noqa: E402",
     "replacement": "from lib import config_root, hook_telemetry  # noqa: E402\n"
                    "from lib.telemetry import store  # noqa: E402,F401",
     "tests": [_HOOK_TESTS + "test_no_hook_entry_point_imports_sqlite3_or_the_writer"]},
    {"name": "unbounded_lock_wait", "file": _STORE_FILE,
     "anchor": "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
     "replacement": "fcntl.flock(fd, fcntl.LOCK_EX)",
     "tests": [_LOCK_DEADLINE]},
    {"name": "emit_raises", "file": _EMIT_FILE,
     "anchor": "        _mark_drop(stem, type(exc).__name__)\n        return None",
     "replacement": "        _mark_drop(stem, type(exc).__name__)\n        raise",
     "tests": [_STORE_TESTS + "test_emit_failure_never_raises_and_is_counted"]},
    {"name": "record_mode_0644", "file": _STORE_FILE,
     "anchor": "RECORD_MODE = 0o600",
     "replacement": "RECORD_MODE = 0o644",
     "tests": [_STORE_TESTS + "test_record_created_owner_only_under_config_root"]},
    {"name": "role_literal_in_code", "file": _STORE_FILE,
     "anchor": "prepared[version_column] = tables.validate(table_name, prepared[column])",
     "replacement": 'if prepared[column] not in ("root", "linked-child", "spawned-child", "judge", "agent-tool"):\n'
                    "                raise tables.TableError(column)\n"
                    "            prepared[version_column] = 1",
     "tests": [_TABLES_TESTS + "test_every_gating_check_reads_its_table"]},
    {"name": "store_failure_raises", "file": _STORE_FILE,
     "anchor": "    except Exception:\n        return on_failed()",
     "replacement": "    except Exception:\n        raise",
     "tests": [_STORE_TESTS + "test_store_failure_never_raises_and_keeps_the_spool"]},
    {"name": "torn_line_glued", "file": _EMIT_FILE,
     "anchor": r'line = b"\n" + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"',
     "replacement": r'line = json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"',
     "tests": [_STORE_TESTS + "test_torn_line_followed_by_append_loses_only_the_torn_event"]},
    {"name": "deadline_not_shared", "file": _STORE_FILE,
     "anchor": "def _begin(conn: sqlite3.Connection, deadline: _Deadline) -> None:\n"
               "    _set_busy(conn, deadline.busy_ms())",
     "replacement": "def _begin(conn: sqlite3.Connection, deadline: _Deadline) -> None:\n"
                    "    _set_busy(conn, 250)",
     "tests": [_LOCK_DEADLINE]},
    {"name": "origin_regenerated_on_open", "file": _STORE_FILE,
     "anchor": "origin = _side_file_origin(side) or str(uuid.uuid4())",
     "replacement": "origin = str(uuid.uuid4())",
     "tests": [_STORE_TESTS + "test_origin_id_created_once_and_kept_in_side_file"]},
    {"name": "offset_outside_transaction", "file": _STORE_FILE,
     "anchor": '        _upsert_offset(conn, spool, generation, new_offset)\n        conn.execute("COMMIT")',
     "replacement": '        conn.execute("COMMIT")\n        _upsert_offset(conn, spool, generation, new_offset)',
     "tests": [_STORE_TESTS + "test_prefix_inserts_and_offset_commit_together"]},
    {"name": "torn_counted_twice", "file": _STORE_FILE,
     "anchor": "            torn += 1",
     "replacement": "            torn += 2",
     "tests": [_STORE_TESTS + "test_newline_terminated_unparsable_line_counted_once_as_torn"]},
    {"name": "no_skip_option_ignored", "file": "scripts/tests/conftest.py",
     "anchor": "self.enabled = bool(config.getoption(NO_SKIP_OPTION))",
     "replacement": "self.enabled = False",
     "tests": [_CATALOGUE_TESTS + "test_no_skip_option_fails_a_run_with_a_skipped_test"]},
    {"name": "migration_1_missing_column", "file": _SQL_FILE,
     "anchor": "    synthetic    INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0, 1)),\n",
     "replacement": "",
     "tests": [_MIGRATION_1]},
    {"name": "migration_1_missing_contours_table", "file": _SQL_FILE,
     "anchor": "CREATE TABLE contours (\n"
               "    contour_id         TEXT PRIMARY KEY,\n"
               "    parent_session     TEXT,\n"
               "    role               TEXT,\n"
               "    role_table_version INTEGER,\n"
               "    weight_class       TEXT,\n"
               "    task_origin        TEXT,\n"
               "    task_id            TEXT,\n"
               "    task_epoch         TEXT\n"
               ");\n"
               "CREATE INDEX contours_task ON contours (task_origin, task_id, task_epoch);\n",
     "replacement": "",
     "tests": [_MIGRATION_1]},
    {"name": "generation_hashes_empty_first_line", "file": _STORE_FILE,
     "anchor": "        if line:\n            return hashlib.sha256(line).hexdigest()",
     "replacement": "        if True:\n            return hashlib.sha256(line).hexdigest()",
     "tests": [_STORE_TESTS + "test_spool_generation_change_restarts_at_offset_zero_without_duplicates"]},
    {"name": "offset_beyond_size_not_reset", "file": _STORE_FILE,
     "anchor": "stored[1] <= size",
     "replacement": "True",
     "tests": [_STORE_TESTS + "test_stored_offset_beyond_size_restarts_at_offset_zero"]},
    {"name": "table_version_not_bumped_on_retire", "file": _TABLES_FILE,
     "anchor": "    version = table.version + 1\n"
               "    members = dict(table.members)\n"
               "    members[member] = Member(member, current.since, version)",
     "replacement": "    version = table.version\n"
                    "    members = dict(table.members)\n"
                    "    members[member] = Member(member, current.since, version)",
     "tests": [_TABLES_TESTS +
               "test_table_version_bumped_on_every_add_or_retire_and_recorded_in_snapshot_history"]},
    {"name": "replay_keeps_imported_row_on_live_arrival", "file": _STORE_FILE,
     "anchor": "\"DELETE FROM events WHERE event_id = ? AND provenance = 'imported'\"",
     "replacement": "\"DELETE FROM events WHERE event_id = ? AND provenance = 'never'\"",
     "tests": [_LIVE_WINS]},
    {"name": "imported_row_id_differs_from_replay_backref", "file": _STORE_FILE,
     "anchor": 'backref_id = imported_row_id(backref["path"], backref["offset"], backref["sha256"], origin=origin)',
     "replacement": 'backref_id = imported_row_id(backref["path"], backref["offset"] + 1, backref["sha256"], '
                    "origin=origin)",
     "tests": [_LIVE_WINS, _ONE_ID]},
    {"name": "telemetry_dirs_created_with_umask_mode", "file": _EMIT_FILE,
     "anchor": "        os.chmod(directory, PRIVATE_DIR_MODE)",
     "replacement": "        pass",
     "tests": [_STORE_TESTS + "test_first_emit_creates_telemetry_and_spool_dirs_with_mode_0700"]},
    {"name": "replay_drops_event_fields", "file": _STORE_FILE,
     "anchor": 'json.dumps(fields, separators=(",", ":"))',
     "replacement": '"{}"',
     "tests": [_STORE_TESTS + "test_emitted_fields_survive_replay_in_the_fields_column"]},
    {"name": "insert_rows_reports_deferral_as_inserted", "file": _STORE_FILE,
     "anchor": 'outcome["deferred"] = len(rows)',
     "replacement": 'outcome["inserted"] = len(rows)',
     "tests": [_STORE_TESTS + "test_insert_rows_returns_inserted_deferred_failed"]},
    {"name": "replay_leaves_child_session_id_null", "file": _STORE_FILE,
     "anchor": 'child_session = fields.get("child_session_id")',
     "replacement": "child_session = None",
     "tests": [_LIVE_WINS]},
    {"name": "replay_all_or_nothing", "file": _STORE_FILE,
     "anchor": "PREFIX_LINES = 500",
     "replacement": "PREFIX_LINES = 1_000_000_000",
     "tests": [_STORE_TESTS + "test_spool_too_large_for_one_deadline_drains_over_successive_calls"]},
    {"name": "replay_reports_drained_with_lines_left", "file": _STORE_FILE,
     "anchor": '            result["deferred"] = True\n            return',
     "replacement": '            result["deferred"] = True\n            result["drained"] = True\n            return',
     "tests": [_STORE_TESTS + "test_replay_stopped_at_deadline_returns_not_drained"]},
]
