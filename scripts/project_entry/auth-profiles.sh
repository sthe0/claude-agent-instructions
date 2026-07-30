#!/usr/bin/env bash
# Namespaced auth-profile framework — Core (org-neutral) portion only.
#
# Core ships: the 'default' profile (no-op) + namespaced list/apply helpers.
# Named profiles are machine-local:
#   ${<NAMESPACE>_AUTH_PROFILE_DIR:-$HOME/.config/<namespace>/auth-profiles.d}/<name>.sh
#
# Core commits NO concrete profile files (those are machine-local, stage 4).

# _auth_profile_dir <namespace>
# Resolve the machine-local profile directory for a supported agent namespace.
_auth_profile_dir() {
  local _namespace="$1"
  case "$_namespace" in
    claude)
      printf '%s\n' "${CLAUDE_AUTH_PROFILE_DIR:-$HOME/.config/claude/auth-profiles.d}"
      ;;
    cursor)
      printf '%s\n' "${CURSOR_AUTH_PROFILE_DIR:-$HOME/.config/cursor/auth-profiles.d}"
      ;;
    *)
      printf '%s\n' "$HOME/.config/$_namespace/auth-profiles.d"
      ;;
  esac
}

# _auth_list_ns <namespace>: print 'default' followed by the basename (sans .sh) of each
# machine-local profile file found in the profile directory.
# Called at source time by backend registration files to generate profile functions.
_auth_list_ns() {
  local _namespace="$1"
  local _profile_dir
  _profile_dir="$(_auth_profile_dir "$_namespace")"
  printf 'default\n'
  if [[ -d "$_profile_dir" ]]; then
    local _f
    for _f in "$_profile_dir"/*.sh; do
      [[ -f "$_f" ]] || continue   # no matches -> glob literal, skip
      printf '%s\n' "$(basename "$_f" .sh)"
    done
  fi
}

# _auth_apply_ns <namespace> <profile> -- <cmd...>
# Sources the named machine-local profile file (which exports the desired env
# vars) inside a subshell, then runs cmd in that env.  The subshell keeps the
# profile-set vars scoped to cmd and its children, not the calling shell.
# 'default' is a no-op: Core defines no additional env beyond the shell default.
_auth_apply_ns() {
  local _namespace="$1"; shift
  local _profile="$1"; shift
  [[ "${1:-}" == "--" ]] && shift

  if [[ "$_profile" == "default" ]]; then
    "$@"
    return
  fi

  local _profile_dir _profile_file
  _profile_dir="$(_auth_profile_dir "$_namespace")"
  _profile_file="${_profile_dir}/${_profile}.sh"
  if [[ ! -f "$_profile_file" ]]; then
    printf '_auth_apply: profile file not found: %s\n' "$_profile_file" >&2
    return 1
  fi
  # shellcheck source=/dev/null
  ( source "$_profile_file" && "$@" )
}

# Compatibility wrappers for existing Claude callers.
_auth_list() { _auth_list_ns claude "$@"; }
_auth_apply() { _auth_apply_ns claude "$@"; }
