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
]
