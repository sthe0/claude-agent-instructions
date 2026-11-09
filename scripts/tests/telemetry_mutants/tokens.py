"""Mutants of token accounting: the collector (lib/telemetry/tokens.py), its ranked upsert and
offset transaction in the store, its callers in sync and the importer, and the token migration.

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_TOKENS = "scripts/lib/telemetry/tokens.py"
_STORE = "scripts/lib/telemetry/store.py"
_SYNC = "scripts/lib/telemetry/sync.py"
_IMPORTER = "scripts/lib/telemetry/importer.py"
_MIGRATION = "scripts/lib/telemetry/migrations/006_tokens.sql"
_K = "scripts/tests/test_telemetry_tokens.py::"

_COLLECTOR_ANCHOR = "    collected = tokens.collect([s for s in stems if s != SYNC_SESSION])"
_TOKEN_ROW_ANCHOR = '"message_id": message_id, "session_id": session, "role"'
_ATTEMPT_CHECK = (
    '    if offset["read_outcome"] != "committed":\n'
    "        return f\"last_read_{offset['read_outcome']}\"\n"
    '    if offset["byte_offset"] != _last_line_end(path, offset["read_size"]):\n')
_PENDING_WRITE = (
    '            if not store.record_read_attempt(source, generation, CONSUMER, size, "pending", run.deadline_s):\n'
    '                run.counts["deferred"] += 1\n'
    "                return\n")

MUTANTS = [
    {"name": "double_count_message_id", "file": _TOKENS, "anchor": _TOKEN_ROW_ANCHOR,
     "replacement": '"message_id": f"{message_id}#{start}", "session_id": session, "role"',
     "tests": [_K + "test_tokens_conserved_across_role_phase_cells_with_message_id_dedup"]},
    {"name": "message_id_unique_per_task_only", "file": _TOKENS, "anchor": _TOKEN_ROW_ANCHOR,
     "replacement": ('"message_id": f"{message_id}@{(who.epoch_at(session, ts_ms) or (None, None, \'\'))[2]}", '
                     '"session_id": session, "role"'),
     "tests": [_K + "test_message_id_unique_across_the_record"]},
    {"name": "no_compaction_sizes", "file": _TOKENS, "anchor": '"pre_tokens": carry.get("last_size")',
     "replacement": '"pre_tokens": None',
     "tests": [_K + "test_compaction_event_carries_pre_and_post_sizes"]},
    {"name": "skip_subagent_transcripts", "file": _TOKENS, "anchor": "found.extend(own + subs)",
     "replacement": "found.extend(own)",
     "tests": [_K + "test_imported_transcripts_with_usage_get_token_rows"]},
    {"name": "judge_gap_silenced", "file": _TOKENS,
     "anchor": 'gaps["missing_judge_call"].append({"event_id": event_id, "source_key": source_key})',
     "replacement": "pass",
     "tests": [_K + "test_judge_ledger_rows_counted_and_missing_judge_call_listed_as_gap"]},
    {"name": "check_tokens_exit_0_on_gap", "file": _TOKENS,
     "anchor": 'ok = conserved and not gaps["missing_judge_call"] and not missing_births',
     "replacement": "ok = conserved and not missing_births",
     "tests": [_K + "test_judge_ledger_rows_counted_and_missing_judge_call_listed_as_gap",
               _K + "test_check_tokens_exit_codes"]},
    {"name": "judge_events_not_read", "file": _TOKENS, "anchor": "judge_events = _judge_events(conn, keys)",
     "replacement": "judge_events = []",
     "tests": [_K + "test_judge_ledger_rows_counted_and_missing_judge_call_listed_as_gap",
               _K + "test_expected_judge_calls_are_the_judge_events"]},
    {"name": "token_model_dropped", "file": _TOKENS, "anchor": '"model": model, **counts, "ts": _iso(ts_ms),',
     "replacement": '"model": None, **counts, "ts": _iso(ts_ms),',
     "tests": [_K + "test_token_rows_carry_the_message_model"]},
    {"name": "collapse_role_phase", "file": _TOKENS,
     "anchor": '"role": who.role(session, subagent),\n                "phase": who.phase_at(session, ts_ms),',
     "replacement": '"role": "root",\n                "phase": phases.OTHER,',
     "tests": [_K + "test_each_message_lands_in_its_expected_role_phase_cell"]},
    {"name": "shared_offset_with_importer", "file": _TOKENS, "anchor": 'CONSUMER = "tokens"',
     "replacement": 'CONSUMER = "transcript_events"',
     "tests": [_K + "test_token_offsets_per_file_and_consumer_start_at_zero"]},
    {"name": "birth_id_from_offset", "file": _TOKENS,
     "anchor": 'cid = _content_id(run.origin, "compaction", session, sha)',
     "replacement": 'cid = _content_id(run.origin, "compaction", session, str(start))',
     "tests": [_K + "test_reread_from_zero_under_changed_generation_and_after_rebuild_keeps_birth_compaction_and_"
                    "judge_usage_counts"]},
    {"name": "births_withheld_for_live_sessions", "file": _TOKENS,
     "anchor": 'if original and not carry.get("born"):',
     "replacement": 'if original and not carry.get("born") and not phases.has_live_events(run.conn, session):',
     "tests": [_K + "test_context_births_written_for_sessions_with_live_events"]},
    {"name": "check_tokens_sums_all_segments", "file": _TOKENS, "anchor": "        elif key[2] in epochs:",
     "replacement": "        elif key is not None:",
     "tests": [_K + "test_copy_owned_by_another_task_listed_as_gap_not_mismatch"]},
    {"name": "epochless_winner_gets_no_token_row", "file": _TOKENS,
     "anchor": '            rows.append(("token_usage", {',
     "replacement": ('            if who.epoch_at(session, ts_ms) is None:\n                continue\n'
                     '            rows.append(("token_usage", {'),
     "tests": [_K + "test_imported_coverage_green_with_epochless_transcript"]},
    {"name": "tokens_offset_advances_past_deferred_batch", "file": _TOKENS,
     "anchor": '            if not outcome["committed"]:',
     "replacement": '            if not outcome["committed"] and False:',
     "tests": [_K + "test_tokens_offset_unchanged_after_deferred_batch"]},
    {"name": "coverage_checks_only_has_token_rows", "file": _TOKENS,
     "anchor": "        problem = _attempt_problem(path, offset)",
     "replacement": '        problem = None if offset else "never_read"',
     "tests": [_K + "test_imported_coverage_fails_when_tokens_offset_short_of_last_line"]},
    {"name": "coverage_compares_with_current_file_size", "file": _TOKENS,
     "anchor": 'offset["byte_offset"] != _last_line_end(path, offset["read_size"])',
     "replacement": 'offset["byte_offset"] != _last_line_end(path, path.stat().st_size)',
     "tests": [_K + "test_transcript_appended_after_import_still_covered"]},
    {"name": "uncommitted_attempt_excused_as_grown_since_read", "file": _TOKENS, "anchor": _ATTEMPT_CHECK,
     "replacement": (
         '    if offset["read_outcome"] not in ("committed", "deferred", "pending"):\n'
         "        return f\"last_read_{offset['read_outcome']}\"\n"
         '    if offset["byte_offset"] != _last_line_end(path, offset["byte_offset"]):\n'),
     "tests": [_K + "test_grown_transcript_whose_tail_batch_deferred_fails_coverage"]},
    {"name": "task_measured_against_current_file_size", "file": _TOKENS,
     "anchor": 'limit = offset["byte_offset"] if offset else 0',
     "replacement": "limit = path.stat().st_size",
     "tests": [_K + "test_task_conserved_when_its_transcript_grows_after_last_read"]},
    {"name": "attempt_outcome_written_only_after_transaction", "file": _TOKENS, "anchor": _PENDING_WRITE,
     "replacement": "",
     "tests": [_K + "test_outcome_write_deferred_after_deferred_batch_fails_coverage"]},
    {"name": "token_table_version_not_validated", "file": _STORE,
     "anchor": '    "token_usage": {"role": ("roles", "role_table_version"), "phase": ("phases", "phase_table_version")},\n',
     "replacement": "",
     "tests": [_K + "test_token_rows_store_role_and_phase_table_versions_validated"]},
    {"name": "winner_first_seen", "file": _STORE, "anchor": "        f'WHERE ({mine}) < ({theirs})',",
     "replacement": "        f'WHERE 0',",
     "tests": [_K + "test_winning_line_is_earliest_timestamp_then_path_whatever_the_read_order"]},
    {"name": "winner_by_path_before_original", "file": _STORE,
     "anchor": '("ts_ms", "copy_rank", "session_start_ms", "transcript", "byte_offset")',
     "replacement": '("ts_ms", "transcript", "copy_rank", "session_start_ms", "byte_offset")',
     "tests": [_K + "test_winning_line_is_earliest_timestamp_then_path_whatever_the_read_order"]},
    {"name": "tokens_offset_committed_separately", "file": _STORE,
     "anchor": "                _write_read_offset(conn, offset)\n",
     "replacement": ('                _write_read_offset(conn, offset)\n                conn.execute("COMMIT")\n'
                     "                _begin(conn, deadline)\n"),
     "tests": [_K + "test_tokens_offset_advances_in_the_same_transaction_as_its_rows"]},
    {"name": "sync_skips_collector", "file": _SYNC, "anchor": _COLLECTOR_ANCHOR,
     "replacement": "    collected = {}",
     "tests": [_K + "test_sync_and_importer_call_the_collector"]},
    {"name": "collector_skipped_on_zero_row_commit", "file": _SYNC, "anchor": _COLLECTOR_ANCHOR,
     "replacement": ('    collected = tokens.collect([s for s in stems if s != SYNC_SESSION '
                     'and results[s].get("inserted", 0) > 0])'),
     "tests": [_K + "test_collector_runs_on_a_zero_row_sync_commit_after_an_early_drain"]},
    {"name": "importer_collector_skips_live_sessions", "file": _IMPORTER,
     "anchor": "counts = tokens.collect(None, deadline_s=BATCH_DEADLINE_S)",
     "replacement": ("counts = tokens.collect([p.stem for p in config_root.iter_transcripts() "
                     "if not run.is_live(p.stem)], deadline_s=BATCH_DEADLINE_S)"),
     "tests": [_K + "test_import_all_collects_tokens_for_sessions_with_live_events"]},
    {"name": "standing_surface_keyed_on_head_only", "file": _MIGRATION, "anchor": "    UNIQUE (origin, core_head)",
     "replacement": "    UNIQUE (core_head)",
     "tests": [_K + "test_standing_surface_rows_of_two_origins_at_one_head_coexist"]},
]
