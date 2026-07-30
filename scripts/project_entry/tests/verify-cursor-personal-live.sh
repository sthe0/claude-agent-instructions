#!/usr/bin/env bash
# Live proof that cursor-personal enters a real workspace and runs cursor-agent.
#
# Authored for stage-4 verify (machine-local wiring must already be in place).
# Not hermetic: needs a real cursor-agent on PATH, a sourced launcher, and a
# personal auth-profile file. Honours CLAUDE_SKIP_ONBOARD=1.
#
# Usage (from the canonical checkout, after landing):
#   CLAUDE_SKIP_ONBOARD=1 bash scripts/project_entry/tests/verify-cursor-personal-live.sh
set -euo pipefail

export CLAUDE_SKIP_ONBOARD="${CLAUDE_SKIP_ONBOARD:-1}"

CANON="${HOME}/claude-agent-instructions"
LAUNCHERS="${CANON}/scripts/cursor-launchers.sh"
BASHRC="${HOME}/.bashrc"
PERSONAL_PROFILE="${HOME}/.config/cursor/auth-profiles.d/personal.sh"
SMOKE_NAME="cursor-smoke"
PROMPT='Print the absolute path of your working directory and nothing else.'

die() { printf 'verify-cursor-personal-live: %s\n' "$*" >&2; exit 1; }

[[ -f "$LAUNCHERS" ]] || die "launcher missing: $LAUNCHERS"
# shellcheck source=/dev/null
source "$LAUNCHERS"

declare -f cursor-personal >/dev/null 2>&1 \
  || die "cursor-personal is not a defined function (is the personal profile present?)"

grep -qF 'cursor-launchers.sh' "$BASHRC" 2>/dev/null \
  || die "~/.bashrc does not source cursor-launchers.sh"

[[ -f "$PERSONAL_PROFILE" ]] \
  || die "missing cursor personal profile: $PERSONAL_PROFILE"

cd "$CANON"

dry_out="$(CLAUDE_LAUNCH_DRYRUN=1 cursor-personal "$SMOKE_NAME" 2>/dev/null)" \
  || die "dry-run failed"
printf '%s\n' "$dry_out" | grep -q 'profile=personal' \
  || die "dry-run missing profile=personal (got: $dry_out)"
enter_path="$(printf '%s\n' "$dry_out" | sed -n 's/^enter=\([^ ]*\).*/\1/p')"
[[ -n "$enter_path" ]] || die "dry-run missing non-empty enter= (got: $dry_out)"

cleanup() {
  if [[ -n "${enter_path:-}" && -e "$enter_path" ]]; then
    git -C "$CANON" worktree remove --force "$enter_path" 2>/dev/null \
      || rm -rf "$enter_path"
  fi
  git -C "$CANON" branch -D "$SMOKE_NAME" 2>/dev/null || true
}
trap cleanup EXIT

answer="$(
  cursor-personal "$SMOKE_NAME" \
    -p "$PROMPT" \
    --output-format text \
    --trust --force --approve-mcps \
    2>/dev/null
)" || die "live cursor-personal run failed"

printf '%s\n' "$answer" | grep -qF "$enter_path" \
  || die "live answer does not contain workspace path ${enter_path} (got: ${answer})"

printf 'verify-cursor-personal-live: OK (workspace %s)\n' "$enter_path"
