#!/usr/bin/env bash
# Hermetic test for claude-launchers.sh + auth-profiles.sh.
#
# Stubs:
#   CLAUDE_AUTH_PROFILE_DIR  -> temp dir with stub eliza/team/personal profiles
#   ENTER_TASK_BIN           -> stub enter-task.sh (echoes a canned dir)
#   OPENING_BIN              -> stub opening.py (default: exit 3, suppressed —
#                               keeps every pre-existing case hermetic)
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

# ── Stub opening.py ──────────────────────────────────────────────────────────
# Default: suppressed (exit 3, no stdout) so pre-existing --key/--new cases —
# which never anticipated an opening dialogue — stay unaffected.
FAKE_OPENING="$TMP/fake-opening.sh"
cat >"$FAKE_OPENING" <<'SCRIPT'
#!/usr/bin/env bash
exit 3
SCRIPT
chmod +x "$FAKE_OPENING"
export OPENING_BIN="$FAKE_OPENING"

# ── Stub claude binary ───────────────────────────────────────────────────────
# command claude bypasses functions, so we provide a real binary at front of PATH.
export CLAUDE_ENV_RECORDED="$TMP/claude-env.log"
export CLAUDE_ARGS="$TMP/claude-args.log"
: >"$CLAUDE_ENV_RECORDED" >"$CLAUDE_ARGS"

FAKE_CLAUDE="$TMP/claude"
cat >"$FAKE_CLAUDE" <<'SCRIPT'
#!/usr/bin/env bash
env >> "$CLAUDE_ENV_RECORDED"
# NUL-delimited: a space-joined "$*" can't distinguish "one arg with a space"
# from "two args", and breaks entirely on an arg containing a newline (a real
# opening prompt can be multi-line).
# printf still runs its format once with an empty conversion when given zero
# data args, so guard on $# or a zero-arg call phantom-records one empty entry.
if [ "$#" -gt 0 ]; then
  printf '%s\0' "$@" >> "$CLAUDE_ARGS"
fi
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

# ── Helper: read the NUL-delimited CLAUDE_ARGS log into _CLAUDE_ARGV ─────────
_claude_argv() {
  _CLAUDE_ARGV=()
  local _item
  while IFS= read -r -d '' _item; do
    _CLAUDE_ARGV+=("$_item")
  done <"$CLAUDE_ARGS"
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
# Test 2 — arg classification: PROJ-7 -> --key
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- arg classification ---\n'
reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-team PROJ-7 2>/dev/null)"
if grep -qF -- '--key PROJ-7' "$ET_CALLS"; then
  ok "PROJ-7 classified as --key"
else
  fail "PROJ-7 not classified as --key (et-calls: $(cat "$ET_CALLS"))"
fi
if printf '%s\n' "$out" | grep -q 'profile=team'; then
  ok "PROJ-7 dry-run: profile=team"
else
  fail "PROJ-7 dry-run: missing profile=team (got: $out)"
fi

# Test 2b — PROJ-1 specifically (the done-criterion example)
reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-team PROJ-1 2>/dev/null)"
if printf '%s\n' "$out" | grep -qF "enter=${ET_DIR}"; then
  ok "PROJ-1 dry-run: enter=<stub-dir>"
else
  fail "PROJ-1 dry-run: missing enter= (got: $out)"
fi
if printf '%s\n' "$out" | grep -q 'profile=team'; then
  ok "PROJ-1 dry-run: profile=team"
else
  fail "PROJ-1 dry-run: missing profile=team (got: $out)"
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
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-task PROJ-1 2>/dev/null)"
if [[ "$out" == "enter=${ET_DIR} profile=default config=${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}" ]]; then
  ok "claude-task dry-run format correct"
else
  fail "claude-task dry-run format wrong (got: '$out')"
fi

reset_logs
out="$(CLAUDE_LAUNCH_DRYRUN=1 claude-team PROJ-1 2>/dev/null)"
if [[ "$out" == "enter=${ET_DIR} profile=team config=${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}" ]]; then
  ok "claude-team dry-run format correct"
else
  fail "claude-team dry-run format wrong (got: '$out')"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 4 — non-dry claude-team applies team.sh env (TEST_TEAM_VAR) to claude
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- non-dry profile env application ---\n'
reset_logs
claude-team PROJ-1 2>/dev/null || true
if grep -q 'TEST_TEAM_VAR=ya-pool-active' "$CLAUDE_ENV_RECORDED"; then
  ok "non-dry claude-team: team.sh env (TEST_TEAM_VAR) visible to claude"
else
  fail "non-dry claude-team: TEST_TEAM_VAR not in claude env (recorded: $(grep TEST "$CLAUDE_ENV_RECORDED" || echo none))"
fi

# Confirm default profile does NOT leak team vars
reset_logs
claude-task PROJ-1 2>/dev/null || true
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
# Capture then grep (not a pipe): under `set -o pipefail`, grep -q closes the
# pipe on first match and _launcher_usage's trailing project_list write takes
# SIGPIPE (141), which pipefail would surface as a spurious failure.
_h_out="$(claude-task -h 2>/dev/null)"
if printf '%s\n' "$_h_out" | grep -q 'Usage: claude-task'; then
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
_claude_argv
if [[ "${#_CLAUDE_ARGV[@]}" -eq 1 && "${_CLAUDE_ARGV[0]}" == "-c" ]]; then
  ok "claude-task -c: -c forwarded to claude"
else
  fail "claude-task -c: -c not forwarded (args: ${_CLAUDE_ARGV[*]:-<empty>})"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 7 — onboard probe: _maybe_onboard integration
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- onboard probe ---\n'

# Case (a): empty hook dir -> onboard NOT called
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 claude-task PROJ-1 >/dev/null 2>/dev/null || true
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
  claude-task PROJ-1 >/dev/null 2>"$_stderr_b" || true
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
  claude-task PROJ-1 >/dev/null 2>/dev/null || true
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
  claude-task PROJ-1 >/dev/null 2>/dev/null || true
if [[ -s "$ONBOARD_LOG" ]]; then
  fail "CLAUDE_SKIP_ONBOARD: onboard should NOT be called (log: $(cat "$ONBOARD_LOG"))"
else
  ok "CLAUDE_SKIP_ONBOARD=1: probe suppressed"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 8 — --list-projects routes to enter-task
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- --list-projects routing ---\n'
reset_logs
CLAUDE_SKIP_ONBOARD=1 claude-task --list-projects >/dev/null 2>/dev/null || true
if grep -qF -- '--list-projects' "$ET_CALLS"; then
  ok "--list-projects: forwarded to enter-task"
else
  fail "--list-projects: not forwarded to enter-task (et-calls: $(cat "$ET_CALLS"))"
fi
# Confirm enter-task was called (not bypassed or sent to claude)
if [[ -s "$CLAUDE_ARGS" ]]; then
  fail "--list-projects: should not launch claude (args: $(cat "$CLAUDE_ARGS"))"
else
  ok "--list-projects: claude not launched"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 9 — --help shows Projects: line when registry has entries
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- --help Projects: line ---\n'
PROJ_TEST_DIR="$TMP/test-projects.d"
mkdir -p "$PROJ_TEST_DIR/myteam/svc"
printf '{"tracker_queue":"SVCQ","tracker_backend":"github"}\n' \
  >"$PROJ_TEST_DIR/myteam/svc/agent-project.json"

reset_logs
_help_out="$(CLAUDE_PROJECTS_DIR="$PROJ_TEST_DIR" \
              CLAUDE_PROJECTS_LOCAL_DIR="$TMP/empty-local.d" \
              CLAUDE_SKIP_ONBOARD=1 \
              claude-task --help 2>/dev/null)"
if printf '%s\n' "$_help_out" | grep -q 'Projects:'; then
  ok "--help: Projects: line present"
else
  fail "--help: Projects: line absent (got: $_help_out)"
fi
if printf '%s\n' "$_help_out" | grep -q 'myteam/svc'; then
  ok "--help: Projects: line contains the project key"
else
  fail "--help: Projects: line missing project key (got: $_help_out)"
fi
if printf '%s\n' "$_help_out" | grep -q 'list-projects'; then
  ok "--help: Projects: line mentions --list-projects"
else
  fail "--help: Projects: line missing --list-projects hint (got: $_help_out)"
fi

# When registry is empty/unavailable, the Projects: line must NOT appear and --help
# must not error.
reset_logs
_help_out2="$(CLAUDE_PROJECTS_DIR="$TMP/nonexistent-dir" \
               CLAUDE_PROJECTS_LOCAL_DIR="$TMP/also-nonexistent" \
               CLAUDE_SKIP_ONBOARD=1 \
               claude-task --help 2>/dev/null)"
if printf '%s\n' "$_help_out2" | grep -q 'Projects:'; then
  fail "--help: Projects: line should not appear when registry is empty"
else
  ok "--help: Projects: line absent when registry empty (degrade silently)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 10 — github tracker_create echoes CLAUDE_TRACKER_QUEUE in dry-run
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- github tracker: queue in dry-run ---\n'
# Source github.sh to get tracker_create and _gh_slug.
GH_BIN="$TMP/stub-gh-unused"
# shellcheck source=scripts/project_entry/trackers/github.sh
source "$SCRIPTS_DIR/project_entry/trackers/github.sh"

_tr_out="$(CLAUDE_DRY_RUN=1 CLAUDE_TRACKER_QUEUE=TESTQ tracker_create 'My issue' 2>&1)"
if printf '%s\n' "$_tr_out" | grep -q 'queue=TESTQ'; then
  ok "github tracker_create: dry-run echoes queue"
else
  fail "github tracker_create: dry-run missing queue (got: $_tr_out)"
fi
# The stdout line must still be DRYRUN<TAB>slug.
_tr_stdout="$(CLAUDE_DRY_RUN=1 CLAUDE_TRACKER_QUEUE=TESTQ tracker_create 'My issue' 2>/dev/null)"
if printf '%s\n' "$_tr_stdout" | grep -q 'DRYRUN'; then
  ok "github tracker_create: dry-run stdout DRYRUN line present"
else
  fail "github tracker_create: dry-run stdout wrong (got: $_tr_stdout)"
fi

# Without CLAUDE_TRACKER_QUEUE: the confirmation-gate message must still work (no
# queue suffix), and unset var must not error.
_tr_no_q="$(CLAUDE_DRY_RUN=1 tracker_create 'No-queue issue' 2>&1)"
if printf '%s\n' "$_tr_no_q" | grep -q 'would create issue'; then
  ok "github tracker_create: no queue -> dry-run message still present"
else
  fail "github tracker_create: no queue -> dry-run message missing (got: $_tr_no_q)"
fi
if printf '%s\n' "$_tr_no_q" | grep -q 'queue='; then
  fail "github tracker_create: no queue -> queue suffix must not appear"
else
  ok "github tracker_create: no queue -> no queue= suffix"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 11 — --new confirmation gate + stderr surfacing
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- --new confirm gate + stderr surfacing ---\n'

# Case (a): non-interactive (</dev/null) --new WITHOUT the gate -> abort with a
# hint, and enter-task is NOT called.
reset_logs
_se_a="$TMP/stderr-11a.txt"
CLAUDE_SKIP_ONBOARD=1 claude-task --new 'Risky task' </dev/null >/dev/null 2>"$_se_a" || true
if [[ -s "$ET_CALLS" ]]; then
  fail "11a: --new without gate should NOT call enter-task (et-calls: $(cat "$ET_CALLS"))"
else
  ok "11a: non-interactive --new without gate: enter-task not called"
fi
if grep -q 'CLAUDE_LAUNCH_ASSUME_YES=1' "$_se_a"; then
  ok "11a: abort message names CLAUDE_LAUNCH_ASSUME_YES=1"
else
  fail "11a: abort message missing the gate hint (stderr: $(cat "$_se_a"))"
fi

# Case (b): --new WITH CLAUDE_LAUNCH_ASSUME_YES=1 -> enter-task called and the
# gate is forwarded into its environment.
reset_logs
FAKE_ET_NEW="$TMP/fake-et-new.sh"
cat >"$FAKE_ET_NEW" <<'SCRIPT'
#!/usr/bin/env bash
printf 'assume=%s\n' "${CLAUDE_LAUNCH_ASSUME_YES:-unset}" >> "$ET_CALLS"
printf '%s\n' "$*" >> "$ET_CALLS"
printf '%s\n' "$ET_DIR"
SCRIPT
chmod +x "$FAKE_ET_NEW"
_saved_et_b="$_LAUNCHERS_ENTER_TASK"
_LAUNCHERS_ENTER_TASK="$FAKE_ET_NEW"
CLAUDE_SKIP_ONBOARD=1 CLAUDE_LAUNCH_ASSUME_YES=1 \
  claude-task --new 'Confirmed task' </dev/null >/dev/null 2>/dev/null || true
_LAUNCHERS_ENTER_TASK="$_saved_et_b"
if grep -qF -- '--new' "$ET_CALLS"; then
  ok "11b: --new with gate calls enter-task"
else
  fail "11b: --new with gate did not call enter-task (et-calls: $(cat "$ET_CALLS"))"
fi
if grep -qx 'assume=1' "$ET_CALLS"; then
  ok "11b: CLAUDE_LAUNCH_ASSUME_YES=1 forwarded to enter-task env"
else
  fail "11b: gate not forwarded to enter-task (et-calls: $(cat "$ET_CALLS"))"
fi

# Case (c): a failing enter-task stub's stderr is surfaced by the launcher
# (no longer swallowed by 2>/dev/null).
reset_logs
FAKE_ET_FAIL="$TMP/fake-et-fail.sh"
cat >"$FAKE_ET_FAIL" <<'SCRIPT'
#!/usr/bin/env bash
printf 'enter-task: explanatory failure reason\n' >&2
exit 1
SCRIPT
chmod +x "$FAKE_ET_FAIL"
_saved_et_c="$_LAUNCHERS_ENTER_TASK"
_LAUNCHERS_ENTER_TASK="$FAKE_ET_FAIL"
_se_c="$TMP/stderr-11c.txt"
CLAUDE_SKIP_ONBOARD=1 claude-task somename </dev/null >/dev/null 2>"$_se_c" || true
_LAUNCHERS_ENTER_TASK="$_saved_et_c"
if grep -q 'explanatory failure reason' "$_se_c"; then
  ok "11c: launcher surfaces enter-task stderr on failure"
else
  fail "11c: enter-task stderr was swallowed (stderr: $(cat "$_se_c"))"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 12 — --project/--workspace/--tracker modifier-flag forwarding
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- --project modifier-flag forwarding ---\n'

# Case (c): --project is forwarded to enter-task alongside --new.
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 CLAUDE_LAUNCH_ASSUME_YES=1 \
  claude-task --project team/proj --new 'T' </dev/null >/dev/null 2>/dev/null || true
if grep -qF -- '--project team/proj' "$ET_CALLS"; then
  ok "12c: --project forwarded to enter-task"
else
  fail "12c: --project not forwarded (et-calls: $(cat "$ET_CALLS"))"
fi
if grep -qF -- '--new' "$ET_CALLS" && grep -qF 'T' "$ET_CALLS"; then
  ok "12c: --new + title still forwarded alongside --project"
else
  fail "12c: --new/title not forwarded (et-calls: $(cat "$ET_CALLS"))"
fi

# Case (d): --project is forwarded to enter-task alongside a tracker key.
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 claude-task --project team/proj PROJ-7 >/dev/null 2>/dev/null || true
if grep -qF -- '--project team/proj' "$ET_CALLS"; then
  ok "12d: --project forwarded alongside a ticket key"
else
  fail "12d: --project not forwarded (et-calls: $(cat "$ET_CALLS"))"
fi
if grep -qF -- '--key PROJ-7' "$ET_CALLS"; then
  ok "12d: ticket key still classified as --key"
else
  fail "12d: ticket key not forwarded as --key (et-calls: $(cat "$ET_CALLS"))"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 13 — --init classification and dry-run output
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- --init classification ---\n'
INIT_TMP="$TMP/init-base"
mkdir -p "$INIT_TMP"

# 12a: CLAUDE_LAUNCH_DRYRUN=1 with --init demo -> enter=<ET_DIR> profile=default
# (fake-enter-task.sh always echoes ET_DIR regardless of args; the assertion
# confirms --init reaches the normal dir-resolve+launch tail, not an early exit)
reset_logs
_init_out="$(CLAUDE_PROJECT_INIT_BASE="$INIT_TMP" CLAUDE_LAUNCH_DRYRUN=1 \
  CLAUDE_SKIP_ONBOARD=1 claude-task --init demo 2>/dev/null)"
if [[ "$_init_out" == "enter=${ET_DIR} profile=default config=${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}" ]]; then
  ok "--init dry-run: output is enter=<stub-dir> profile=default"
else
  fail "--init dry-run: wrong output (got: '$_init_out')"
fi
if grep -qF -- '--init demo' "$ET_CALLS"; then
  ok "--init: forwarded to enter-task as --init demo"
else
  fail "--init: not forwarded (et-calls: $(cat "$ET_CALLS"))"
fi

# 12b: --help mentions --init
reset_logs
_help_init="$(CLAUDE_SKIP_ONBOARD=1 claude-task --help 2>/dev/null)"
if printf '%s\n' "$_help_init" | grep -q -- '--init'; then
  ok "--help: mentions --init"
else
  fail "--help: does not mention --init (got: $_help_init)"
fi

# 12c: --init without a name -> error, no enter-task call
reset_logs
if CLAUDE_SKIP_ONBOARD=1 claude-task --init 2>/dev/null; then
  fail "--init without name should return non-zero"
else
  ok "--init without name: returns non-zero"
fi
if [[ -s "$ET_CALLS" ]]; then
  fail "--init without name: should NOT call enter-task"
else
  ok "--init without name: enter-task not called"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 14 — dry-run argv= diagnostic line lands on stderr, stdout unchanged
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- dry-run argv= diagnostic (stderr only) ---\n'
FAKE_OPENING_PROMPT="$TMP/fake-opening-prompt.sh"
cat >"$FAKE_OPENING_PROMPT" <<'SCRIPT'
#!/usr/bin/env bash
printf 'ticket:\n  some ticket text\nartifacts: (none)\nmode: opening\n'
exit 0
SCRIPT
chmod +x "$FAKE_OPENING_PROMPT"

reset_logs
_s4_out="$TMP/stage4-stdout.txt"
_s4_err="$TMP/stage4-stderr.txt"
OPENING_BIN="$FAKE_OPENING_PROMPT" CLAUDE_LAUNCH_DRYRUN=1 \
  claude-task ABC-123 -c >"$_s4_out" 2>"$_s4_err" || true
_s4_expected_out="enter=${ET_DIR} profile=default config=${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}"
if [[ "$(cat "$_s4_err")" == "argv=<prompt> -c" && "$(cat "$_s4_out")" == "$_s4_expected_out" ]]; then
  ok "dryrun:argv-line-on-stderr"
else
  fail "dryrun:argv-line-on-stderr (stdout: $(cat "$_s4_out"), stderr: $(cat "$_s4_err"))"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 15 — opening prompt: exact argv composition at the real exec point
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- opening prompt: exact argv composition ---\n'

# _make_opening_stub <stub_path> <text_file> <exit_code>
# Writes an OPENING_BIN replacement that cats <text_file> (verbatim, including
# any embedded spaces/newlines) to stdout, then exits <exit_code>.
_make_opening_stub() {
  local _stub="$1" _textfile="$2" _rc="$3"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'cat %q\n' "$_textfile"
    printf 'exit %s\n' "$_rc"
  } >"$_stub"
  chmod +x "$_stub"
}

# _assert_claude_argv <label> [expected_arg]...
# Compares the composed claude argv against the expected list ELEMENT-WISE
# (never by substring) via the NUL-delimited CLAUDE_ARGS log.
_assert_claude_argv() {
  local _label="$1"; shift
  _claude_argv
  if [[ "${#_CLAUDE_ARGV[@]}" -eq "$#" ]]; then
    local _i=0 _mismatch=0 _exp
    for _exp in "$@"; do
      [[ "${_CLAUDE_ARGV[_i]}" == "$_exp" ]] || _mismatch=1
      ((_i++))
    done
    if [[ "$_mismatch" -eq 0 ]]; then
      ok "$_label"
      return
    fi
  fi
  fail "$_label (got: ${_CLAUDE_ARGV[*]:-<empty>}; want: $*)"
}

OPEN_TEXT="$TMP/opening-text"

# opening:key-injects — a --key spec composes the prompt as a single argv element
reset_logs
printf 'PROMPT-KEY' >"$OPEN_TEXT.key"
_make_opening_stub "$TMP/opening-key.sh" "$OPEN_TEXT.key" 0
OPENING_BIN="$TMP/opening-key.sh" claude-task ABC-123 2>/dev/null || true
_assert_claude_argv "opening:key-injects" "PROMPT-KEY"

# opening:new-injects — a --new spec composes the prompt too
reset_logs
printf 'PROMPT-NEW' >"$OPEN_TEXT.new"
_make_opening_stub "$TMP/opening-new.sh" "$OPEN_TEXT.new" 0
OPENING_BIN="$TMP/opening-new.sh" CLAUDE_LAUNCH_ASSUME_YES=1 \
  claude-task --new 'Some Title' </dev/null 2>/dev/null || true
_assert_claude_argv "opening:new-injects" "PROMPT-NEW"

# opening:name-suppressed — a plain --name spec never invokes OPENING_BIN
reset_logs
printf 'SHOULD-NOT-APPEAR' >"$OPEN_TEXT.leak1"
_make_opening_stub "$TMP/opening-leak1.sh" "$OPEN_TEXT.leak1" 0
OPENING_BIN="$TMP/opening-leak1.sh" claude-task myfeature 2>/dev/null || true
_assert_claude_argv "opening:name-suppressed"

# opening:init-suppressed — --init never invokes OPENING_BIN
reset_logs
printf 'SHOULD-NOT-APPEAR' >"$OPEN_TEXT.leak2"
_make_opening_stub "$TMP/opening-leak2.sh" "$OPEN_TEXT.leak2" 0
OPENING_BIN="$TMP/opening-leak2.sh" CLAUDE_PROJECT_INIT_BASE="$INIT_TMP" \
  claude-task --init demo 2>/dev/null || true
_assert_claude_argv "opening:init-suppressed"

# opening:no-flag-suppresses — --no-opening overrides a --key spec's default-on
reset_logs
printf 'SHOULD-NOT-APPEAR' >"$OPEN_TEXT.leak3"
_make_opening_stub "$TMP/opening-leak3.sh" "$OPEN_TEXT.leak3" 0
OPENING_BIN="$TMP/opening-leak3.sh" claude-task --no-opening ABC-123 2>/dev/null || true
_assert_claude_argv "opening:no-flag-suppresses"

# opening:env-off-suppresses — CLAUDE_OPENING=off overrides a --key spec's default-on
reset_logs
printf 'SHOULD-NOT-APPEAR' >"$OPEN_TEXT.leak4"
_make_opening_stub "$TMP/opening-leak4.sh" "$OPEN_TEXT.leak4" 0
CLAUDE_OPENING=off OPENING_BIN="$TMP/opening-leak4.sh" claude-task ABC-123 2>/dev/null || true
_assert_claude_argv "opening:env-off-suppresses"

# opening:inplace-untouched — the in-place (no-spec) path never reaches opening logic
reset_logs
printf 'SHOULD-NOT-APPEAR' >"$OPEN_TEXT.leak5"
_make_opening_stub "$TMP/opening-leak5.sh" "$OPEN_TEXT.leak5" 0
OPENING_BIN="$TMP/opening-leak5.sh" claude-task -c 2>/dev/null || true
_assert_claude_argv "opening:inplace-untouched" "-c"

# opening:user-flags-order-preserved — prompt precedes forwarded flags, in order
# (this is the mutation-proof case: swapping the exec point's prompt/cargs
# order must turn this RED).
reset_logs
printf 'PROMPT-ORDER' >"$OPEN_TEXT.order"
_make_opening_stub "$TMP/opening-order.sh" "$OPEN_TEXT.order" 0
OPENING_BIN="$TMP/opening-order.sh" claude-task ABC-123 --model haiku -c 2>/dev/null || true
_assert_claude_argv "opening:user-flags-order-preserved" "PROMPT-ORDER" "--model" "haiku" "-c"

# opening:prompt-with-space-and-newline — a multi-line, space-containing prompt
# still arrives as exactly ONE argv element. This is the recorder-upgrade
# control: a single-token stub prompt would look identical under the old
# space-joined "$*" recorder, hiding the exact defect this stage fixes.
reset_logs
printf 'hello world\nsecond line has spaces too' >"$OPEN_TEXT.multiline"
_make_opening_stub "$TMP/opening-multiline.sh" "$OPEN_TEXT.multiline" 0
OPENING_BIN="$TMP/opening-multiline.sh" claude-task ABC-123 -c 2>/dev/null || true
_assert_claude_argv "opening:prompt-with-space-and-newline" \
  "$(printf 'hello world\nsecond line has spaces too')" "-c"

# opening:crash-suppresses-loudly — opening.py's own internal crash (exit 7,
# neither 0 nor the suppressed-exit 3): prompt suppressed, one stderr warning
# names the exit code, but the launch still proceeds to claude.
reset_logs
FAKE_OPENING_CRASH="$TMP/opening-crash.sh"
cat >"$FAKE_OPENING_CRASH" <<'SCRIPT'
#!/usr/bin/env bash
exit 7
SCRIPT
chmod +x "$FAKE_OPENING_CRASH"
_se_crash="$TMP/stderr-crash.txt"
OPENING_BIN="$FAKE_OPENING_CRASH" claude-task ABC-123 -c 2>"$_se_crash" || true
_claude_argv
if [[ "${#_CLAUDE_ARGV[@]}" -eq 1 && "${_CLAUDE_ARGV[0]}" == "-c" ]] \
  && grep -q 'opening.py exited 7' "$_se_crash"; then
  ok "opening:crash-suppresses-loudly"
else
  fail "opening:crash-suppresses-loudly (argv: ${_CLAUDE_ARGV[*]:-<empty>}, stderr: $(cat "$_se_crash"))"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
