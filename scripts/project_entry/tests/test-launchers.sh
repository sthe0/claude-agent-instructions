#!/usr/bin/env bash
# Hermetic test for claude-launchers.sh + auth-profiles.sh.
#
# Stubs:
#   CLAUDE_AUTH_PROFILE_DIR  -> temp dir with stub eliza/team/personal profiles
#   ENTER_TASK_BIN           -> stub enter-task.sh (echoes a canned dir)
#   claude binary            -> stub in $TMP added to PATH front (records env/args)
#
# No real enter-task/claude/git is called. Exit 0 on all-pass.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$HERE/../.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { ((PASS++)); printf '  OK  %s\n' "$1"; }
fail() { ((FAIL++)); printf ' FAIL %s\n' "$1"; }

# ── Stub profile directory ───────────────────────────────────────────────────
export CLAUDE_AUTH_PROFILE_DIR="$TMP/auth-profiles.d"
mkdir -p "$CLAUDE_AUTH_PROFILE_DIR"

cat >"$CLAUDE_AUTH_PROFILE_DIR/eliza.sh" <<'PROFILE'
export TEST_ELIZA_VAR=eliza-active
PROFILE

cat >"$CLAUDE_AUTH_PROFILE_DIR/team.sh" <<'PROFILE'
export TEST_TEAM_VAR=ya-pool-active
PROFILE

cat >"$CLAUDE_AUTH_PROFILE_DIR/personal.sh" <<'PROFILE'
export TEST_PERSONAL_VAR=personal-active
PROFILE

# ── Stub enter-task.sh ───────────────────────────────────────────────────────
export ET_CALLS="$TMP/et-calls.log"
export ET_DIR="$TMP/stub-workdir"
mkdir -p "$ET_DIR"
: >"$ET_CALLS"

FAKE_ET="$TMP/fake-enter-task.sh"
cat >"$FAKE_ET" <<'SCRIPT'
#!/usr/bin/env bash
# Record every invocation and print the canned directory.
printf '%s\n' "$*" >> "$ET_CALLS"
printf '%s\n' "$ET_DIR"
SCRIPT
chmod +x "$FAKE_ET"
export ENTER_TASK_BIN="$FAKE_ET"

# ── Stub claude binary ───────────────────────────────────────────────────────
# command claude bypasses functions, so we provide a real binary at front of PATH.
export CLAUDE_ENV_RECORDED="$TMP/claude-env.log"
export CLAUDE_ARGS="$TMP/claude-args.log"
: >"$CLAUDE_ENV_RECORDED" >"$CLAUDE_ARGS"

FAKE_CLAUDE="$TMP/claude"
cat >"$FAKE_CLAUDE" <<'SCRIPT'
#!/usr/bin/env bash
env >> "$CLAUDE_ENV_RECORDED"
printf '%s\n' "$*" >> "$CLAUDE_ARGS"
SCRIPT
chmod +x "$FAKE_CLAUDE"
# Prepend TMP so command claude finds the stub.
export PATH="$TMP:$PATH"

# Onboard call-log (defined before reset_logs so the function can clear it).
export ONBOARD_LOG="$TMP/onboard.log"
: >"$ONBOARD_LOG"

# ── Source the launchers (profile dir is already set via env) ────────────────
# shellcheck source=scripts/claude-launchers.sh
source "$SCRIPTS_DIR/claude-launchers.sh"

# ── Helper: reset per-test log files ─────────────────────────────────────────
reset_logs() {
  : >"$ET_CALLS" >"$CLAUDE_ENV_RECORDED" >"$CLAUDE_ARGS" >"$ONBOARD_LOG"
}

# ── Onboard global setup — empty hook dir so existing tests are unaffected ───
export EMPTY_HOOK_DIR="$TMP/empty-onboard.d"
mkdir -p "$EMPTY_HOOK_DIR"
export CLAUDE_ONBOARD_HOOK_DIR="$EMPTY_HOOK_DIR"
FAKE_ONBOARD="$TMP/fake-onboard.sh"
cat >"$FAKE_ONBOARD" <<HOOKSCRIPT
#!/usr/bin/env bash
printf 'onboard\n' >> "$ONBOARD_LOG"
HOOKSCRIPT
chmod +x "$FAKE_ONBOARD"
export CLAUDE_ONBOARD_BIN="$FAKE_ONBOARD"

# ═══════════════════════════════════════════════════════════════════════════
# Test 1 — all expected functions are defined
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- function presence ---\n'
for _fn in claude-task claude-eliza claude-team claude-personal onboard; do
  if declare -f "$_fn" &>/dev/null; then
    ok "$_fn is defined"
  else
    fail "$_fn is NOT defined"
  fi
done

# ═══════════════════════════════════════════════════════════════════════════
# Test 2 — arg classification: DEEPAGENT-7 -> --key
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- arg classification ---\n'
reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-team DEEPAGENT-7 2>/dev/null)"
if grep -qF -- '--key DEEPAGENT-7' "$ET_CALLS"; then
  ok "DEEPAGENT-7 classified as --key"
else
  fail "DEEPAGENT-7 not classified as --key (et-calls: $(cat "$ET_CALLS"))"
fi
if printf '%s\n' "$out" | grep -q 'profile=team'; then
  ok "DEEPAGENT-7 dry-run: profile=team"
else
  fail "DEEPAGENT-7 dry-run: missing profile=team (got: $out)"
fi

# Test 2b — DEEPAGENT-1 specifically (the done-criterion example)
reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-team DEEPAGENT-1 2>/dev/null)"
if printf '%s\n' "$out" | grep -qF "enter=${ET_DIR}"; then
  ok "DEEPAGENT-1 dry-run: enter=<stub-dir>"
else
  fail "DEEPAGENT-1 dry-run: missing enter= (got: $out)"
fi
if printf '%s\n' "$out" | grep -q 'profile=team'; then
  ok "DEEPAGENT-1 dry-run: profile=team"
else
  fail "DEEPAGENT-1 dry-run: missing profile=team (got: $out)"
fi

# Test 2c — --new classification
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 claude-task --new 'My New Task' >/dev/null 2>&1 || true
if grep -qF -- '--new' "$ET_CALLS"; then
  ok "--new classified as --new"
else
  fail "--new not classified as --new (et-calls: $(cat "$ET_CALLS"))"
fi
if grep -q 'My New Task' "$ET_CALLS"; then
  ok "--new title forwarded to enter-task"
else
  fail "--new title not forwarded (et-calls: $(cat "$ET_CALLS"))"
fi

# Test 2d — empty args -> in-place launch (NO workspace entry)
reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-task 2>/dev/null)"
if [[ -s "$ET_CALLS" ]]; then
  fail "empty args should NOT call enter-task (et-calls: $(cat "$ET_CALLS"))"
else
  ok "empty args: enter-task not called (in-place)"
fi
if printf '%s\n' "$out" | grep -q '^inplace profile=default dir='; then
  ok "empty args: in-place dry-run line printed"
else
  fail "empty args: missing in-place dry-run line (got: $out)"
fi

# Test 2e — plain word -> --name
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 claude-task myfeature >/dev/null 2>&1 || true
if grep -qF -- '--name myfeature' "$ET_CALLS"; then
  ok "plain word classified as --name"
else
  fail "plain word not classified as --name (et-calls: $(cat "$ET_CALLS"))"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 3 — dry-run output format: correct enter= and profile= values
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- dry-run output format ---\n'
reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-task DEEPAGENT-1 2>/dev/null)"
if [[ "$out" == "enter=${ET_DIR} profile=default" ]]; then
  ok "claude-task dry-run format correct"
else
  fail "claude-task dry-run format wrong (got: '$out')"
fi

reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-team DEEPAGENT-1 2>/dev/null)"
if [[ "$out" == "enter=${ET_DIR} profile=team" ]]; then
  ok "claude-team dry-run format correct"
else
  fail "claude-team dry-run format wrong (got: '$out')"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 4 — non-dry claude-team applies team.sh env (TEST_TEAM_VAR) to claude
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- non-dry profile env application ---\n'
reset_logs
claude-team DEEPAGENT-1 2>/dev/null || true
if grep -q 'TEST_TEAM_VAR=ya-pool-active' "$CLAUDE_ENV_RECORDED"; then
  ok "non-dry claude-team: team.sh env (TEST_TEAM_VAR) visible to claude"
else
  fail "non-dry claude-team: TEST_TEAM_VAR not in claude env (recorded: $(grep TEST "$CLAUDE_ENV_RECORDED" || echo none))"
fi

# Confirm default profile does NOT leak team vars
reset_logs
claude-task DEEPAGENT-1 2>/dev/null || true
if grep -q 'TEST_TEAM_VAR' "$CLAUDE_ENV_RECORDED"; then
  fail "default profile should not expose TEST_TEAM_VAR"
else
  ok "default profile: no TEST_TEAM_VAR leakage"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 5 — -h/--help prints usage; calls neither enter-task nor claude
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- help ---\n'
reset_logs
out="$(claude-personal --help 2>/dev/null)"
if printf '%s\n' "$out" | grep -q 'Usage: claude-personal'; then
  ok "--help prints usage with the command name"
else
  fail "--help did not print usage (got: $out)"
fi
if [[ -s "$ET_CALLS" ]]; then
  fail "--help should not call enter-task (et-calls: $(cat "$ET_CALLS"))"
else
  ok "--help: enter-task not called"
fi
if [[ -s "$CLAUDE_ARGS" ]]; then
  fail "--help should not launch claude (args: $(cat "$CLAUDE_ARGS"))"
else
  ok "--help: claude not launched"
fi
reset_logs
if claude-task -h 2>/dev/null | grep -q 'Usage: claude-task'; then
  ok "-h prints usage (claude-task)"
else
  fail "-h did not print usage (claude-task)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 6 — bare invocation launches plain claude in-place WITH the auth profile
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- bare in-place launch applies auth profile ---\n'
reset_logs
claude-team 2>/dev/null || true
if [[ -s "$ET_CALLS" ]]; then
  fail "bare claude-team should NOT call enter-task (et-calls: $(cat "$ET_CALLS"))"
else
  ok "bare claude-team: enter-task not called"
fi
if grep -q 'TEST_TEAM_VAR=ya-pool-active' "$CLAUDE_ENV_RECORDED"; then
  ok "bare claude-team: team.sh env applied to in-place claude"
else
  fail "bare claude-team: TEST_TEAM_VAR not in claude env (recorded: $(grep TEST "$CLAUDE_ENV_RECORDED" || echo none))"
fi

# Test 6b — a bare claude flag (-c) is forwarded to claude, not treated as a name
reset_logs
claude-task -c 2>/dev/null || true
if [[ -s "$ET_CALLS" ]]; then
  fail "claude-task -c should NOT call enter-task (et-calls: $(cat "$ET_CALLS"))"
else
  ok "claude-task -c: enter-task not called (flag passthrough)"
fi
if grep -qx -- '-c' "$CLAUDE_ARGS"; then
  ok "claude-task -c: -c forwarded to claude"
else
  fail "claude-task -c: -c not forwarded (args: $(cat "$CLAUDE_ARGS"))"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 7 — onboard probe: _maybe_onboard integration
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- onboard probe ---\n'

# Case (a): empty hook dir -> onboard NOT called
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 claude-task DEEPAGENT-1 >/dev/null 2>/dev/null || true
if [[ -s "$ONBOARD_LOG" ]]; then
  fail "empty hook dir: onboard should NOT be called (log: $(cat "$ONBOARD_LOG"))"
else
  ok "empty hook dir: onboard not called"
fi

# Case (b): hook --needs-init=0 -> onboard called once, BEFORE enter-task + banner printed
# _LAUNCHERS_ENTER_TASK is set at source time so we temporarily override it directly.
reset_logs
HOOK_DIR_B="$TMP/hook-b.d"; mkdir -p "$HOOK_DIR_B"
ORDER_LOG_B="$TMP/order-b.log"; : >"$ORDER_LOG_B"
cat >"$HOOK_DIR_B/10-init.sh" <<'HSCRIPT'
#!/usr/bin/env bash
if [[ "${1:-}" == "--needs-init" ]]; then exit 0; fi
HSCRIPT
chmod +x "$HOOK_DIR_B/10-init.sh"
FAKE_ONBOARD_B="$TMP/fake-onboard-b.sh"
cat >"$FAKE_ONBOARD_B" <<HSCRIPT
#!/usr/bin/env bash
printf 'onboard\n' >> "$ONBOARD_LOG"
printf 'onboard\n' >> "$ORDER_LOG_B"
HSCRIPT
chmod +x "$FAKE_ONBOARD_B"
FAKE_ET_B="$TMP/fake-et-b.sh"
cat >"$FAKE_ET_B" <<HSCRIPT
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$ET_CALLS"
printf 'enter-task\n' >> "$ORDER_LOG_B"
printf '%s\n' "$ET_DIR"
HSCRIPT
chmod +x "$FAKE_ET_B"
_stderr_b="$TMP/stderr-b.txt"
_saved_et="$_LAUNCHERS_ENTER_TASK"
_LAUNCHERS_ENTER_TASK="$FAKE_ET_B"
CLAUDE_ONBOARD_HOOK_DIR="$HOOK_DIR_B" CLAUDE_ONBOARD_BIN="$FAKE_ONBOARD_B" \
  CLAUDE_LAUNCH_DRYRUN=1 \
  claude-task DEEPAGENT-1 >/dev/null 2>"$_stderr_b" || true
_LAUNCHERS_ENTER_TASK="$_saved_et"
if grep -q 'environment not initialized' "$_stderr_b"; then
  ok "needs-init=0: banner printed"
else
  fail "needs-init=0: banner not printed (stderr: $(cat "$_stderr_b"))"
fi
if [[ "$(grep -c '^onboard$' "$ONBOARD_LOG" 2>/dev/null || echo 0)" -eq 1 ]]; then
  ok "needs-init=0: onboard called exactly once"
else
  fail "needs-init=0: onboard call count wrong (log: $(cat "$ONBOARD_LOG"))"
fi
_ob_line="$(grep -n '^onboard$' "$ORDER_LOG_B" | head -1 | cut -d: -f1)"
_et_line="$(grep -n '^enter-task$' "$ORDER_LOG_B" | head -1 | cut -d: -f1)"
if [[ -n "$_ob_line" && -n "$_et_line" && "$_ob_line" -lt "$_et_line" ]]; then
  ok "needs-init=0: onboard called before enter-task"
else
  fail "needs-init=0: ordering wrong (onboard@${_ob_line:-none}, enter-task@${_et_line:-none})"
fi

# Case (c): hook --needs-init=1 (already initialized) -> onboard NOT called
reset_logs
HOOK_DIR_C="$TMP/hook-c.d"; mkdir -p "$HOOK_DIR_C"
cat >"$HOOK_DIR_C/10-already.sh" <<'HSCRIPT'
#!/usr/bin/env bash
if [[ "${1:-}" == "--needs-init" ]]; then exit 1; fi
HSCRIPT
chmod +x "$HOOK_DIR_C/10-already.sh"
CLAUDE_ONBOARD_HOOK_DIR="$HOOK_DIR_C" CLAUDE_LAUNCH_DRYRUN=1 \
  claude-task DEEPAGENT-1 >/dev/null 2>/dev/null || true
if [[ -s "$ONBOARD_LOG" ]]; then
  fail "needs-init=1: onboard should NOT be called (log: $(cat "$ONBOARD_LOG"))"
else
  ok "needs-init=1: onboard not called (already initialized)"
fi

# Case (d): CLAUDE_SKIP_ONBOARD=1 -> probe suppressed entirely
reset_logs
HOOK_DIR_D="$TMP/hook-d.d"; mkdir -p "$HOOK_DIR_D"
cat >"$HOOK_DIR_D/10-init.sh" <<'HSCRIPT'
#!/usr/bin/env bash
if [[ "${1:-}" == "--needs-init" ]]; then exit 0; fi
HSCRIPT
chmod +x "$HOOK_DIR_D/10-init.sh"
CLAUDE_ONBOARD_HOOK_DIR="$HOOK_DIR_D" CLAUDE_SKIP_ONBOARD=1 CLAUDE_LAUNCH_DRYRUN=1 \
  claude-task DEEPAGENT-1 >/dev/null 2>/dev/null || true
if [[ -s "$ONBOARD_LOG" ]]; then
  fail "CLAUDE_SKIP_ONBOARD: onboard should NOT be called (log: $(cat "$ONBOARD_LOG"))"
else
  ok "CLAUDE_SKIP_ONBOARD=1: probe suppressed"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
