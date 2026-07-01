#!/usr/bin/env bash
# onboard — one-command machine initialization for the Claude agent system.
#
# Performs two universal steps (A and B), then delegates org/machine-specific
# initialization to discoverable hooks (C):
#
#   A) setup-symlinks.sh  — global symlinks + settings
#   B) doctor.sh          — readiness preflight (advisory; non-zero is warned, not fatal)
#   C) machine-local hooks in CLAUDE_ONBOARD_HOOK_DIR, called in sorted order
#
# Usage:
#     ~/claude-agent-instructions/scripts/onboard.sh [-h | --help]
#     onboard    (if sourced via claude-launchers.sh)
#
# Machine-local hooks are *.sh files in the hook directory (default:
# ~/.config/claude/onboard.d), called in sorted order with no arguments.
# A non-zero hook exit is surfaced to stderr and aborts onboard with that status.
# Later hooks do not run after a failure.
#
# Env seams:
#   SETUP_SYMLINKS_BIN       override setup-symlinks.sh path (for tests)
#   DOCTOR_BIN               override doctor.sh path (for tests)
#   CLAUDE_ONBOARD_HOOK_DIR  override hook directory (default: ~/.config/claude/onboard.d)
#   CLAUDE_DRY_RUN           set to any non-empty value: print plan, no side effects
set -uo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "$_SCRIPT_DIR/lib/config-root.sh"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  grep '^#' "${BASH_SOURCE[0]}" | sed 's/^#\{0,1\} \{0,1\}//'
  exit 0
fi

_setup_symlinks="${SETUP_SYMLINKS_BIN:-$_SCRIPT_DIR/setup-symlinks.sh}"
_doctor="${DOCTOR_BIN:-$_SCRIPT_DIR/doctor.sh}"
_hook_dir="${CLAUDE_ONBOARD_HOOK_DIR:-$HOME/.config/claude/onboard.d}"
_dry="${CLAUDE_DRY_RUN:-}"

# A: setup-symlinks
if [[ -n "$_dry" ]]; then
  printf 'onboard: would run setup-symlinks (%s)\n' "$_setup_symlinks" >&2
else
  "$_setup_symlinks"
fi

# B: doctor (advisory — non-zero is warned but does not abort)
if [[ -n "$_dry" ]]; then
  printf 'onboard: would run doctor (%s)\n' "$_doctor" >&2
else
  _rc=0
  "$_doctor" || _rc=$?
  if [[ $_rc -ne 0 ]]; then
    printf 'onboard: doctor reported issues (see above) — continuing\n' >&2
  fi
fi

# C: machine-local hooks
if [[ -d "$_hook_dir" ]]; then
  _hooks=()
  while IFS= read -r -d '' _h; do
    _hooks+=("$_h")
  done < <(find "$_hook_dir" -maxdepth 1 -name '*.sh' -print0 2>/dev/null | sort -z || true)

  if [[ ${#_hooks[@]} -gt 0 ]]; then
    for _h in "${_hooks[@]}"; do
      if [[ -n "$_dry" ]]; then
        printf 'onboard: would run hook %s\n' "$_h" >&2
      else
        _rc=0
        "$_h" || _rc=$?
        if [[ $_rc -ne 0 ]]; then
          printf 'onboard: hook %s failed\n' "$_h" >&2
          exit "$_rc"
        fi
      fi
    done
  fi
fi

printf 'onboard: done\n' >&2

# Closing guidance: the user↔core switch + how to start. Printed only on a clean
# run (doctor's own "Ready" block carries the full version). Kept short — the
# single fact a new user must not miss is that the system is a SEPARATE install
# from their personal claude, driven by claude-task / claude-agent.
if [[ -z "$_dry" ]]; then
  printf '\n' >&2
  printf 'How to start working on tasks:\n' >&2
  printf '  - Run the SYSTEM with "claude-task" (or "claude-agent" for scripted -p/-c).\n' >&2
  printf '    Bare "claude" stays your OWN personal ~/.claude, untouched — that is the\n' >&2
  printf '    user<->core switch: system lives in %s.\n' "$CLAUDE_AGENT_HOME" >&2
  printf '  - Then describe your task in plain language; a substantive task auto-plans\n' >&2
  printf '    and stops at the approval gate. Mention a ticket key to load its context.\n' >&2
  printf '  (If any [FAIL] appeared above, fix it first — usually re-run setup-symlinks.sh.)\n' >&2
fi
