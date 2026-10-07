#!/usr/bin/env bash
# Run a command under a per-user task cap (RLIMIT_NPROC) and a wall-clock timeout.
#
#   cap-run.sh [--extra-tasks N] [--timeout S] -- <command...>
#
# For the first trial of anything that spawns processes per invocation (test
# parallelizers, pools, recursive hooks): a wrong guard then ends as a failed fork
# (EAGAIN) in the trial, not as an exponential process tree on the host. The cap is
# the user's task count now plus N, and is never above a limit already in force, so
# a nested call clamps instead of raising. Not enforced for root.
# See memory-global/leaves/fanout-trial-under-cap.md.
set -u

extra=256 # config.md: fanout-cap-extra-tasks
secs=3600 # config.md: fanout-cap-timeout-s

usage() {
    echo "usage: cap-run.sh [--extra-tasks N] [--timeout S] -- <command...>" >&2
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
        --) shift; break ;;
        *) usage ;;
    esac
done
[ $# -gt 0 ] || usage
is_uint "$extra" || usage
is_uint "$secs" || usage

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

# --foreground keeps timeout in the caller's process group, so a group kill from a
# supervising runner reaches the whole tree instead of orphaning it.
if command -v timeout >/dev/null 2>&1; then
    exec timeout --foreground -k 10 "$secs" "$@"
fi
echo "cap-run.sh: 'timeout' not found; running without a time limit" >&2
exec "$@"
