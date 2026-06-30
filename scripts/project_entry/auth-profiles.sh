#!/usr/bin/env bash
# Auth-profile framework — Core (org-neutral) portion only.
#
# Core ships: the 'default' profile (no-op) + _auth_list + _auth_apply.
# Named profiles are machine-local:
#   ${CLAUDE_AUTH_PROFILE_DIR:-$HOME/.config/claude/auth-profiles.d}/<name>.sh
#
# Core commits NO concrete profile files (those are machine-local, stage 4).

_CLAUDE_AUTH_PROFILE_DIR="${CLAUDE_AUTH_PROFILE_DIR:-$HOME/.config/claude/auth-profiles.d}"

# _auth_list: print 'default' followed by the basename (sans .sh) of each
# machine-local profile file found in the profile directory.
# Called at source time by claude-launchers.sh to generate claude-<P> functions.
_auth_list() {
  printf 'default\n'
  if [[ -d "$_CLAUDE_AUTH_PROFILE_DIR" ]]; then
    local _f
    for _f in "$_CLAUDE_AUTH_PROFILE_DIR"/*.sh; do
      [[ -f "$_f" ]] || continue   # no matches -> glob literal, skip
      printf '%s\n' "$(basename "$_f" .sh)"
    done
  fi
}

# _auth_apply <profile> -- <cmd...>
# Sources the named machine-local profile file (which exports the desired env
# vars) inside a subshell, then runs cmd in that env.  The subshell keeps the
# profile-set vars scoped to cmd and its children, not the calling shell.
# 'default' is a no-op: Core defines no additional env beyond the shell default.
_auth_apply() {
  local _profile="$1"; shift
  [[ "${1:-}" == "--" ]] && shift

  if [[ "$_profile" == "default" ]]; then
    "$@"
    return
  fi

  local _pf="${_CLAUDE_AUTH_PROFILE_DIR}/${_profile}.sh"
  if [[ ! -f "$_pf" ]]; then
    printf '_auth_apply: profile file not found: %s\n' "$_pf" >&2
    return 1
  fi
  # shellcheck source=/dev/null
  ( source "$_pf" && "$@" )
}
