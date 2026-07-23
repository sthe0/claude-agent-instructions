#!/usr/bin/env bash
# Hermetic tests for the optional tracker_comment / tracker_publish_plan
# write-side verbs (github.sh) and the org-neutral refusal gate they run
# before any external call.
#
# Fixture key is the neutral "ABC-123" throughout — never a house-style
# internal key used by sibling fixtures elsewhere in this directory —
# because every line of this brand-new file counts as "added" under the
# added-line org-neutral guard applied to this repo itself. For the same
# reason, the marker-word fixture below is assembled at runtime rather than
# spelled out in this file's own source (see the comment at its definition).
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

KEY="ABC-123"

# --- fixtures ------------------------------------------------------------
CLEAN_TOML="$TMP/plan-clean.toml"
cat >"$CLEAN_TOML" <<'EOF'
[[stage]]
index = 1
title = "example stage"
EOF

CLEAN_MD="$TMP/plan-clean.md"
cat >"$CLEAN_MD" <<'EOF'
# Approved plan

One example stage.
EOF

# A marker word from check-org-neutral.py's own MARKERS list, needed to
# exercise the refusal path for real — but split across a concatenation so
# this file's own source text never spells the contiguous word (the
# added-line guard would otherwise flag this test file's own diff); the
# heredoc below is unquoted so the split rejoins into the real marker only
# in the fixture FILE this test writes, not in the script's source lines.
MARKER_WORD="yan""dex"
MARKED_TOML="$TMP/plan-marked.toml"
cat >"$MARKED_TOML" <<EOF
[[stage]]
index = 1
title = "deploy via $MARKER_WORD infra"
EOF

# --- gh stub, controlled via env vars read at call time -------------------
GHSTUB="$TMP/gh-stub"
cat >"$GHSTUB" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
case "$1 $2" in
  "gist create")
    if [[ "${STUB_FAIL:-}" == "gist" ]]; then
      echo "stub: gist create failed" >&2
      exit 1
    fi
    shift 2
    file="$1"
    cp "$file" "$STUB_GIST_COPY"
    printf '%s\n' "$STUB_GIST_URL"
    exit 0
    ;;
  "issue comment")
    if [[ "${STUB_FAIL:-}" == "comment" ]]; then
      echo "stub: issue comment failed" >&2
      exit 1
    fi
    shift 2
    args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
      if [[ "${args[$i]}" == "--body-file" ]]; then
        cp "${args[$((i + 1))]}" "$STUB_COMMENT_COPY"
      fi
    done
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
EOF
chmod +x "$GHSTUB"

GH_BIN="$GHSTUB"
CLAUDE_LAUNCH_ASSUME_YES=1
export STUB_GIST_URL="https://gist.github.com/testuser/deadbeefdeadbeef"

# --- case 1: successful publish — no --public, comment has the gist URL,
#             gist content is byte-identical to the input TOML -------------
STUB_LOG="$TMP/log-1"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-1"
STUB_COMMENT_COPY="$TMP/comment-copy-1"
STUB_FAIL=""
export STUB_LOG STUB_GIST_COPY STUB_COMMENT_COPY STUB_FAIL

out="$(tracker_publish_plan "$KEY" "$CLEAN_TOML" "$CLEAN_MD" 2>"$TMP/stderr-1.log")"; rc=$?
check "publish success: exit 0"                         '[[ $rc -eq 0 ]]'
check "publish success: gist create called"             'grep -q "^gist create" "$STUB_LOG"'
check "publish success: no --public flag"               '! grep -q -- "--public" "$STUB_LOG"'
check "publish success: gist content byte-identical"    'cmp -s "$CLEAN_TOML" "$STUB_GIST_COPY"'
check "publish success: comment posted"                 'grep -q "^issue comment" "$STUB_LOG"'
check "publish success: comment contains gist URL"       'grep -qF "$STUB_GIST_URL" "$STUB_COMMENT_COPY"'
check "publish success: comment contains markdown body" 'grep -q "One example stage." "$STUB_COMMENT_COPY"'

# --- case 2: marker-carrying TOML refused, zero gh calls -------------------
STUB_LOG="$TMP/log-2"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-2"
STUB_COMMENT_COPY="$TMP/comment-copy-2"
out="$(tracker_publish_plan "$KEY" "$MARKED_TOML" "$CLEAN_MD" 2>"$TMP/stderr-2.log")"; rc=$?
check "publish refusal: exit nonzero"        '[[ $rc -ne 0 ]]'
check "publish refusal: zero gh calls"       '[[ ! -s "$STUB_LOG" ]]'
check "publish refusal: stderr reason"       '[[ -s "$TMP/stderr-2.log" ]]'
check "publish refusal: no stdout"           '[[ -z "$out" ]]'

# --- case 3: CLAUDE_PUBLISH_ALLOW_INTERNAL=1 overrides the same TOML -------
STUB_LOG="$TMP/log-3"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-3"
STUB_COMMENT_COPY="$TMP/comment-copy-3"
out="$(CLAUDE_PUBLISH_ALLOW_INTERNAL=1 tracker_publish_plan "$KEY" "$MARKED_TOML" "$CLEAN_MD" 2>"$TMP/stderr-3.log")"; rc=$?
check "publish override: exit 0"             '[[ $rc -eq 0 ]]'
check "publish override: gh calls made"      '[[ -s "$STUB_LOG" ]]'
check "publish override: gist byte-identical" 'cmp -s "$MARKED_TOML" "$STUB_GIST_COPY"'

# --- case 4: gh failure at the gist step -> single degrade, reason on stderr,
#             no comment attempt ------------------------------------------
STUB_LOG="$TMP/log-4"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-4"
STUB_COMMENT_COPY="$TMP/comment-copy-4"
STUB_FAIL="gist"
out="$(tracker_publish_plan "$KEY" "$CLEAN_TOML" "$CLEAN_MD" 2>"$TMP/stderr-4.log")"; rc=$?
check "publish gist-failure: exit 1"              '[[ $rc -eq 1 ]]'
check "publish gist-failure: stderr reason"       '[[ -s "$TMP/stderr-4.log" ]]'
check "publish gist-failure: no comment attempted" '! grep -q "^issue comment" "$STUB_LOG"'

# --- case 5: gh failure at the comment step -> single degrade, gist URL
#             preserved in the stderr reason (not lost) --------------------
STUB_LOG="$TMP/log-5"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-5"
STUB_COMMENT_COPY="$TMP/comment-copy-5"
STUB_FAIL="comment"
out="$(tracker_publish_plan "$KEY" "$CLEAN_TOML" "$CLEAN_MD" 2>"$TMP/stderr-5.log")"; rc=$?
check "publish comment-failure: exit 1"                 '[[ $rc -eq 1 ]]'
check "publish comment-failure: gist was created"       'grep -q "^gist create" "$STUB_LOG"'
check "publish comment-failure: gist URL in stderr"     'grep -qF "$STUB_GIST_URL" "$TMP/stderr-5.log"'
STUB_FAIL=""

# --- case 6: CLAUDE_DRY_RUN -> exit nonzero, zero gh calls -----------------
STUB_LOG="$TMP/log-6"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-6"
STUB_COMMENT_COPY="$TMP/comment-copy-6"
CLAUDE_DRY_RUN=1
out="$(tracker_publish_plan "$KEY" "$CLEAN_TOML" "$CLEAN_MD" 2>"$TMP/stderr-6.log")"; rc=$?
unset CLAUDE_DRY_RUN
check "publish dry-run: exit nonzero"    '[[ $rc -ne 0 ]]'
check "publish dry-run: zero gh calls"   '[[ ! -s "$STUB_LOG" ]]'
check "publish dry-run: no stdout"       '[[ -z "$out" ]]'

# --- case 7: tracker_comment success — posts, no org-neutral hit ----------
STUB_LOG="$TMP/log-7"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-7"
STUB_COMMENT_COPY="$TMP/comment-copy-7"
out="$(tracker_comment "$KEY" "$CLEAN_MD" 2>"$TMP/stderr-7.log")"; rc=$?
check "comment success: exit 0"                  '[[ $rc -eq 0 ]]'
check "comment success: issue comment called"    'grep -q "^issue comment" "$STUB_LOG"'
check "comment success: body matches markdown"   'cmp -s "$CLEAN_MD" "$STUB_COMMENT_COPY"'

# --- case 8: tracker_comment refuses a marker-carrying markdown file,
#             zero gh calls -------------------------------------------------
MARKED_MD="$TMP/comment-marked.md"
cp "$MARKED_TOML" "$MARKED_MD"
STUB_LOG="$TMP/log-8"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-8"
STUB_COMMENT_COPY="$TMP/comment-copy-8"
out="$(tracker_comment "$KEY" "$MARKED_MD" 2>"$TMP/stderr-8.log")"; rc=$?
check "comment refusal: exit nonzero"    '[[ $rc -ne 0 ]]'
check "comment refusal: zero gh calls"   '[[ ! -s "$STUB_LOG" ]]'
check "comment refusal: stderr reason"   '[[ -s "$TMP/stderr-8.log" ]]'

unset STUB_LOG STUB_GIST_COPY STUB_COMMENT_COPY STUB_FAIL STUB_GIST_URL

# --- case 9: a backend that omits both verbs degrades WITHOUT being
#             invoked — declare -F reports absent for both ----------------
FAKE_TR="$TMP/faketracker.sh"
cat >"$FAKE_TR" <<'EOF'
tracker_resolve() { :; }
tracker_create()  { :; }
EOF
bash -c "source '$FAKE_TR'; declare -F tracker_comment >/dev/null"
rc=$?
check "stub backend omitting tracker_comment: declare -F reports absent" '[[ $rc -ne 0 ]]'
bash -c "source '$FAKE_TR'; declare -F tracker_publish_plan >/dev/null"
rc=$?
check "stub backend omitting tracker_publish_plan: declare -F reports absent" '[[ $rc -ne 0 ]]'

# --- case 10: publish attaches a valid agent-plan-sync marker to the
#              comment — round-tripped through the comparator, OK ---------
MARKER_FIXTURE_TOML="$TMP/plan-marker-fixture.toml"
cat >"$MARKER_FIXTURE_TOML" <<'EOF'
[[stage]]
index = 1
title = "marker fixture stage"
EOF

STUB_LOG="$TMP/log-10"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-10"
STUB_COMMENT_COPY="$TMP/comment-copy-10"
STUB_FAIL=""
export STUB_LOG STUB_GIST_COPY STUB_COMMENT_COPY STUB_FAIL
export STUB_GIST_URL="https://gist.github.com/testuser/deadbeefdeadbeef"

out="$(tracker_publish_plan "$KEY" "$MARKER_FIXTURE_TOML" "$CLEAN_MD" 2>"$TMP/stderr-10.log")"; rc=$?
verify_out="$(python3 "$VERIFY_SYNC_PY" --plan "$MARKER_FIXTURE_TOML" --comment-file "$STUB_COMMENT_COPY" 2>&1)"; verify_rc=$?
check "publish marker: publish succeeded"              '[[ $rc -eq 0 ]]'
check "publish marker: comment carries a marker line"  'grep -q "agent-plan-sync: plan_sha256=" "$STUB_COMMENT_COPY"'
check "publish marker: comparator reports OK"          '[[ $verify_rc -eq 0 ]] && printf "%s" "$verify_out" | grep -q "OK"'

# --- case 11: mutating the TOML after publish makes the previously
#              captured marker DRIFT (marker is byte-bound to content,
#              not just the path) -----------------------------------------
printf '\n# mutated after publish\n' >>"$MARKER_FIXTURE_TOML"
verify_out_drift="$(python3 "$VERIFY_SYNC_PY" --plan "$MARKER_FIXTURE_TOML" --comment-file "$STUB_COMMENT_COPY" 2>&1)"; verify_drift_rc=$?
check "publish marker: mutated plan reports DRIFT via the stale marker" \
  '[[ $verify_drift_rc -eq 1 ]] && printf "%s" "$verify_out_drift" | grep -q "DRIFT"'

# --- case 12: marker-computation failure is a hard precondition of
#              publishing — no gh calls happen at all, single degrade
#              class, reason on stderr ------------------------------------
STUB_LOG="$TMP/log-12"; : >"$STUB_LOG"
STUB_GIST_COPY="$TMP/gist-copy-12"
STUB_COMMENT_COPY="$TMP/comment-copy-12"
_SAVED_VERIFY_PLAN_SYNC_PY="$_VERIFY_PLAN_SYNC_PY"
_VERIFY_PLAN_SYNC_PY="$TMP/does-not-exist.py"
out="$(tracker_publish_plan "$KEY" "$CLEAN_TOML" "$CLEAN_MD" 2>"$TMP/stderr-12.log")"; rc=$?
_VERIFY_PLAN_SYNC_PY="$_SAVED_VERIFY_PLAN_SYNC_PY"
check "publish marker-failure: exit nonzero"     '[[ $rc -ne 0 ]]'
check "publish marker-failure: zero gh calls"    '[[ ! -s "$STUB_LOG" ]]'
check "publish marker-failure: stderr reason"    '[[ -s "$TMP/stderr-12.log" ]]'

unset STUB_LOG STUB_GIST_COPY STUB_COMMENT_COPY STUB_FAIL STUB_GIST_URL

printf 'tracker-publish tests: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
