#!/usr/bin/env bash
# Wait for a process to exit, identified by an explicit PID — never by a
# search pattern that could match unrelated processes or the waiter itself.
#
# Usage:
#     scripts/wait-for-pid.sh <PID> [--expect-substring TEXT] [--interval N]
#
# Options:
#   <PID>                  The process to wait for (required; must be a
#                          non-empty positive integer).
#   --expect-substring TEXT
#                          Optional guard: on each poll, verify the live
#                          process's command line (ps -o args=) contains TEXT.
#                          If the PID is alive but TEXT is absent, the PID was
#                          silently reused by an unrelated process; exit 2.
#   --interval N           Poll interval in seconds (default 10).
#
# Exit codes:
#   0  process exited normally (kill -0 returned non-zero, meaning gone)
#   1  bad arguments (PID missing, non-integer, or zero)
#   2  PID is alive but --expect-substring TEXT is absent from its command
#      line — the PID was silently reused by an unrelated process
#
# Why only PID, never a pattern:
#   `pgrep -f PATTERN` matches against EVERY process's full command line,
#   including the waiter's own shell wrapper (if the pattern string appears
#   verbatim in the launch command that invoked this script). This causes
#   the waiter to report the target as "still running" even after it exits.
#   The confirmed occurrence (session baa1daea, 2026-08-25, ~2 h stalled wait)
#   is documented in memory-global/leaves/long-job-monitoring.md.

set -uo pipefail

INTERVAL=10
EXPECT=""
PID=""

usage() {
  echo "usage: wait-for-pid.sh <PID> [--expect-substring TEXT] [--interval N]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-substring)
      [[ $# -ge 2 ]] || usage
      EXPECT="$2"
      shift 2
      ;;
    --interval)
      [[ $# -ge 2 ]] || usage
      INTERVAL="$2"
      shift 2
      ;;
    -*)
      usage
      ;;
    *)
      if [[ -z "$PID" ]]; then
        PID="$1"
        shift
      else
        usage
      fi
      ;;
  esac
done

if [[ -z "$PID" ]]; then
  echo "wait-for-pid.sh: PID argument is required" >&2
  exit 1
fi
if ! [[ "$PID" =~ ^[1-9][0-9]*$ ]]; then
  echo "wait-for-pid.sh: PID must be a positive integer, got: $PID" >&2
  exit 1
fi

while kill -0 "$PID" 2>/dev/null; do
  if [[ -n "$EXPECT" ]]; then
    cmdline="$(ps -o args= -p "$PID" 2>/dev/null || true)"
    if [[ -n "$cmdline" && "$cmdline" != *"$EXPECT"* ]]; then
      echo "wait-for-pid.sh: PID $PID is alive but its command line does not contain the expected substring" >&2
      echo "  expected substring: $EXPECT" >&2
      echo "  actual command line: $cmdline" >&2
      echo "  The PID was probably reused by an unrelated process." >&2
      exit 2
    fi
  fi
  sleep "$INTERVAL"
done
