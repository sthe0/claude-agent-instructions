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
_K = "scripts/tests/test_telemetry_tokens.py::"
_I = "scripts/tests/test_telemetry_importer.py::"
_R = "scripts/tests/test_core_version_report.py::"

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
     "anchor": "if bounds and ms is not None and bounds[0] <= ms <= bounds[1]:", "replacement": "if False:",
     "tests": [_R + "test_check_events_excuses_acts_in_the_declared_unrecorded_window"]},
    # check-latency
    {"name": "latency_from_last_event", "file": _REPORT,
     "anchor": "else (commit - first).total_seconds()",
     "replacement": f"else (commit - {_LAST_EVENT}).total_seconds()",
     "tests": [_R + "test_check_latency_measures_from_first_recorded_trigger_start_after_eligibility"]},
    {"name": "latency_from_eligibility", "file": _SYNC,
     "anchor": "first = next((t for t in _times(conn, \"SELECT time FROM events WHERE kind = ?\", (TRIGGER_START,)) if t >= end), None)",
     "replacement": "first = end",
     "tests": [_R + "test_check_latency_measures_from_first_recorded_trigger_start_after_eligibility"]},
    {"name": "latency_uses_last_commit", "file": _SYNC,
     "anchor": "(SYNC_COMMIT, session_id)) if t >= end), None)",
     "replacement": f"(SYNC_COMMIT, session_id)) if t >= end), None) and ({_SYNC_COMMITS} or [None])[-1]",
     "tests": [_R + "test_timely_first_commit_followed_by_commits_past_S_is_not_late"]},
    {"name": "latency_ttl_counts_sync_events", "file": _SYNC,
     "anchor": 'last = _times(conn, "SELECT time FROM events WHERE session_id = ?", (session_id,))',
     "replacement": 'last = _times(conn, "SELECT time FROM events WHERE session_id = ? '
                    'OR json_extract(fields, \'$.session\') = ?", (session_id, session_id))',
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

    {"name": "oversized_first_line_skipped", "file": _IMPORTER,
     "anchor": 'return raw if raw.endswith(b"\\n") or len(raw) == FIRST_LINE_MAX else None',
     "replacement": 'return raw if raw.endswith(b"\\n") else None',
     "tests": [_K + "test_oversized_first_line_transcript_is_collected",
               _I + "test_transcript_with_oversized_first_line_gets_transcript_events"]},
]
