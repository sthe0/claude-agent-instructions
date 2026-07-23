#!/usr/bin/env bash
# Hermetic tests for the optional tracker_plan_marker verb (github.sh):
# concatenates a ticket's posted comment bodies, in chronological order,
# for verify-ticket-plan-sync.py's --comment-file - mode to consume.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$HERE/../.." && pwd)"
GITHUB_SH="$SCRIPTS_DIR/project_entry/trackers/github.sh"
VERIFY_SYNC_PY="$SCRIPTS_DIR/verify-ticket-plan-sync.py"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf '  [ OK ] %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1 — ($2)"; fi; }

# shellcheck source=/dev/null
source "$GITHUB_SH"

# --- case a: multi-comment success — concatenated bodies in order --------
GHSTUB_OK="$TMP/gh-stub-ok"
cat >"$GHSTUB_OK" <<'EOF'
#!/usr/bin/env bash
cat <<'JSON'
{"comments":[{"body":"first comment"},{"body":"second comment"}]}
JSON
EOF
chmod +x "$GHSTUB_OK"

GH_BIN="$GHSTUB_OK"
out="$(tracker_plan_marker 42)"; rc=$?
expected=$'first comment\nsecond comment'
check "tracker_plan_marker success: exit 0"                  '[[ $rc -eq 0 ]]'
check "tracker_plan_marker success: concatenated in order"   '[[ "$out" == "$expected" ]]'

# --- case b: zero comments -> success, empty stdout (NOT a failure) ------
GHSTUB_EMPTY="$TMP/gh-stub-empty"
cat >"$GHSTUB_EMPTY" <<'EOF'
#!/usr/bin/env bash
cat <<'JSON'
{"comments":[]}
JSON
EOF
chmod +x "$GHSTUB_EMPTY"

GH_BIN="$GHSTUB_EMPTY"
out="$(tracker_plan_marker 42)"; rc=$?
check "tracker_plan_marker zero-comments: exit 0"       '[[ $rc -eq 0 ]]'
check "tracker_plan_marker zero-comments: empty stdout" '[[ -z "$out" ]]'

# --- case c: gh absent (127) normalizes to exit 1 -------------------------
GH_BIN="$TMP/nonexistent-gh-binary"
out="$(tracker_plan_marker 1 2>"$TMP/stderr-127.log")"; rc=$?
check "tracker_plan_marker: gh binary absent (127) normalizes to exit 1" '[[ $rc -eq 1 ]]'
check "tracker_plan_marker: empty stdout on gh-absent"                   '[[ -z "$out" ]]'
check "tracker_plan_marker: stderr reason present on gh-absent"          '[[ -s "$TMP/stderr-127.log" ]]'

# --- case d: gh auth failure (4) normalizes to exit 1 ---------------------
GHSTUB_AUTHFAIL="$TMP/gh-stub-authfail"
cat >"$GHSTUB_AUTHFAIL" <<'EOF'
#!/usr/bin/env bash
exit 4
EOF
chmod +x "$GHSTUB_AUTHFAIL"
GH_BIN="$GHSTUB_AUTHFAIL"
out="$(tracker_plan_marker 1 2>"$TMP/stderr-4.log")"; rc=$?
check "tracker_plan_marker: gh auth failure (4) normalizes to exit 1" '[[ $rc -eq 1 ]]'
check "tracker_plan_marker: empty stdout on gh auth failure"          '[[ -z "$out" ]]'
check "tracker_plan_marker: stderr reason present on gh auth failure" '[[ -s "$TMP/stderr-4.log" ]]'

# --- case e: CLAUDE_DRY_RUN -> exit 1, nothing on stdout, one stderr line -
GH_BIN="$GHSTUB_OK"
CLAUDE_DRY_RUN=1
out="$(tracker_plan_marker 42 2>"$TMP/stderr-dry.log")"; rc=$?
unset CLAUDE_DRY_RUN
check "tracker_plan_marker: CLAUDE_DRY_RUN normalizes to exit 1" '[[ $rc -eq 1 ]]'
check "tracker_plan_marker: CLAUDE_DRY_RUN prints nothing on stdout" '[[ -z "$out" ]]'
check "tracker_plan_marker: CLAUDE_DRY_RUN writes exactly one stderr line" \
  '[[ $(wc -l <"$TMP/stderr-dry.log") -eq 1 ]]'

# --- case f: a backend that omits tracker_plan_marker degrades WITHOUT
#             being invoked (presence-probe contract) ---------------------
FAKE_TR="$TMP/faketracker.sh"
cat >"$FAKE_TR" <<'EOF'
tracker_resolve() { :; }
tracker_create()  { :; }
EOF
bash -c "source '$FAKE_TR'; declare -F tracker_plan_marker >/dev/null"
rc=$?
check "stub backend omitting tracker_plan_marker: declare -F reports absent" '[[ $rc -ne 0 ]]'

# --- case g: end-to-end single marker — output round-trips through the
#             comparator and reports OK ------------------------------------
FIXTURE_TOML="$TMP/plan-fixture.toml"
cat >"$FIXTURE_TOML" <<'EOF'
[[stage]]
index = 1
title = "marker verb fixture stage"
EOF
marker_line="$(python3 "$VERIFY_SYNC_PY" --emit-marker --plan "$FIXTURE_TOML")"

GHSTUB_MARKER="$TMP/gh-stub-marker"
cat >"$GHSTUB_MARKER" <<EOF
#!/usr/bin/env bash
cat <<'JSON'
{"comments":[{"body":"Approved plan.\n\n$marker_line"}]}
JSON
EOF
chmod +x "$GHSTUB_MARKER"

GH_BIN="$GHSTUB_MARKER"
out="$(tracker_plan_marker 42)"; rc=$?
verify_out="$(printf '%s' "$out" | python3 "$VERIFY_SYNC_PY" --plan "$FIXTURE_TOML" --comment-file - 2>&1)"; verify_rc=$?
check "tracker_plan_marker e2e: verb succeeded"        '[[ $rc -eq 0 ]]'
check "tracker_plan_marker e2e: comparator reports OK" '[[ $verify_rc -eq 0 ]] && printf "%s" "$verify_out" | grep -q "OK"'

# --- case h: stale-then-fresh multi-comment ordering — the comparator's
#             last-marker-wins scan picks the FRESH marker, not the STALE
#             one, when both are present in realistic chronological order --
FIXTURE_OLD_TOML="$TMP/plan-fixture-old.toml"
cat >"$FIXTURE_OLD_TOML" <<'EOF'
[[stage]]
index = 1
title = "an older fixture stage, deliberately different bytes"
EOF
stale_marker_line="$(python3 "$VERIFY_SYNC_PY" --emit-marker --plan "$FIXTURE_OLD_TOML")"
fresh_marker_line="$marker_line"

GHSTUB_MULTI="$TMP/gh-stub-multi"
cat >"$GHSTUB_MULTI" <<EOF
#!/usr/bin/env bash
cat <<'JSON'
{"comments":[{"body":"plain comment, no marker"},{"body":"Approved plan (old).\n\n$stale_marker_line"},{"body":"Approved plan (current).\n\n$fresh_marker_line"}]}
JSON
EOF
chmod +x "$GHSTUB_MULTI"

GH_BIN="$GHSTUB_MULTI"
out="$(tracker_plan_marker 42)"; rc=$?
verify_out_multi="$(printf '%s' "$out" | python3 "$VERIFY_SYNC_PY" --plan "$FIXTURE_TOML" --comment-file - 2>&1)"; verify_multi_rc=$?
check "tracker_plan_marker multi: verb succeeded"                   '[[ $rc -eq 0 ]]'
check "tracker_plan_marker multi: raw stdout contains stale marker" 'printf "%s" "$out" | grep -qF "$stale_marker_line"'
check "tracker_plan_marker multi: raw stdout contains fresh marker" 'printf "%s" "$out" | grep -qF "$fresh_marker_line"'
check "tracker_plan_marker multi: comparator picks the FRESH marker (OK)" \
  '[[ $verify_multi_rc -eq 0 ]] && printf "%s" "$verify_out_multi" | grep -q "OK"'

printf 'tracker-plan-marker tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
