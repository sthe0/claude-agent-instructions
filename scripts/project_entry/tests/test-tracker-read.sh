#!/usr/bin/env bash
# Hermetic tests for the optional tracker_read verb (github.sh) and the
# declare -F presence-probe / none-sentinel short-circuit it relies on.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$HERE/../.." && pwd)"
GITHUB_SH="$SCRIPTS_DIR/project_entry/trackers/github.sh"
ENTER="$SCRIPTS_DIR/enter-task.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  [ OK ] %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1 — ($2)"; fi; }

# shellcheck source=/dev/null
source "$GITHUB_SH"

# --- case 1: rendered flat-text shape on success -----------------------------
GHSTUB_OK="$TMP/gh-stub-ok"
cat >"$GHSTUB_OK" <<'EOF'
#!/usr/bin/env bash
cat <<'JSON'
{"title":"Add the widget","state":"OPEN","author":{"login":"alice"},"body":"Please add a widget.","comments":[{"author":{"login":"bob"},"body":"LGTM","createdAt":"2024-01-02T03:04:05Z"}]}
JSON
EOF
chmod +x "$GHSTUB_OK"

GH_BIN="$GHSTUB_OK"
out="$(tracker_read 42)"; rc=$?
check "tracker_read success: exit 0"                  '[[ $rc -eq 0 ]]'
check "tracker_read success: renders title"           'grep -qx "title: Add the widget" <<<"$out"'
check "tracker_read success: renders status"          'grep -qx "status: OPEN" <<<"$out"'
check "tracker_read success: renders author"          'grep -qx "author: alice" <<<"$out"'
check "tracker_read success: description marker"      'grep -qx "\-\-\- description \-\-\-" <<<"$out"'
check "tracker_read success: description body"        'grep -qx "Please add a widget." <<<"$out"'
check "tracker_read success: comment marker"          'grep -qx "\-\-\- comment 1 by bob at 2024-01-02T03:04:05Z \-\-\-" <<<"$out"'
check "tracker_read success: comment body"            'grep -qx "LGTM" <<<"$out"'

# --- case 2: a backend that omits tracker_read degrades WITHOUT being invoked ---
# Fresh bash process that sources only a stub tracker lacking tracker_read —
# this is the corrected replacement for the superseded draft's impossible
# trackers/none.sh test (D1): "none" is a sentinel string, not a file.
FAKE_TR="$TMP/faketracker.sh"
cat >"$FAKE_TR" <<'EOF'
tracker_resolve() { :; }
tracker_create()  { :; }
EOF
bash -c "source '$FAKE_TR'; declare -F tracker_read >/dev/null"
rc=$?
check "stub backend omitting tracker_read: declare -F reports absent" '[[ $rc -ne 0 ]]'

# --- case 3: gh absent (exit 127) normalizes to tracker_read exit 1 ----------
GH_BIN="$TMP/nonexistent-gh-binary"
out="$(tracker_read 1 2>"$TMP/stderr-127.log")"; rc=$?
check "tracker_read: gh binary absent (127) normalizes to exit 1" '[[ $rc -eq 1 ]]'
check "tracker_read: empty stdout on gh-absent"                   '[[ -z "$out" ]]'
check "tracker_read: stderr reason present on gh-absent"          '[[ -s "$TMP/stderr-127.log" ]]'

# --- case 4: gh auth failure (exit 4) normalizes to tracker_read exit 1 ------
GHSTUB_AUTHFAIL="$TMP/gh-stub-authfail"
cat >"$GHSTUB_AUTHFAIL" <<'EOF'
#!/usr/bin/env bash
exit 4
EOF
chmod +x "$GHSTUB_AUTHFAIL"
GH_BIN="$GHSTUB_AUTHFAIL"
out="$(tracker_read 1 2>"$TMP/stderr-4.log")"; rc=$?
check "tracker_read: gh auth failure (4) normalizes to exit 1" '[[ $rc -eq 1 ]]'
check "tracker_read: empty stdout on gh auth failure"          '[[ -z "$out" ]]'
check "tracker_read: stderr reason present on gh auth failure" '[[ -s "$TMP/stderr-4.log" ]]'

# --- case 5: CLAUDE_DRY_RUN -> exit 1, nothing on stdout, one stderr line ----
GH_BIN="$GHSTUB_OK"
CLAUDE_DRY_RUN=1
out="$(tracker_read 42 2>"$TMP/stderr-dry.log")"; rc=$?
unset CLAUDE_DRY_RUN
check "tracker_read: CLAUDE_DRY_RUN normalizes to exit 1" '[[ $rc -eq 1 ]]'
check "tracker_read: CLAUDE_DRY_RUN prints nothing on stdout" '[[ -z "$out" ]]'
check "tracker_read: CLAUDE_DRY_RUN writes exactly one stderr line" \
  '[[ $(wc -l <"$TMP/stderr-dry.log") -eq 1 ]]'

# --- case 6: the "none" sentinel short-circuits at enter-task.sh:211 --------
# before any tracker backend is ever sourced (D1: NOT the --key/--new
# die-guards at :231/:237 — a different control that rejects the sentinel
# for those selectors). --name forces tr_name="none" regardless of what a
# tracker backend name would otherwise resolve to (enter-task.sh:168), so a
# sentinel backend dropped in the plugin dir must never be sourced.
PLUGIN_DIR="$TMP/plugins"
mkdir -p "$PLUGIN_DIR/trackers"
SENTINEL_MARK="$TMP/sentinel-sourced"
cat >"$PLUGIN_DIR/trackers/sentinel.sh" <<EOF
touch "$SENTINEL_MARK"
tracker_resolve() { :; }
tracker_create()  { :; }
EOF
rm -f "$SENTINEL_MARK"

GITSTUB="$TMP/git-stub"
cat >"$GITSTUB" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
  "rev-parse --show-toplevel") printf '%s\n' "$CLAUDE_WORKSPACE_ROOT" ;;
  "worktree list") ;;
  "worktree add") ;;
  *) ;;
esac
exit 0
EOF
chmod +x "$GITSTUB"

REPO_DIR="$TMP/repo"
mkdir -p "$REPO_DIR"

CLAUDE_PROJECT_PLUGIN_DIR="$PLUGIN_DIR" \
  CLAUDE_TRACKER_BACKEND="sentinel" \
  CLAUDE_WORKSPACE_ROOT="$REPO_DIR" \
  GIT_BIN="$GITSTUB" \
  bash "$ENTER" --name whatever --dry-run >/dev/null 2>"$TMP/enter-stderr.log"

check "none sentinel: --name selector never sources the sentinel tracker backend" \
  '[[ ! -f "$SENTINEL_MARK" ]]'

printf 'tracker-read tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
