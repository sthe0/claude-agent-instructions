#!/usr/bin/env bash
# Hermetic test for land-on-main.sh.
#
# Builds a local bare "origin" repo + a local clone with a feature branch
# carrying unrelated WIP (a committed file + an unstaged edit) plus one
# STAGED change, then drives land-on-main.sh against them. No real network:
# "origin" is a bare repo on local disk; git push talks to it over a file path.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$HERE/.." && pwd)"
LAND_BIN="$SCRIPTS_DIR/land-on-main.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
ok()   { ((PASS++)); printf '  OK  %s\n' "$1"; }
fail() { ((FAIL++)); printf ' FAIL %s\n' "$1"; }

# Commit identity for every git invocation in this test AND in the
# land-on-main.sh child process it launches (env vars are inherited).
export GIT_AUTHOR_NAME="Test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="Test" GIT_COMMITTER_EMAIL="test@example.com"

# ── Fixture builder ──────────────────────────────────────────────────────────
# make_fixture <name> -> prints "<origin-bare-dir> <local-clone-dir>"
# Local clone: feature branch off main with a committed WIP file, an unstaged
# WIP edit to wip-unstaged.txt, and a STAGED edit to README.md.
make_fixture() {
  local name="$1"
  local origin="$TMP/$name-origin.git"
  local local_repo="$TMP/$name-local"

  git init --quiet --bare -b main "$origin" >/dev/null
  local seed="$TMP/$name-seed"
  git clone --quiet "$origin" "$seed" >/dev/null
  printf 'original readme\n' >"$seed/README.md"
  printf 'untouched\n' >"$seed/wip-unstaged.txt"
  git -C "$seed" add README.md wip-unstaged.txt
  git -C "$seed" commit --quiet -m "seed: initial content"
  git -C "$seed" push --quiet origin main >/dev/null

  git clone --quiet "$origin" "$local_repo" >/dev/null
  git -C "$local_repo" checkout --quiet -b "feature/$name"
  printf 'committed wip\n' >"$local_repo/wip-committed.txt"
  git -C "$local_repo" add wip-committed.txt
  git -C "$local_repo" commit --quiet -m "wip: unrelated committed work"
  printf 'unstaged dirty edit\n' >>"$local_repo/wip-unstaged.txt"
  printf 'updated readme via staged change\n' >"$local_repo/README.md"
  git -C "$local_repo" add README.md

  printf '%s %s\n' "$origin" "$local_repo"
}

snapshot() {
  local repo="$1"
  printf 'branch=%s head=%s status=%s\n' \
    "$(git -C "$repo" rev-parse --abbrev-ref HEAD)" \
    "$(git -C "$repo" rev-parse HEAD)" \
    "$(git -C "$repo" status --porcelain | sort | tr '\n' '|')"
}

# ═══════════════════════════════════════════════════════════════════════════
# Test 1 — feature-branch-with-WIP: lands the staged change, leaves the rest
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- feature-branch-with-WIP ---\n'
read -r ORIGIN LOCAL <<<"$(make_fixture t1)"
ORIGIN_BEFORE="$(git -C "$ORIGIN" rev-parse main)"
LOCAL_BEFORE="$(snapshot "$LOCAL")"

out="$(cd "$LOCAL" && LAND_ON_MAIN_REMOTE=origin LAND_ON_MAIN_BRANCH=main "$LAND_BIN" -m "land: readme update" 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "exits 0"
else
  fail "expected exit 0, got $rc (output: $out)"
fi

ORIGIN_AFTER="$(git -C "$ORIGIN" rev-parse main)"
if [[ "$ORIGIN_AFTER" != "$ORIGIN_BEFORE" ]]; then
  ok "origin/main advanced"
else
  fail "origin/main did not advance"
fi

if git -C "$ORIGIN" log -1 --format=%s main | grep -qF "land: readme update"; then
  ok "landed commit carries the given message"
else
  fail "landed commit message wrong (got: $(git -C "$ORIGIN" log -1 --format=%s main))"
fi

LANDED_README="$(git -C "$ORIGIN" show main:README.md)"
if [[ "$LANDED_README" == "updated readme via staged change" ]]; then
  ok "landed commit contains the staged README content"
else
  fail "landed commit README content wrong (got: $LANDED_README)"
fi

if git -C "$ORIGIN" ls-tree -r --name-only main | grep -qx 'wip-committed.txt'; then
  fail "unrelated WIP file leaked into origin/main"
else
  ok "unrelated WIP file did NOT leak into origin/main"
fi

LOCAL_AFTER="$(snapshot "$LOCAL")"
if [[ "$LOCAL_AFTER" == "$LOCAL_BEFORE" ]]; then
  ok "caller branch/HEAD/status left exactly as-found"
else
  fail "caller repo state changed (before: $LOCAL_BEFORE | after: $LOCAL_AFTER)"
fi

WT_COUNT="$(git -C "$LOCAL" worktree list | wc -l)"
if [[ "$WT_COUNT" -eq 1 ]]; then
  ok "isolated worktree cleaned up (only main worktree remains)"
else
  fail "leftover worktree(s): $(git -C "$LOCAL" worktree list)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 2 — dry-run: no push, no mutation anywhere
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- dry-run ---\n'
read -r ORIGIN LOCAL <<<"$(make_fixture t2)"
ORIGIN_BEFORE="$(git -C "$ORIGIN" rev-parse main)"
LOCAL_BEFORE="$(snapshot "$LOCAL")"

out="$(cd "$LOCAL" && LAND_ON_MAIN_REMOTE=origin LAND_ON_MAIN_BRANCH=main "$LAND_BIN" -m "dry run msg" --dry-run 2>&1)"
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ok "--dry-run exits 0"
else
  fail "--dry-run expected exit 0, got $rc (output: $out)"
fi
if printf '%s' "$out" | grep -q 'DRY-RUN'; then
  ok "--dry-run output announces DRY-RUN"
else
  fail "--dry-run output missing DRY-RUN marker (got: $out)"
fi
if printf '%s' "$out" | grep -qF 'dry run msg'; then
  ok "--dry-run output includes the given message"
else
  fail "--dry-run output missing the message (got: $out)"
fi

ORIGIN_AFTER="$(git -C "$ORIGIN" rev-parse main)"
if [[ "$ORIGIN_AFTER" == "$ORIGIN_BEFORE" ]]; then
  ok "--dry-run: origin/main unchanged"
else
  fail "--dry-run: origin/main advanced unexpectedly"
fi

LOCAL_AFTER="$(snapshot "$LOCAL")"
if [[ "$LOCAL_AFTER" == "$LOCAL_BEFORE" ]]; then
  ok "--dry-run: caller repo state unchanged"
else
  fail "--dry-run: caller repo state changed (before: $LOCAL_BEFORE | after: $LOCAL_AFTER)"
fi

WT_COUNT="$(git -C "$LOCAL" worktree list | wc -l)"
if [[ "$WT_COUNT" -eq 1 ]]; then
  ok "--dry-run: no worktree was created"
else
  fail "--dry-run: unexpected worktree(s): $(git -C "$LOCAL" worktree list)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 3 — missing -m: refuses with usage, no push
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- missing -m ---\n'
read -r ORIGIN LOCAL <<<"$(make_fixture t3)"
ORIGIN_BEFORE="$(git -C "$ORIGIN" rev-parse main)"

out="$(cd "$LOCAL" && LAND_ON_MAIN_REMOTE=origin LAND_ON_MAIN_BRANCH=main "$LAND_BIN" 2>&1)"
rc=$?

if [[ "$rc" -eq 2 ]]; then
  ok "missing -m: exits 2"
else
  fail "missing -m: expected exit 2, got $rc (output: $out)"
fi
if printf '%s' "$out" | grep -qi 'message is required'; then
  ok "missing -m: error names the missing -m/--message"
else
  fail "missing -m: error message unclear (got: $out)"
fi

ORIGIN_AFTER="$(git -C "$ORIGIN" rev-parse main)"
if [[ "$ORIGIN_AFTER" == "$ORIGIN_BEFORE" ]]; then
  ok "missing -m: origin/main unchanged"
else
  fail "missing -m: origin/main advanced unexpectedly"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Test 4 — empty-stage: refuses when nothing is staged
# ═══════════════════════════════════════════════════════════════════════════
printf '\n--- empty-stage ---\n'
read -r ORIGIN LOCAL <<<"$(make_fixture t4)"
git -C "$LOCAL" reset --quiet   # unstage the fixture's staged README change
ORIGIN_BEFORE="$(git -C "$ORIGIN" rev-parse main)"

out="$(cd "$LOCAL" && LAND_ON_MAIN_REMOTE=origin LAND_ON_MAIN_BRANCH=main "$LAND_BIN" -m "should not land" 2>&1)"
rc=$?

if [[ "$rc" -eq 2 ]]; then
  ok "empty-stage: exits 2"
else
  fail "empty-stage: expected exit 2, got $rc (output: $out)"
fi
if printf '%s' "$out" | grep -qi 'nothing staged'; then
  ok "empty-stage: error names the empty stage"
else
  fail "empty-stage: error message unclear (got: $out)"
fi

ORIGIN_AFTER="$(git -C "$ORIGIN" rev-parse main)"
if [[ "$ORIGIN_AFTER" == "$ORIGIN_BEFORE" ]]; then
  ok "empty-stage: origin/main unchanged"
else
  fail "empty-stage: origin/main advanced unexpectedly"
fi

# ═══════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
