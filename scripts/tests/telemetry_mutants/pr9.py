"""Mutants of PR 9: a transcript deleted between listing and reading (lib/telemetry/tokens.py,
lib/telemetry/snapshot.py), the CPU clock of the recording-cost benchmark (telemetry-bench.py),
the sync's materialize outside the write transaction, its sync.end row and the retry of a spool
still deferred after the budget (lib/telemetry/sync.py, lib/telemetry/store.py), and
check-latency's lock-contention window and judged-session floor (lib/telemetry/report.py,
scripts/telemetry.py).

Each mutant is `{name, file, anchor, replacement, tests}`: `anchor` occurs exactly once in
`file` (a path under the repo root), `replacement` keeps the module importable, `tests` are
the pytest node ids that must go red. The catalogue test checks the anchors and the ids.
"""
from __future__ import annotations

_TOKENS = "scripts/lib/telemetry/tokens.py"
_SNAPSHOT = "scripts/lib/telemetry/snapshot.py"
_BENCH = "scripts/telemetry-bench.py"
_K = "scripts/tests/test_telemetry_tokens.py::"
_B = "scripts/tests/test_telemetry_bench.py::"
_SYNC = "scripts/lib/telemetry/sync.py"
_STORE = "scripts/lib/telemetry/store.py"
_REPORT = "scripts/lib/telemetry/report.py"
_CLI = "scripts/telemetry.py"
_S = "scripts/tests/test_telemetry_sync_scale.py::"
_L = "scripts/tests/test_telemetry_live_shape.py::"
_RETRIED = _S + "test_deferred_spool_retried_after_budget_until_commit"
_SPAWN = "            _spawn_retry(stem, begun, session)\n            retried.append(stem)\n"
_RETRY_START = "    skipped = {NO_TIME_ROWS: 0}\n    ceiling = "
_UNCHANGED = "return events <= stored[0] and tasks <= stored[1] and keyed <= stored[2]"
_HOLD_END = "outcome[\"value\"] = work(conn)\n                _commit(conn)\n"
_CONTENTION = "tuple(report.outage_end_from_landing(conn, _as_dict(f)) for f in contention)"

_COVERAGE_CATCH = ("            verdict = _transcript_coverage(conn, path, session)\n"
                   "        except FileNotFoundError:\n")
_CHECK_CATCH = ("            lines = list(_usage_lines(path, session, limit))\n"
                "        except FileNotFoundError:\n")
_SNAPSHOT_CATCH = "        except FileNotFoundError as err:\n"

MUTANTS = [
    # a transcript deleted between listing and reading
    {"name": "vanished_transcript_raises_in_coverage", "file": _TOKENS, "anchor": _COVERAGE_CATCH,
     "replacement": _COVERAGE_CATCH + "            raise\n",
     "tests": [_K + "test_check_imported_coverage_counts_transcript_vanished_after_listing",
               _K + "test_check_tokens_cli_reports_vanished"]},
    {"name": "vanished_transcript_raises_in_check_tokens", "file": _TOKENS, "anchor": _CHECK_CATCH,
     "replacement": _CHECK_CATCH + "            raise\n",
     "tests": [_K + "test_check_tokens_counts_transcript_vanished_after_listing"]},
    {"name": "vanished_transcript_raises_in_snapshot", "file": _SNAPSHOT, "anchor": _SNAPSHOT_CATCH,
     "replacement": _SNAPSHOT_CATCH + "            raise\n",
     "tests": [_K + "test_snapshot_skips_transcript_vanished_after_listing"]},

    # the benchmark's clock
    {"name": "bench_hook_judges_wall_clock", "file": _BENCH,
     "anchor": "    hook = p95([cpu for cpu, _ in hooks])",
     "replacement": "    hook = p95([wall for _, wall in hooks])",
     "tests": [_B + "test_bench_judges_hook_cpu_time_not_wall_clock"]},
    {"name": "bench_command_judges_wall_clock", "file": _BENCH,
     "anchor": "p95([on[0] - off[0] for on, off in runs])",
     "replacement": "p95([on[1] - off[1] for on, off in runs])",
     "tests": [_B + "test_bench_judges_command_cpu_excess_not_wall_clock"]},

    # the sync: reads outside the write transaction, waiting marks, sync.end
    {"name": "materialize_reads_inside_write_transaction", "file": _SYNC,
     "anchor": "reads[session] = _read_outside(step is file_rows, session)",
     "replacement": "reads[session] = None",
     "tests": [_S + "test_materialize_reads_sessions_outside_write_transaction",
               _S + "test_writer_with_short_deadline_commits_while_materialize_files_large_session"]},
    {"name": "materialize_rereads_unchanged_waiting_session", "file": _SYNC,
     "anchor": _UNCHANGED, "replacement": "return False",
     "tests": [_S + "test_materialize_skips_waiting_session_unchanged_since_last_try"]},
    {"name": "materialize_skips_waiting_session_after_task_row", "file": _SYNC,
     "anchor": _UNCHANGED,
     "replacement": "return events <= stored[0] and keyed <= stored[2]",
     "tests": [_S + "test_materialize_rereads_waiting_session_after_task_row_arrives"]},
    {"name": "waiting_mark_ignores_session_key_update", "file": _SYNC,
     "anchor": _UNCHANGED,
     "replacement": "return events <= stored[0] and tasks <= stored[1]",
     "tests": [_S + "test_materialize_rereads_waiting_session_after_session_key_set_by_update"]},
    {"name": "sync_end_hold_misses_materialize", "file": _STORE,
     "anchor": _HOLD_END,
     "replacement": "outcome[\"value\"] = work(conn)\n                conn.execute(\"COMMIT\")\n",
     "tests": [_S + "test_sync_end_records_max_lock_hold_including_materialize"]},

    # the retry of a spool still deferred after the budget
    {"name": "deferred_spool_not_retried_after_budget", "file": _SYNC,
     "anchor": _SPAWN, "replacement": "            pass\n",
     "tests": [_RETRIED]},
    {"name": "deferred_spool_retried_by_every_sync", "file": _SYNC,
     "anchor": "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)", "replacement": "pass",
     "tests": [_S + "test_deferred_spool_retried_by_one_sync_only"]},
    {"name": "sweep_lock_held_through_retry", "file": _SYNC,
     "anchor": "close_fds=True", "replacement": "close_fds=False",
     "tests": [_S + "test_sweep_retry_does_not_hold_sweep_lock"]},
    {"name": "retry_commit_row_missing", "file": _SYNC,
     "anchor": "_write_own(SYNC_COMMIT, {\"session\": stem, \"rows_inserted\": inserted, \"retry\": True})",
     "replacement": "pass",
     "tests": [_RETRIED]},
    {"name": "retry_writes_own_trigger_start", "file": _SYNC,
     "anchor": _RETRY_START,
     "replacement": "    _write_own(TRIGGER_START, {\"kind\": \"retry\"})\n" + _RETRY_START,
     "tests": [_RETRIED]},

    # check-latency: the lock-contention window, the judged-session floor
    {"name": "lock_contention_window_unbounded", "file": _REPORT,
     "anchor": "lock_contention[0] <= first < lock_contention[1]:",
     "replacement": "lock_contention[0] <= first:",
     "tests": [_L + "test_lock_contention_window_names_late_session_in_window"]},
    {"name": "lock_contention_landing_unchecked", "file": _CLI,
     "anchor": _CONTENTION,
     "replacement": "tuple(sync.parse_time(_as_dict(f).get(\"landed_at\")) for f in contention)",
     "tests": [_L + "test_lock_contention_landing_bound_to_land_event"]},
    {"name": "min_judged_floor_ignored", "file": _REPORT,
     "anchor": "result[\"ok\"] = result[\"ok\"] and count >= floor",
     "replacement": "result[\"ok\"] = result[\"ok\"]",
     "tests": [_L + "test_min_judged_after_landing_fails_below_floor"]},
    {"name": "deferred_after_landing_excused", "file": _REPORT,
     "anchor": "> DEFERRED_JUDGED_AFTER_S):",
     "replacement": "> DEFERRED_JUDGED_AFTER_S and False):",
     "tests": [_L + "test_deferred_after_until_landing_judged_late_past_ceiling"]},
]
