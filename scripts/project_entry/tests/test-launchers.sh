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

# ── Source the launchers (profile dir is already set via env) ────────────────
# shellcheck source=scripts/claude-launchers.sh
source "$SCRIPTS_DIR/claude-launchers.sh"

# ── Helper: reset per-test log files ─────────────────────────────────────────
reset_logs() {
  : >"$ET_CALLS" >"$CLAUDE_ENV_RECORDED" >"$CLAUDE_ARGS"
}

# ═══════════════════════════════════════════════════════════════════════════
# Test 1 — all expected functions are defined
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- function presence ---\n'
for _fn in claude-task claude-eliza claude-team claude-personal; do
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

# Test 2d — empty args -> --reuse
reset_logs
CLAUDE_LAUNCH_DRYRUN=1 claude-task >/dev/null 2>&1 || true
if grep -qF -- '--reuse' "$ET_CALLS"; then
  ok "empty args classified as --reuse"
else
  fail "empty args not classified as --reuse (et-calls: $(cat "$ET_CALLS"))"
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
# Summary
# ═══════════════════════════════════════════════════════════════════════════
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
