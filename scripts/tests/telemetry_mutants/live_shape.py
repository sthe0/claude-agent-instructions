"""Mutants of the record's fit to live source shapes: time readers and empty imported times, the
one act-key function, the session a report run is charged to, judge usage in the ledger and its
coverage check, and the import from a frozen source snapshot (lib/telemetry/snapshot.py).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_IMPORTER = "scripts/lib/telemetry/importer.py"
_SYNC = "scripts/lib/telemetry/sync.py"
_REPORT = "scripts/lib/telemetry/report.py"
_RECORDER = "scripts/lib/telemetry/recorder.py"
_TOKENS = "scripts/lib/telemetry/tokens.py"
_LEDGER = "scripts/lib/judge_ledger.py"
_T = "scripts/tests/test_telemetry_live_shape.py::"
_ONE_FUNCTION = _T + "test_act_key_normalization_is_one_function_used_by_recorder_importer_report"

MUTANTS = [
    {"name": "iso_misses_live_ts_shape", "file": _IMPORTER, "anchor": "        return int(round(number))\n",
     "replacement": "        return None\n",
     "tests": [_T + "test_importer_reads_live_ts_shapes"]},
    {"name": "stored_empty_time_not_filled", "file": _IMPORTER,
     "anchor": "        store.fill_empty_time(conn, event_id, moment)\n", "replacement": "        pass\n",
     "tests": [_T + "test_import_fills_empty_time_of_stored_row_once_ts_is_readable"]},
    {"name": "parse_time_raises_on_empty", "file": _SYNC,
     "anchor": "    if not text:\n        return None\n    return datetime.fromisoformat(text)",
     "replacement": "    return datetime.fromisoformat(text)",
     "tests": [_T + "test_sync_run_tolerates_imported_rows_with_empty_time"]},
    {"name": "latency_marks_reads_empty_time", "file": _SYNC,
     "anchor": "    return known_times((row[0] for row in conn.execute(sql, args)), skipped)",
     "replacement": "    return sorted(parse_time(row[0] or '1970-01-01T00:00:00+00:00') for row in conn.execute(sql, args))",
     "tests": [_T + "test_latency_marks_skip_rows_with_empty_time"]},
    {"name": "act_key_unnormalized_in_recorder", "file": _RECORDER,
     "anchor": '    live.emit_event(session, "command", live.act_key(verb), fields)',
     "replacement": '    live.emit_event(session, "command", live.CLI_PREFIX + verb, fields)',
     "tests": [_ONE_FUNCTION]},
    {"name": "act_key_unnormalized_in_importer", "file": _IMPORTER,
     "anchor": '"command", live.act_key(entry.get("event"))',
     "replacement": '"command", live.CLI_PREFIX + str(entry.get("event"))',
     "tests": [_ONE_FUNCTION]},
    {"name": "act_key_unnormalized_in_report", "file": _REPORT,
     "anchor": 'entry.get("event") and live.act_key(entry["event"]) in command_keys',
     "replacement": 'entry.get("event") and live.CLI_PREFIX + entry["event"] in command_keys',
     "tests": [_ONE_FUNCTION, _T + "test_check_events_matches_hyphen_and_underscore_act_keys"]},
    {"name": "emit_usage_ignores_session_env", "file": _REPORT,
     "anchor": '    env_session = os.environ.get("CLAUDE_CODE_SESSION_ID")\n',
     "replacement": "    env_session = None\n",
     "tests": [_T + "test_report_usage_follows_session_env"]},
    {"name": "emit_usage_ignores_task_session", "file": _REPORT,
     "anchor": "            return task_opener_session(conn, task_id) or REPORT_SOURCE\n",
     "replacement": "            return REPORT_SOURCE\n",
     "tests": [_T + "test_report_usage_reaches_task_session_without_session_env"]},
    {"name": "judge_call_usage_not_recorded", "file": _LEDGER, "anchor": "        usage=counts,\n",
     "replacement": "        usage=None,\n",
     "tests": [_T + "test_judge_ledger_call_records_usage_from_response"]},
    {"name": "usageless_judge_rows_fail_coverage", "file": _TOKENS,
     "anchor": '    ok = conserved and not gaps["missing_judge_call"] and not missing_births\n',
     "replacement": ('    ok = conserved and not gaps["missing_judge_call"] and not missing_births '
                     'and not gaps["judge_rows_without_usage"]\n'),
     "tests": [_T + "test_check_tokens_lists_judge_rows_without_usage_separately"]},
    {"name": "post_change_usageless_line_passes", "file": _TOKENS,
     "anchor": '        elif obj.get("usage_reason"):\n', "replacement": "        elif True:\n",
     "tests": [_T + "test_check_tokens_fails_post_change_judge_line_without_usage"]},
    {"name": "judge_line_with_usage_but_no_row_passes", "file": _TOKENS,
     "anchor": "            if store.imported_row_id(str(path), start, sha, origin) not in rows:\n",
     "replacement": "            if False:\n",
     "tests": [_T + "test_check_tokens_fails_judge_line_with_usage_but_no_row"]},
    {"name": "import_sources_ignored", "file": _IMPORTER, "anchor": "    snapshot.activate(sources)\n",
     "replacement": "    pass\n",
     "tests": [_T + "test_import_from_snapshot_reads_no_live_source"]},
    {"name": "snapshot_not_cut_at_size", "file": "scripts/lib/telemetry/snapshot.py",
     "anchor": "                _copy_prefix(path, out / copy, size)\n",
     "replacement": "                shutil.copyfile(path, out / copy)\n",
     "tests": [_T + "test_snapshot_cuts_append_only_sources_at_their_size"]},
    {"name": "import_run_event_replayed", "file": _IMPORTER,
     "anchor": "    stats = run.stats\n    store.insert_rows(\"events\", [{\n",
     "replacement": "    stats = run.stats\n    stats.rows_added += 1\n    store.insert_rows(\"events\", [{\n",
     "tests": [_T + "test_import_run_event_not_in_second_run"]},
    {"name": "snapshot_ids_from_snapshot_path", "file": _IMPORTER,
     "anchor": "            _read_unit(run, unit, handle)\n",
     "replacement": "            unit.path = handle.name\n            _read_unit(run, unit, handle)\n",
     "tests": [_T + "test_snapshot_import_after_live_import_of_same_bytes_adds_no_row"]},
    {"name": "snapshot_backref_points_to_snapshot", "file": _IMPORTER,
     "anchor": '            fields["backref"] = _backref(line, unit.path)\n',
     "replacement": '            fields["backref"] = _backref(line, snapshot.readable_path(unit.path))\n',
     "tests": [_T + "test_snapshot_rows_carry_live_path_backreferences"]},
]
