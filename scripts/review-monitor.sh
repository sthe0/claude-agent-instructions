#!/usr/bin/env bash
# Detached poller that drives an open code review to mergeable — the mechanism
# half of leaves/review-accompanies-code.md (author owns the drive) and
# leaves/long-job-monitoring.md § Generalization (how to watch without burning
# model tokens or offloading the cadence to the user).
#
# Difficulty removed: a review the agent authored goes red or collects
# unanswered comments and sits there until the user says "the tests failed in
# your review". Polling it from the model thread is the expensive wrong fix;
# this script is the zero-token watcher. It logs markers the model reacts to
# when it is next woken, and it registers itself so the turn-end guardian
# (hook-review-mergeable-guardian.py) knows the review is being driven.
#
# Run it detached and record its PID, per leaves/long-job-monitoring.md step 1:
#
#   nohup bash scripts/review-monitor.sh \
#       --review-id https://reviews.example.com/review/42 \
#       --probe 'my-review-cli status {id} --format agent-status' \
#       --out /tmp/cc-scratch/review-42.log >/dev/null 2>&1 &
#
# ...and reap it at the review's terminal state or at task resolution,
# whichever comes first.
#
# >>> CONTRACT (printed verbatim by --help; keep the delimiters)
# PROBE CONTRACT (the pluggable backend)
# --------------------------------------
# Core ships the harness and this contract, never a concrete probe: which CLI
# or API reports a review's state is platform-specific, so the adapter is
# operator- or project-supplied (exactly as long_job_detect.py takes its
# orchestrator names from agent-identity.local and ships none).
#
# The --probe value is a command template run through `sh -c` after two
# substitutions: `{id}` becomes the --review-id value verbatim (the review URL,
# normally), and `{num}` becomes the review's bare numeric id extracted from it
# — so a probe that wants `<cli> status 42` gets it without the caller having to
# pass the id separately from the URL. The probe must print ONE line on stdout,
# whitespace-separated `key=value` pairs:
#
#   tests=<pending|success|failure> approved=<pending|success> \
#       unresolved_comments=<int> merged=<true|false>
#
# Unknown or missing keys are read as their pending/zero default, so a probe
# for a platform without, say, an approval concept simply omits `approved=`.
#
# MARKERS written to --out (one per line, UTC-timestamped)
# -------------------------------------------------------
#   MONITOR_STARTED    the poller armed (carries its own pid, for reaping)
#   MERGED             merged=true            -> TERMINAL: done
#   CHECK_FAILED       tests went failure     -> TERMINAL: fix the code
#   APPROVED           approved went success  -> TERMINAL: land it
#   NEW_COMMENTS       unresolved_comments up -> logged, polling continues
#   CAP_HIT            --max iterations spent without a terminal state
#   PROBE_UNREADABLE   probe printed nothing parseable (logged, keeps polling)
#
# A terminal marker stops the poller, because each one needs the model's
# judgment next (read the failure, land the change) and further polling of the
# same state buys nothing. NEW_COMMENTS is deliberately NOT terminal: comments
# arrive while checks are still running, and a watcher that quits on the first
# one stops tracking a review that is still moving. The model sees it on its
# next wake by reading this log — which is why the log records transitions
# rather than only the final state (leaves/long-job-monitoring.md step 1).
#
# USAGE
#   review-monitor.sh --review-id <id> --probe '<cmd with {id}>' --out <file>
#                     [--max N] [--sleep S] [--registry <file>]
#
#   --review-id  the review URL (preferred). It is interpolated into --probe as
#                {id}, its numeric part as {num}, and it is normalized into the
#                registry key the turn-end guardian matches, so arming a monitor
#                for a review you opened SILENCES the guardian for it. A bare
#                platform id ("42") is accepted but is PROBE-ONLY: no review
#                identity can be derived from it, so its registry key cannot
#                match what the guardian derives from the review's URL and the
#                nudge will keep firing. The script warns when you pass one.
#   --probe      status command template (see PROBE CONTRACT above)
#   --out        marker log file (parent dir created if absent)
#   --max        max poll iterations before CAP_HIT (default 288)
#   --sleep      seconds between polls (default 300)
#   --registry   monitored-reviews registry (default: the agent config root's
#                monitored-reviews.json, or $CLAUDE_MONITORED_REVIEWS)
# <<< CONTRACT
set -euo pipefail

SELF="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"

# Print the header block above rather than restating it: the contract has one
# home, and a delimiter range survives edits that a line range would not.
usage() {
  sed -n '/^# >>> CONTRACT/,/^# <<< CONTRACT/p' "$SELF" \
    | sed -e '1d' -e '$d' -e 's/^# \{0,1\}//'
}

REVIEW_ID=""
PROBE=""
OUT=""
MAX=288
SLEEP=300
REGISTRY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --review-id) REVIEW_ID="${2:-}"; shift 2 ;;
    --probe)     PROBE="${2:-}";     shift 2 ;;
    --out)       OUT="${2:-}";       shift 2 ;;
    --max)       MAX="${2:-}";       shift 2 ;;
    --sleep)     SLEEP="${2:-}";     shift 2 ;;
    --registry)  REGISTRY="${2:-}";  shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "review-monitor: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$REVIEW_ID" || -z "$PROBE" || -z "$OUT" ]]; then
  echo "review-monitor: --review-id, --probe and --out are all required" >&2
  usage >&2
  exit 2
fi

# A non-numeric --max/--sleep would make `[[ $i -lt $MAX ]]` read it as 0 and
# exit the loop immediately — a poller that silently never polls is worse than
# a refused launch.
if ! [[ "$MAX" =~ ^[0-9]+$ && "$SLEEP" =~ ^[0-9]+$ ]]; then
  echo "review-monitor: --max and --sleep must be non-negative integers" >&2
  exit 2
fi

# Every path below is interpolated from a variable, so each one is guarded for
# non-emptiness before use — an empty var would otherwise collapse a path to
# its parent (CLAUDE.md § Limits). This script never deletes anything.
OUT_DIR="$(dirname "$OUT")"
[[ -n "$OUT_DIR" ]] || { echo "review-monitor: cannot resolve --out directory" >&2; exit 2; }
mkdir -p "$OUT_DIR"

# The registry key the guardian will look for, derived by the same code that
# derives it from a review URL in the transcript — the two must agree or arming
# a monitor never silences the nudge. Empty (exit 1) means --review-id is not a
# review URL: the poll still works, the guardian match cannot.
REVIEW_KEY="$(python3 "$SCRIPT_DIR/review_open_detect.py" identity "$REVIEW_ID" 2>/dev/null || true)"
REVIEW_NUM="$REVIEW_ID"
if [[ -n "$REVIEW_KEY" ]]; then
  REVIEW_NUM="${REVIEW_KEY##*/}"
else
  echo "review-monitor: '$REVIEW_ID' is not a review URL — polling anyway, but the" \
       "turn-end guardian cannot match a bare id; pass the review URL to silence it" >&2
fi

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

emit() { # marker, detail
  printf '%s %s %s\n' "$(stamp)" "$1" "${2:-}" >> "$OUT"
}

registry_write() { # status
  local status="$1" args=()
  if [[ -n "$REGISTRY" ]]; then
    args=(--registry "$REGISTRY")
  fi
  python3 "$SCRIPT_DIR/review_open_detect.py" registry-upsert "$REVIEW_ID" \
    --out "$OUT" --pid "$$" --status "$status" "${args[@]+"${args[@]}"}" || true
}

field() { # key, line -> value on stdout ("" when absent)
  local key="$1" line="$2"
  if [[ "$line" =~ (^|[[:space:]])"$key"=([^[:space:]]*) ]]; then
    printf '%s' "${BASH_REMATCH[2]}"
  fi
}

registry_write running
emit MONITOR_STARTED "review=$REVIEW_ID pid=$$ max=$MAX sleep=${SLEEP}s"

prev_comments=""
i=0
while [[ "$i" -lt "$MAX" ]]; do
  i=$((i + 1))
  probe_cmd="${PROBE//\{id\}/$REVIEW_ID}"
  probe_cmd="${probe_cmd//\{num\}/$REVIEW_NUM}"
  # A probe that fails (network blip, expired auth) must not kill a poller that
  # may have hours left to run — log it and keep going.
  line="$(sh -c "$probe_cmd" 2>/dev/null || true)"
  line="${line%%$'\n'*}"

  tests="$(field tests "$line")"
  approved="$(field approved "$line")"
  comments="$(field unresolved_comments "$line")"
  merged="$(field merged "$line")"

  if [[ -z "$tests$approved$comments$merged" ]]; then
    emit PROBE_UNREADABLE "iteration=$i"
  else
    if [[ "$comments" =~ ^[0-9]+$ && "$prev_comments" =~ ^[0-9]+$ \
          && "$comments" -gt "$prev_comments" ]]; then
      emit NEW_COMMENTS "unresolved=$comments (was $prev_comments) review=$REVIEW_ID"
    fi
    if [[ "$comments" =~ ^[0-9]+$ ]]; then
      prev_comments="$comments"
    fi

    if [[ "$merged" == "true" ]]; then
      emit MERGED "review=$REVIEW_ID"
      registry_write done
      exit 0
    fi
    if [[ "$tests" == "failure" ]]; then
      emit CHECK_FAILED "review=$REVIEW_ID"
      registry_write done
      exit 0
    fi
    if [[ "$approved" == "success" ]]; then
      emit APPROVED "review=$REVIEW_ID unresolved=${comments:-0}"
      registry_write done
      exit 0
    fi
  fi

  sleep "$SLEEP"
done

emit CAP_HIT "no terminal state after $MAX polls review=$REVIEW_ID"
registry_write done
exit 0
