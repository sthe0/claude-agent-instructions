#!/usr/bin/env bash
# Hermetic test for enter-task.sh + the git/github backends + the registry.
#
# Externals are stubbed via the *_BIN seams: GIT_BIN (records every call; answers
# rev-parse / worktree list; logs `worktree add`), GH_BIN (canned issue view /
# create). CLAUDE_PROJECT_PLUGIN_DIR points at an empty temp dir so no real
# machine plugin leaks in. No real git/gh/.claude is touched. Exit 0 on all-pass.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$HERE/../.." && pwd)"
ENTER="$SCRIPTS_DIR/enter-task.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAKE_TOPLEVEL="$TMP/myrepo"          # repo name = myrepo, parent = $TMP
mkdir -p "$FAKE_TOPLEVEL"
export GIT_CALLS="$TMP/git-calls.log"
export GH_CALLS="$TMP/gh-calls.log"
export WT_LIST="$TMP/wt-list.txt"     # existing worktrees, porcelain `worktree <path>`
: >"$GIT_CALLS"; : >"$GH_CALLS"; : >"$WT_LIST"

# ── Stub binaries ───────────────────────────────────────────────────────────
GITSTUB="$TMP/git-stub"; GHSTUB="$TMP/gh-stub"
cat >"$GITSTUB" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$GIT_CALLS"
# Strip optional '-C <dir>' prefix so matching works regardless of cwd context.
shift_n=0
[[ "\${1:-}" == "-C" ]] && shift_n=2
shift \$shift_n 2>/dev/null || true
case "\$1 \$2" in
  "rev-parse --show-toplevel") printf '%s\n' "$FAKE_TOPLEVEL" ;;
  "worktree list")             cat "$WT_LIST" ;;
  "worktree add")              : ;;   # recorded above; succeed silently
  *) : ;;
esac
EOF
cat >"$GHSTUB" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$GH_CALLS"
case "\$1" in
  issue)
    case "\$2" in
      view)   printf '%s\n' "Add the widget" ;;            # --jq .title output
      create) printf 'https://github.com/o/r/issues/42\n' ;;
    esac ;;
esac
EOF
chmod +x "$GITSTUB" "$GHSTUB"
export GIT_BIN="$GITSTUB" GH_BIN="$GHSTUB"
export CLAUDE_PROJECT_PLUGIN_DIR="$TMP/plugins"; mkdir -p "$CLAUDE_PROJECT_PLUGIN_DIR"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  [ OK ] %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1 — ($2)"; fi; }

run() { # run enter-task; capture final stdout line into REPLY, stderr discarded
  REPLY="$(bash "$ENTER" "$@" 2>>"$TMP/stderr.log" | tail -1)"
}

# 1. git default selected with no plugins; --name derives <repo>-<name> + dir.
: >"$GIT_CALLS"
run --name feat-x --dry-run
check "default git: --name feat-x prints .../myrepo-feat-x" \
  '[[ "$REPLY" == "$TMP/myrepo-feat-x" ]]'
check "dry-run --name: no \`worktree add\` recorded" \
  '! grep -q "worktree add" "$GIT_CALLS"'
check "dry-run --name: no gh calls at all" \
  '[[ ! -s "$GH_CALLS" ]]'

# 2. non-dry --name create: a `worktree add ... -b <branch>` IS recorded.
: >"$GIT_CALLS"; : >"$WT_LIST"
run --name feat-y
check "non-dry --name: prints .../myrepo-feat-y" \
  '[[ "$REPLY" == "$TMP/myrepo-feat-y" ]]'
check "non-dry --name: records \`worktree add\` on branch feat-y" \
  'grep -q "worktree add $TMP/myrepo-feat-y -b feat-y" "$GIT_CALLS"'

# 3. worktree-exists path -> reuse, NO `worktree add`.
: >"$GIT_CALLS"
printf 'worktree %s\n' "$TMP/myrepo-feat-z" >"$WT_LIST"
run --name feat-z
check "reuse: existing worktree records NO \`worktree add\`" \
  '! grep -q "worktree add" "$GIT_CALLS"'
check "reuse: still prints the dir" \
  '[[ "$REPLY" == "$TMP/myrepo-feat-z" ]]'

# 4. --new under CLAUDE_LAUNCH_ASSUME_YES=1 -> tracker create THEN proceeds.
: >"$GIT_CALLS"; : >"$GH_CALLS"; : >"$WT_LIST"
CLAUDE_LAUNCH_ASSUME_YES=1 run --new "Add the widget" --tracker github
check "--new: records a gh \`issue create\`" \
  'grep -q "issue create" "$GH_CALLS"'
check "--new: proceeds to a worktree named <key>-<slug>" \
  '[[ "$REPLY" == "$TMP/myrepo-42-add-the-widget" ]]'

# 4b. --new WITHOUT assume-yes is confirmation-gated (no create, nonzero).
: >"$GH_CALLS"
if bash "$ENTER" --new "Add the widget" --tracker github >/dev/null 2>&1; then
  bad "--new without assume-yes should be gated (nonzero)"
else
  ok "--new without assume-yes is confirmation-gated"
fi
check "gated --new: no gh \`issue create\` performed" \
  '! grep -q "issue create" "$GH_CALLS"'

# 5. --key resolves an existing issue via the tracker.
: >"$GH_CALLS"; : >"$WT_LIST"
run --key 7 --tracker github
check "--key 7: derives <key>-<slug> from issue title" \
  '[[ "$REPLY" == "$TMP/myrepo-7-add-the-widget" ]]'
check "--key 7: used gh \`issue view\`" \
  'grep -q "issue view 7" "$GH_CALLS"'

# 6. a fake plugin backend dropped in the plugin dir is discovered by name.
mkdir -p "$CLAUDE_PROJECT_PLUGIN_DIR/backends"
cat >"$CLAUDE_PROJECT_PLUGIN_DIR/backends/fake.sh" <<EOF
backend_detect() { return 0; }
backend_ensure_workspace() { printf '/plugin/ws-%s\n' "\$1"; }
backend_compose() { :; }
EOF
run --name plug-me --workspace fake --dry-run
check "plugin discovery: --workspace fake resolves the machine-local file" \
  '[[ "$REPLY" == "/plugin/ws-plug-me" ]]'

# 6c. empty-slug key path: a tracker whose resolve returns 'KEY<TAB>' (no slug,
#     e.g. a non-Latin summary that slugifies to nothing) must yield a dir named
#     <repo>-KEY with NO trailing dash.
mkdir -p "$CLAUDE_PROJECT_PLUGIN_DIR/trackers"
cat >"$CLAUDE_PROJECT_PLUGIN_DIR/trackers/emptyslug.sh" <<'EOF'
tracker_resolve() { printf '%s\t\n' "$1"; }   # key, empty slug
tracker_create()  { printf '%s\t\n' "PROJ-7"; }
EOF
: >"$WT_LIST"
run --key PROJ-7 --tracker emptyslug --dry-run
check "empty slug: key path yields <repo>-KEY with no trailing dash" \
  '[[ "$REPLY" == "$TMP/myrepo-PROJ-7" ]]'

# 7. dry-run end to end records zero EXTERNAL (mutating) calls.
: >"$GIT_CALLS"; : >"$GH_CALLS"; : >"$WT_LIST"
run --name nothing --dry-run
check "dry-run: zero mutating git calls" \
  '! grep -qE "worktree add" "$GIT_CALLS"'
check "dry-run: zero gh calls" \
  '[[ ! -s "$GH_CALLS" ]]'

# === Registry-wired tests ===================================================
# Set up isolated registry roots and blank identity so nothing from the real
# machine leaks into these tests.
TEST_SHARED="$TMP/test-shared"
TEST_LOCAL="$TMP/test-local"
WS_ROOT="$TMP/beta-ws"
mkdir -p "$TEST_SHARED/proj/alpha" "$TEST_SHARED/proj/beta" "$TEST_LOCAL" "$WS_ROOT"

printf '{"workspace_backend":"git","workspace_subpath":"proj/alpha","tracker_backend":"github","tracker_queue":"Q-ALPHA"}\n' \
  > "$TEST_SHARED/proj/alpha/agent-project.json"
printf '{"workspace_backend":"git","workspace_subpath":"proj/beta","workspace_path":"%s","tracker_backend":"github","tracker_queue":"Q-BETA"}\n' \
  "$WS_ROOT" > "$TEST_SHARED/proj/beta/agent-project.json"

FAKE_ID="$TMP/fake-identity.local"; : >"$FAKE_ID"
export CLAUDE_AGENT_IDENTITY="$FAKE_ID"
export CLAUDE_PROJECTS_DIR="$TEST_SHARED"
export CLAUDE_PROJECTS_LOCAL_DIR="$TEST_LOCAL"

# Helper that also captures stderr for asserting on log output.
run_log() {
  local logfile="$TMP/stderr-reg.log"
  : >"$logfile"
  REPLY="$(bash "$ENTER" "$@" 2>"$logfile" | tail -1)"
  LAST_STDERR="$(cat "$logfile")"
}

# 8. --list-projects prints the keyed table with registered records.
list_out="$(bash "$ENTER" --list-projects 2>/dev/null)"
list_rc=$?
check "--list-projects exits 0" '[[ $list_rc -eq 0 ]]'
check "--list-projects shows proj/alpha and Q-ALPHA" \
  'grep -q "proj/alpha" <<<"$list_out" && grep -q "Q-ALPHA" <<<"$list_out"'
check "--list-projects shows proj/beta and Q-BETA" \
  'grep -q "proj/beta" <<<"$list_out" && grep -q "Q-BETA" <<<"$list_out"'

# 9. --register writes a local record that is then resolvable.
: >"$GIT_CALLS"; : >"$WT_LIST"
MY_WS="$TMP/my-ws"; mkdir -p "$MY_WS"
bash "$ENTER" --register "$MY_WS" --as "test/proj" 2>/dev/null
check "--register: agent-project.json written in local root" \
  '[[ -f "$TEST_LOCAL/test/proj/agent-project.json" ]]'
reg_json="$(cat "$TEST_LOCAL/test/proj/agent-project.json" 2>/dev/null)"
check "--register: workspace_path recorded" \
  'grep -q "workspace_path" <<<"$reg_json"'

# 9b. Registered record is resolvable; worktree rooted at workspace_path.
: >"$GIT_CALLS"; : >"$WT_LIST"
run_log --name "feat-r" --project "test/proj" --dry-run
check "--project 'test/proj': resolves to record (project key in log)" \
  'grep -q "project=test/proj" <<<"$LAST_STDERR"'
check "--project 'test/proj': worktree rooted at registered workspace_path" \
  '[[ "$REPLY" == "$TMP/my-ws-feat-r" ]]'

# 10. --project proj/alpha sets subpath + queue visible in the dry-run log.
: >"$GIT_CALLS"; : >"$WT_LIST"
run_log --name "task-a" --project "proj/alpha" --dry-run
check "--project proj/alpha: queue=Q-ALPHA in log" \
  'grep -q "queue=Q-ALPHA" <<<"$LAST_STDERR"'
check "--project proj/alpha: subpath=proj/alpha in log" \
  'grep -q "subpath=proj/alpha" <<<"$LAST_STDERR"'

# 11. old --project as subpath-appended-to-dir: verify it is GONE.
# The project_dir should be the worktree root, not worktree/proj/alpha.
check "--project no longer appends subpath to project_dir" \
  '[[ "$REPLY" == "$TMP/myrepo-task-a" ]]'

# 12. --new from a no-record cwd with no explicit --tracker aborts with the list.
NOWHERE="$TMP/nowhere-cwd"; mkdir -p "$NOWHERE"
# Use a detector stub that returns 'git github' so there IS a tracker detected
# (but no project resolves). The guard must still fire because no --tracker flag.
FAKE_DET="$TMP/det-github.py"
printf 'print("git github")\n' > "$FAKE_DET"
abort_out="$(cd "$NOWHERE" && CLAUDE_BACKEND_DETECTOR="$FAKE_DET" \
  bash "$ENTER" --new "task" 2>&1 >/dev/null)"
abort_rc=$?
check "--new from no-record cwd exits non-zero" '[[ $abort_rc -ne 0 ]]'
check "--new from no-record cwd prints project list (proj/alpha)" \
  'grep -q "proj/alpha" <<<"$abort_out"'

# 13. --new with explicit --tracker bypasses the empty-context guard.
: >"$GIT_CALLS"; : >"$GH_CALLS"; : >"$WT_LIST"
CLAUDE_LAUNCH_ASSUME_YES=1 run_log --new "Add the widget" --tracker github
check "--new with explicit --tracker bypasses empty-context guard" \
  'grep -q "issue create" "$GH_CALLS"'

echo
printf 'enter-task tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
