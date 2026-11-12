"""Mutants of the per-Core-version report and its verification tools: the record readers and the
core-version view (lib/telemetry/report.py), check-events and check-latency (scripts/telemetry.py),
the live-versus-transcript comparison (scripts/telemetry-compare.py), the synthetic driver and the
benchmark, and the first-line key the importer and the token collector share.

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_IMPORTER = "scripts/lib/telemetry/importer.py"
_REPORT = "scripts/lib/telemetry/report.py"
_SYNC = "scripts/lib/telemetry/sync.py"
_TOKENS = "scripts/lib/telemetry/tokens.py"
_K = "scripts/tests/test_telemetry_tokens.py::"
_I = "scripts/tests/test_telemetry_importer.py::"
_R = "scripts/tests/test_core_version_report.py::"
_COMPARE = "scripts/telemetry-compare.py"
_DRIVER = "scripts/telemetry-synthetic-task.py"
_BENCH = "scripts/telemetry-bench.py"
_C = "scripts/tests/test_telemetry_compare.py::"

_LAST_EVENT = 'sync.parse_time(conn.execute("SELECT MAX(time) FROM events WHERE session_id = ?", (session,)).fetchone()[0])'
_SYNC_COMMITS = ('[t for t in _times(conn, "SELECT time FROM events WHERE kind = ? AND json_extract(fields, \'$.session\') = ?", '
                 '(SYNC_COMMIT, session_id)) if t >= end]')

MUTANTS = [
    # the core-version view and its readers
    {"name": "flake_counts_two", "file": _REPORT,
     "anchor": "            flaky.add(group)",
     "replacement": "            flaky.add((group, len(flaky)))\n            seen_red.discard(group)",
     "tests": [_R + "test_red_then_green_on_same_digest_and_ref_is_one_flake"]},
    {"name": "report_reads_transcripts", "file": _REPORT,
     "anchor": '"spend_usd": _spend(conn, key),',
     "replacement": '"spend_usd": _spend(conn, key) + sum(transcript_cost.price_weighted_usd(transcript_cost.usage_rows(p)) '
                    'for r in config_root.projects_roots() for p in r.rglob("*.jsonl")),',
     "tests": [_R + "test_report_built_from_record_opens_no_transcript"]},
    {"name": "weight_class_not_normalized", "file": _REPORT,
     "anchor": "    count = len(group)", "replacement": "    count = all_tasks",
     "tests": [_R + "test_columns_per_head_normalized_by_weight_class"]},
    {"name": "synthetic_included_in_report", "file": _REPORT,
     "anchor": "if row[5] and not include_synthetic:", "replacement": "if False:",
     "tests": [_R + "test_synthetic_tasks_excluded_by_default"]},
    {"name": "other_dropped_when_zero", "file": _REPORT,
     "anchor": "return ({name: by_category.get(name, 0) for name in categories},",
     "replacement": 'return ({name: by_category.get(name, 0) for name in categories if name != "other" or by_category.get(name)},',
     "tests": [_R + "test_other_category_counted_per_head", _R + "test_zero_count_categories_are_written"]},
    {"name": "self_cost_check_always_passes", "file": _REPORT,
     "anchor": 'status = 1 if payload["self_cost_failures"] else status', "replacement": "status = status",
     "tests": [_R + "test_check_self_cost_fails_without_overhead_landing_minutes_or_attempts"]},
    {"name": "land_attempts_counted_once", "file": _REPORT,
     "anchor": '"land_attempts": sum(1 for _, s, _, _ in rows if s == "land:attempt"),',
     "replacement": '"land_attempts": int(any(s == "land:attempt" for _, s, _, _ in rows)),',
     "tests": [_R + "test_land_attempts_counted_from_landing_events"]},
    {"name": "unresolved_events_dropped", "file": _REPORT,
     "anchor": "target[source_key] = target.get(source_key, 0) + count", "replacement": "pass",
     "tests": [_R + "test_every_event_resolves_to_one_registry_entry_or_is_counted_unresolved"]},
    {"name": "landing_minutes_whole_task", "file": _REPORT,
     "anchor": 'landing_ms = sum(i["end"] - i["start"] for i in intervals if i["start"] is not None and i["end"] is not None)',
     "replacement": 'landing_ms = (max(i["end"] for i in intervals) - min(i["start"] for i in intervals)) if intervals else 0',
     "tests": [_R + "test_landing_minutes_sum_per_pr_intervals"]},
    {"name": "imported_unresolved_fails_check", "file": _REPORT,
     "anchor": 'if args.check and payload["unresolved_source_keys"]:',
     "replacement": 'if args.check and (payload["unresolved_source_keys"] or payload["imported_unresolved_source_keys"]):',
     "tests": [_R + "test_imported_unresolved_source_keys_counted_not_failed"]},
    {"name": "spend_priced_at_one_model", "file": _REPORT,
     "anchor": '"cache_read_input_tokens": r[3] or 0}, r[4])',
     "replacement": '"cache_read_input_tokens": r[3] or 0}, None)',
     "tests": [_R + "test_spend_usd_prices_each_row_by_its_model_on_a_mixed_model_fixture"]},
    {"name": "null_alignment_causes_dropped", "file": _REPORT,
     "anchor": "unaligned.append(row)", "replacement": "pass",
     "tests": [_R + "test_readers_return_the_declared_fields"]},
    # check-events
    {"name": "check_events_ignores_gates", "file": _REPORT,
     "anchor": "if row.get(\"session\") in task_sessions and row.get(\"gate\"):",
     "replacement": "if False:",
     "tests": [_R + "test_check_events_fails_on_a_missing_event_row"]},
    {"name": "unrecorded_window_ignored", "file": _REPORT,
     "anchor": "elif bounds and ms is not None and bounds[0] <= ms <= bounds[1]:", "replacement": "elif False:",
     "tests": [_R + "test_check_events_excuses_acts_in_the_declared_unrecorded_window"]},
    # check-latency
    {"name": "latency_from_last_event", "file": _REPORT,
     "anchor": "else (commit - first).total_seconds()",
     "replacement": f"else (commit - {_LAST_EVENT}).total_seconds()",
     "tests": [_R + "test_check_latency_measures_from_first_recorded_trigger_start_after_eligibility"]},
    {"name": "latency_from_eligibility", "file": _SYNC,
     "anchor": "first = next((t for t in starts if t >= end), None)",
     "replacement": "first = end",
     "tests": [_R + "test_check_latency_measures_from_first_recorded_trigger_start_after_eligibility"]},
    {"name": "latency_uses_last_commit", "file": _SYNC,
     "anchor": "(SYNC_COMMIT, session_id), skipped) if t >= end), None)",
     "replacement": f"(SYNC_COMMIT, session_id), skipped) if t >= end), None) and ({_SYNC_COMMITS} or [None])[-1]",
     "tests": [_R + "test_timely_first_commit_followed_by_commits_past_S_is_not_late"]},
    {"name": "latency_ttl_counts_sync_events", "file": _SYNC,
     "anchor": 'last = _times(conn, "SELECT event_id, time FROM events WHERE session_id = ?", (session_id,), skipped)',
     "replacement": 'last = _times(conn, "SELECT event_id, time FROM events WHERE session_id = ? '
                    'OR json_extract(fields, \'$.session\') = ?", (session_id, session_id), skipped)',
     "tests": [_R + "test_killed_session_E_unchanged_across_repeated_sweeps"]},
    {"name": "latency_appearance_requires_commit_after_T1", "file": _REPORT,
     "anchor": "seconds = 0.0 if first is None or commit < first else",
     "replacement": 'seconds = float("inf") if first is not None and commit < first else 0.0 if first is None else',
     "tests": [_R + "test_check_latency_ttl_boundary_race_commit_between_E_and_T1_is_not_late"]},
    {"name": "latency_waiting_never_fails", "file": _REPORT,
     "anchor": "elif first is None and commit is None:", "replacement": "elif commit is None:",
     "tests": [_R + "test_check_latency_fails_when_a_later_trigger_opportunity_passed"]},
    {"name": "latency_passes_empty_population", "file": _REPORT,
     "anchor": 'ok = not late and any(r["A"] for r in rows)', "replacement": "ok = not late",
     "tests": [_R + "test_check_latency_fails_on_an_empty_population"]},
    {"name": "latency_in_flight_counted_late", "file": _REPORT,
     "anchor": 'status = "in_flight"', "replacement": 'status = "late"',
     "tests": [_R + "test_check_latency_lists_an_in_flight_sync_younger_than_the_bound"]},
    {"name": "deferred_sync_counted_as_late", "file": _REPORT,
     "anchor": 'status = "sync_deferred"', "replacement": 'status = "late"',
     "tests": [_R + "test_check_latency_lists_deferred_sync_rows_as_named_loss_not_late"]},

    {"name": "tolerance_includes_se", "file": _COMPARE,
     "anchor": 'reconstruction_half_width(stat["lo"], stat["hi"]))',
     "replacement": 'reconstruction_half_width(stat["lo"], stat["hi"]) + stat["se"])',
     "tests": [_C + "test_tolerance_is_max_of_two_points_and_reconstruction_half_width"]},

    {"name": "bench_ignores_bound", "file": _BENCH,
     "anchor": "for name, ms in commands.items() if ms > command_bound_ms]",
     "replacement": "for name, ms in commands.items() if False]",
     "tests": [_C + "test_bench_fails_when_a_bound_is_exceeded"]},

    {"name": "trace_counts_hooks_as_command_path", "file": _DRIVER,
     "anchor": "return HOOK_PATH if any(",
     "replacement": "return COMMAND_PATH if any(",
     "tests": [_C + "test_trace_classifies_hook_and_command_path_processes"]},

    {"name": "compare_ignores_snapshot_sha", "file": _COMPARE,
     "anchor": "if actual != expected:",
     "replacement": "if False:",
     "tests": [_C + "test_compare_refuses_a_snapshot_with_another_sha256"]},

    {"name": "historical_transcript_side_reads_frozen_aggregate", "file": _COMPARE,
     "anchor": "lines = snapshot_lines(snapshot, {s for _, windows in chosen for s in windows})",
     "replacement": "lines = {s: _file_lines(p) for s, p in local_transcripts().items()}",
     "tests": [_C + "test_historical_transcript_side_computed_over_the_record_side_task_set"]},

    {"name": "historical_missing_task_kept_on_record_side", "file": _COMPARE,
     "anchor": "if any(s not in stems for s in required):",
     "replacement": "if False:",
     "tests": [_C + "test_historical_task_missing_from_the_snapshot_left_out_of_both_sides"]},

    {"name": "historical_passes_on_empty_join", "file": _COMPARE,
     "anchor": 'payload["ok"] = False\n        payload["error"] = f"{len(chosen)} task(s) join',
     "replacement": 'payload["error"] = f"{len(chosen)} task(s) join',
     "tests": [_C + "test_historical_side_fails_when_no_task_joins_the_snapshot"]},

    {"name": "historical_transcript_side_counts_whole_session", "file": _COMPARE,
     "anchor": "return phase_ms(phases.bash_points(rows), landing, window)",
     "replacement": "return phase_ms(phases.bash_points(rows), landing, (0, 2**62))",
     "tests": [_C + "test_historical_transcript_side_splits_a_session_at_task_boundaries"]},

    {"name": "historical_partially_recorded_task_kept", "file": _COMPARE,
     "anchor": "elif _partially_recorded(conn, windows):",
     "replacement": "elif False:",
     "tests": [_C + "test_historical_task_with_partially_recorded_session_left_out_of_both_sides"]},

    {"name": "historical_unimported_transcript_task_kept", "file": _COMPARE,
     "anchor": "elif any(not _has_rows(conn, s, RECORDED) for s in required):",
     "replacement": "elif False:",
     "tests": [_C + "test_historical_task_with_unimported_transcript_left_out_of_both_sides"]},

    {"name": "historical_judge_contour_requires_transcript", "file": _COMPARE,
     "anchor": "required = [s for s in contours if roles.get(s) != JUDGE_ROLE]",
     "replacement": "required = list(contours)",
     "tests": [_C + "test_historical_task_with_transcriptless_judge_contour_stays_in_both_sides"]},

    {"name": "live_partially_recorded_task_kept", "file": _COMPARE,
     "anchor": "elif _partially_recorded(conn, sessions):",
     "replacement": "elif False:",
     "tests": [_C + "test_live_side_excludes_partially_recorded_and_open_tasks"]},

    {"name": "live_imported_task_kept", "file": _COMPARE,
     "anchor": "if not windows:",
     "replacement": "if False:",
     "tests": [_C + "test_live_side_excludes_imported_only_tasks"]},

    {"name": "live_mixed_provenance_task_kept", "file": _COMPARE,
     "anchor": "elif any(_transcript_derived(conn, s) for s in sessions):",
     "replacement": "elif False:",
     "tests": [_C + "test_live_side_excludes_tasks_with_an_imported_session"]},

    {"name": "live_task_without_transcript_kept", "file": _COMPARE,
     "anchor": "elif any(s not in transcripts for s in needs):",
     "replacement": "elif False:",
     "tests": [_C + "test_live_task_without_transcript_left_out_of_both_sides"]},

    {"name": "judge_contour_requires_transcript", "file": _COMPARE,
     "anchor": "needs = [s for s in sessions if roles.get(s) != JUDGE_ROLE]",
     "replacement": "needs = list(sessions)",
     "tests": [_C + "test_task_with_transcriptless_judge_contour_stays_in_the_live_set"]},

    {"name": "synthesize_ignores_min_tasks", "file": _COMPARE,
     "anchor": "wanted = min(max(min_tasks - len(chosen), 0), max_synthetic)",
     "replacement": "wanted = max_synthetic",
     "tests": [_C + "test_synthesize_tops_up_to_min_tasks"]},

    {"name": "driver_omits_synthetic_env", "file": _DRIVER,
     "anchor": 'env[live.SYNTHETIC_ENV] = "1"',
     "replacement": "env.pop(live.SYNTHETIC_ENV, None)",
     "tests": [_C + "test_driver_run_task_reads_synthetic_true"]},
    # the child walks the engine sequence, not just classify and status
    {"name": "driver_runs_status_only", "file": _DRIVER,
     "anchor": 'code = run_task(task_prompt(Path(scratch) / "state", plan), child_env(), trace_log)',
     "replacement": ("code = run_task(\"Run `python3 ~/claude-agent-instructions/scripts/agentctl-cli.py classify "
                     "--title synthetic` and then `agentctl-cli.py status`, and stop.\", child_env(), trace_log)"),
     "tests": [_C + "test_driver_prompt_walks_the_engine_against_an_isolated_state_root"]},

    {"name": "oversized_first_line_skipped", "file": _IMPORTER,
     "anchor": 'return raw if raw.endswith(b"\\n") or len(raw) == FIRST_LINE_MAX else None',
     "replacement": 'return raw if raw.endswith(b"\\n") else None',
     "tests": [_K + "test_oversized_first_line_transcript_is_collected",
               _I + "test_transcript_with_oversized_first_line_gets_transcript_events"]},

    # the importer and the collector open the record under their batch deadline, not the command path's
    {"name": "import_opens_under_command_path_deadline", "file": _IMPORTER,
     "anchor": "boot = store.open_record(BATCH_DEADLINE_S)",
     "replacement": "boot = store.open_record()",
     "tests": [_I + "test_import_completes_when_creating_the_record_outlasts_the_command_path_deadline"]},
    {"name": "collect_opens_under_command_path_deadline", "file": _TOKENS,
     "anchor": "boot = store.open_record(deadline_s)",
     "replacement": "boot = store.open_record()",
     "tests": [_K + "test_collect_on_a_fresh_record_survives_a_bootstrap_past_the_command_path_deadline"]},
]
