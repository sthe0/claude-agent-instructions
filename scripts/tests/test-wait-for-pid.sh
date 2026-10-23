#!/usr/bin/env bash
# Hermetic tests for scripts/wait-for-pid.sh.
#
# Each test is self-contained: it creates processes with predictable
# lifetimes, calls wait-for-pid.sh, and asserts the exit code and
# output. No real network or external state is touched.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"
SCRIPTS_DIR="$(cd "$HERE/.." && pwd -P)"
WAIT_BIN="$SCRIPTS_DIR/wait-for-pid.sh"

PASS=0; FAIL=0
ok()   { ((PASS++)); printf '  OK  %s\n' "$1"; }
fail() { ((FAIL++)); printf ' FAIL %s\n' "$1"; }

# ─── helpers ───────────────────────────────────────────────────────────────

# run_wait <args...> — run the script under bash (works without the executable
# bit being set) and return its exit code.
run_wait() { bash "$WAIT_BIN" "$@"; }

# ═══════════════════════════════════════════════════════════════════════════
# Test 1 — exits 0 after process terminates
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- exits-0-after-process-terminates ---\n'

sleep 0.2 &
CHILD=$!
out="$(run_wait "$CHILD" --interval 0 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "exits 0 when target process has finished"
else
  fail "expected exit 0, got $rc (output: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 2 — exits 1 when PID is missing
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- exits-1-missing-pid ---\n'

out="$(run_wait 2>&1)"
rc=$?

if [[ "$rc" -eq 1 ]]; then
  ok "exits 1 when PID argument is missing"
else
  fail "expected exit 1, got $rc (output: $out)"
fi
if printf '%s' "$out" | grep -qi 'required'; then
  ok "error message mentions 'required'"
else
  fail "error message unclear (got: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 3 — exits 1 when PID is non-integer
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- exits-1-non-integer-pid ---\n'

out="$(run_wait "not-a-pid" 2>&1)"
rc=$?

if [[ "$rc" -eq 1 ]]; then
  ok "exits 1 for non-integer PID"
else
  fail "expected exit 1, got $rc (output: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 4 — exits 1 when PID is zero
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- exits-1-zero-pid ---\n'

out="$(run_wait "0" 2>&1)"
rc=$?

if [[ "$rc" -eq 1 ]]; then
  ok "exits 1 for PID=0"
else
  fail "expected exit 1, got $rc (output: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 5 — exits 0 quickly when PID is already gone
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- exits-0-already-gone-pid ---\n'

# Start and immediately collect a short-lived process so its PID is free.
sleep 0 &
GONE=$!
wait "$GONE" 2>/dev/null || true

out="$(run_wait "$GONE" --interval 0 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "exits 0 immediately for an already-gone PID"
else
  fail "expected exit 0 for already-gone PID $GONE, got $rc (output: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 6 — --expect-substring passes when cmdline contains TEXT
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- expect-substring-passes ---\n'

sleep 0.2 &
CHILD=$!
# The substring "sleep" is always in `sleep 0.2`'s command line.
out="$(run_wait "$CHILD" --expect-substring "sleep" --interval 0 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "--expect-substring exits 0 when substring is present"
else
  fail "--expect-substring: expected exit 0, got $rc (output: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 7 — --expect-substring exits 2 when cmdline does NOT contain TEXT
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- expect-substring-mismatch ---\n'

sleep 10 &
CHILD=$!
out="$(run_wait "$CHILD" --expect-substring "DEFINITELY_NOT_IN_CMDLINE_XYZ" --interval 0 2>&1)"
rc=$?
kill "$CHILD" 2>/dev/null || true
wait "$CHILD" 2>/dev/null || true

if [[ "$rc" -eq 2 ]]; then
  ok "--expect-substring exits 2 when substring absent (PID reuse guard)"
else
  fail "--expect-substring: expected exit 2, got $rc (output: $out)"
fi
if printf '%s' "$out" | grep -qi 'reused\|does not contain'; then
  ok "--expect-substring error mentions reuse or mismatch"
else
  fail "--expect-substring error message unclear (got: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 8 — --expect-substring skips the check when process just exited
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- expect-substring-already-gone ---\n'

sleep 0 &
GONE=$!
wait "$GONE" 2>/dev/null || true

# PID is gone; even with a non-matching substring the guard cannot fire
# (kill -0 returns non-zero immediately, loop never enters the check).
out="$(run_wait "$GONE" --expect-substring "IMPOSSIBLE_MATCH" --interval 0 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "--expect-substring exits 0 for an already-gone PID (guard never fires)"
else
  fail "--expect-substring already-gone: expected exit 0, got $rc (output: $out)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
