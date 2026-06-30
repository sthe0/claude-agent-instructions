#!/usr/bin/env bash
# Hermetic test for the two-root project registry (projects.py + projects.sh).
#
# Drives the projects.sh seam over TEMP roots only — no network, no real
# registry, no real HOME. Covers: resolve-by-name (key and workspace_path),
# resolve-by-PWD (path-suffix + longest-match tiebreak), empty resolve, the
# list table, malformed-JSON skip-with-warning (no crash), two-root merge by
# key (a local workspace_path completes a shared portable record), and
# register writing a local record that is then resolvable.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$HERE/../projects.sh"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  [ OK ] %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SHARED="$TMP/shared"
LOCAL="$TMP/local"

# rec <root> <key> <json>  — write an agent-project.json record.
rec() {
  local dir="$1/$2"
  mkdir -p "$dir"
  printf '%s\n' "$3" > "$dir/agent-project.json"
}

# Shared, portable records (no machine-local absolute paths).
rec "$SHARED" "alpha/one" '{"workspace_subpath":"alpha/one","workspace_backend":"git","tracker_backend":"github","tracker_queue":"Q-ALPHA"}'
rec "$SHARED" "beta/two"  '{"workspace_subpath":"beta/two","tracker_queue":"Q-BETA"}'
rec "$SHARED" "short"     '{"workspace_subpath":"one","tracker_queue":"Q-SHORT"}'
# A malformed record — must be skipped with a warning, never crash the load.
mkdir -p "$SHARED/broken"
printf '%s\n' 'not json {' > "$SHARED/broken/agent-project.json"

# Machine-local record COMPLETES the shared beta/two with an absolute path.
rec "$LOCAL" "beta/two" '{"workspace_path":"/abs/checkout/beta"}'

# Point the seam at the temp roots (shared first, machine-local last).
export CLAUDE_PROJECTS_DIR="$SHARED"
export CLAUDE_PROJECTS_LOCAL_DIR="$LOCAL"
unset CLAUDE_PROJECT_ROOTS CLAUDE_AGENT_IDENTITY 2>/dev/null || true

# ── 1. root ordering (shared then machine-local) ────────────────────────────
roots_out="$(project_roots)"
if [[ "$roots_out" == "$SHARED"$'\n'"$LOCAL" ]]; then
  ok "project_roots: shared then machine-local"
else
  bad "project_roots ordering — got: $(printf '%s' "$roots_out" | tr '\n' '|')"
fi

# identity projects_dir is the shared fallback when CLAUDE_PROJECTS_DIR is unset.
idf="$TMP/identity.local"
printf 'projects_dir=%s\n' "$SHARED" > "$idf"
got_id="$(CLAUDE_PROJECTS_DIR='' CLAUDE_AGENT_IDENTITY="$idf" project_roots | head -1)"
if [[ "$got_id" == "$SHARED" ]]; then ok "project_roots: identity projects_dir fallback"
else bad "identity fallback — got '$got_id'"; fi

# ── 2. list table (skips malformed, warns, exits 0) ─────────────────────────
warn_log="$TMP/warn.log"
list_out="$(project_list 2>"$warn_log")"; list_rc=$?
if [[ $list_rc -eq 0 ]]; then ok "project_list exits 0 despite malformed record"
else bad "project_list rc=$list_rc"; fi
if grep -q 'alpha/one' <<<"$list_out" && grep -q 'beta/two' <<<"$list_out" && grep -q 'Q-ALPHA' <<<"$list_out"; then
  ok "project_list shows keyed rows (alpha/one, beta/two, Q-ALPHA)"
else
  bad "project_list table missing rows — got: $list_out"
fi
if grep -qi 'malformed' "$warn_log"; then ok "malformed record warned on stderr"
else bad "no malformed-JSON warning — stderr: $(cat "$warn_log")"; fi

# ── 3. resolve by name (key) ────────────────────────────────────────────────
got="$(project_resolve "alpha/one")"
if [[ "$got" == "alpha/one" ]]; then ok "resolve by key -> alpha/one"
else bad "resolve by key — got '$got'"; fi

# ── 4. resolve by name (workspace_path, proves two-root merge) ──────────────
# /abs/checkout/beta lives ONLY in the machine-local record; resolving it by
# that path proves the local record merged onto the shared beta/two.
got="$(project_resolve "/abs/checkout/beta")"
if [[ "$got" == "beta/two" ]]; then ok "resolve by workspace_path -> beta/two (two-root merge)"
else bad "resolve by workspace_path — got '$got'"; fi

# ── 5. resolve by PWD (path-suffix + longest-match tiebreak) ────────────────
# pwd ends in alpha/one; both 'alpha/one' and 'one' (the 'short' record) are
# path-suffixes — the longer 'alpha/one' must win.
wt="$TMP/wt/alpha/one"
mkdir -p "$wt"
got="$(cd "$wt" && project_resolve)"
if [[ "$got" == "alpha/one" ]]; then ok "resolve by PWD suffix, longest match -> alpha/one"
else bad "resolve by PWD — got '$got'"; fi

# A pwd ending only in 'one' resolves to the shorter 'short' record.
wt2="$TMP/wt2/foo/one"
mkdir -p "$wt2"
got="$(cd "$wt2" && project_resolve)"
if [[ "$got" == "short" ]]; then ok "resolve by PWD suffix -> short (key 'short', subpath 'one')"
else bad "resolve by PWD short — got '$got'"; fi

# ── 6. empty resolve (no selector, unrelated pwd) ───────────────────────────
nowhere="$TMP/elsewhere/nope"
mkdir -p "$nowhere"
if (cd "$nowhere" && project_resolve) >/dev/null 2>&1; then
  bad "empty resolve unexpectedly matched"
else
  ok "empty resolve returns non-zero (no match)"
fi

# ── 7. register writes a local record that is then resolvable ───────────────
reg_path="$(project_register "$LOCAL" "delta/new" "workspace_subpath=delta/new" "tracker_queue=Q-DELTA")"
if [[ -f "$reg_path" ]] && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$reg_path" 2>/dev/null; then
  ok "project_register wrote valid JSON at $reg_path"
else
  bad "project_register did not write valid JSON (path: $reg_path)"
fi
got="$(project_resolve "delta/new")"
if [[ "$got" == "delta/new" ]]; then ok "registered record is resolvable by key"
else bad "registered record not resolvable — got '$got'"; fi
if project_list 2>/dev/null | grep -q 'Q-DELTA'; then ok "registered record appears in list (Q-DELTA)"
else bad "registered record missing from list"; fi

echo
printf 'projects tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
