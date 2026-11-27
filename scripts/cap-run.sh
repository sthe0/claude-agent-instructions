#!/usr/bin/env bash
# Run a command under a per-user task cap (RLIMIT_NPROC) and a wall-clock timeout.
#
#   cap-run.sh [--extra-tasks N] [--timeout S] [--own-tasks N] -- <command...>
#
# For the first trial of anything that spawns processes per invocation (test
# parallelizers, pools, recursive hooks): a wrong guard then ends as a failed fork
# (EAGAIN) in the trial, not as an exponential process tree on the host. The cap is
# the user's task count now plus N, and is never above a limit already in force, so
# a nested call clamps instead of raising. Not enforced for root.
# --own-tasks N bounds the run's own process tree instead: the supervisor re-sets the
# tree's soft RLIMIT_NPROC to (other tasks) + N + 128 every 0.5 s, so other sessions'
# later growth cannot consume the run's headroom, and kills the tree past N + 128.
# A daemonized child that re-parents to init leaves the tree and so escapes this mode's
# bound (the count-based cap bounded such daemons machine-wide) — accepted: the budget
# is for the run's own tree. Away from Linux, or without resource.prlimit, the
# count-based cap stays.
# See memory-global/leaves/fanout-trial-under-cap.md.
set -u

extra=512 # config.md: fanout-cap-extra-tasks
secs=3600 # config.md: fanout-cap-timeout-s
own=""

usage() {
    echo "usage: cap-run.sh [--extra-tasks N] [--timeout S] [--own-tasks N] -- <command...>" >&2
    exit 2
}

is_uint() {
    case "$1" in
        '' | *[!0-9]*) return 1 ;;
    esac
    return 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --extra-tasks) [ $# -ge 2 ] || usage; extra=$2; shift 2 ;;
        --timeout) [ $# -ge 2 ] || usage; secs=$2; shift 2 ;;
        --own-tasks) [ $# -ge 2 ] || usage; own=$2; shift 2 ;;
        --) shift; break ;;
        *) usage ;;
    esac
done
[ $# -gt 0 ] || usage
is_uint "$extra" || usage
is_uint "$secs" || usage
[ -z "$own" ] || { is_uint "$own" || usage; [ "$own" -gt 0 ] || usage; }

# --own-tasks bounds the started tree itself, so the count-based cap below is not set:
# a per-tree soft limit raised as others grow is the cap (a nested call clamps through
# the supervisor's own inherited soft limit). The supervisor touches only PIDs in the
# tree it started and never raises a hard limit.
if [ -n "$own" ]; then
    if [ "$(uname -s)" != "Linux" ]; then
        echo "cap-run.sh: --own-tasks unsupported away from Linux; keeping the count-based cap" >&2
    elif ! command -v python3 >/dev/null 2>&1; then
        echo "cap-run.sh: --own-tasks needs python3; keeping the count-based cap" >&2
    elif ! python3 -c 'import resource; raise SystemExit(0 if hasattr(resource, "prlimit") else 1)' 2>/dev/null; then
        echo "cap-run.sh: --own-tasks needs resource.prlimit; keeping the count-based cap" >&2
    else
        exec python3 "$(dirname -- "$0")/lib/cap_own_tasks.py" \
            --own-tasks "$own" --timeout "$secs" -- "$@"
    fi
fi

uid=$(id -u)
if [ "$(uname -s)" = "Linux" ]; then
    # RLIMIT_NPROC counts threads on Linux, so count threads, not processes.
    count=$(ps -u "$uid" -o nlwp= | awk '{s += $1} END {print s + 0}')
else
    count=$(ps -u "$uid" -o pid= | awk 'END {print NR}')
fi

want=$((count + extra))
cap=$want
for lim in "$(ulimit -Su)" "$(ulimit -Hu)"; do
    if is_uint "$lim" && [ "$lim" -lt "$cap" ]; then
        cap=$lim
    fi
done
if [ "$cap" -lt "$want" ]; then
    echo "cap-run.sh: requested cap $want clamped to $cap by a limit already in force" >&2
fi
ulimit -u "$cap" || { echo "cap-run.sh: cannot set ulimit -u $cap" >&2; exit 2; }
# The command can tell it runs capped: it compares this value with its own RLIMIT_NPROC.
export CAP_RUN_NPROC="$cap"

# --foreground keeps timeout in the caller's process group, so a group kill from a
# supervising runner reaches the whole tree instead of orphaning it.
if command -v timeout >/dev/null 2>&1; then
    exec timeout --foreground -k 10 "$secs" "$@"
fi
echo "cap-run.sh: 'timeout' not found; running without a time limit" >&2
exec "$@"
