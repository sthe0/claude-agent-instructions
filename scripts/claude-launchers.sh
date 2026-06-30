#!/usr/bin/env bash
# claude-launchers.sh — sourced launcher functions for Claude Code sessions.
#
# Source this file from ~/.bashrc (or equivalent). It defines:
#   claude-task      dispatch with the 'default' auth profile
#   claude-<P>       dispatch with machine-local profile P (generated per profile
#                    listed by _auth_list at source time; unconfigured machine
#                    exposes only claude-task)
#
# Core ships only the 'default' profile + the apply/list framework; no
# specialized auth. Any pre-existing machine-local raw claude() fallback (e.g. one
# that sources a proxy env file) lives in ~/.bashrc, not here, so this file stays
# org-neutral.
#
# Requires bash; uses BASH_SOURCE for self-location. These are sourced functions
# so that cd can persist in the caller's shell if callers extend the dispatch.
#
# Env seams for tests:
#   ENTER_TASK_BIN   override the enter-task.sh path (default: co-located script)
#   CLAUDE_AUTH_PROFILE_DIR  override the profile dir (consumed by auth-profiles.sh)
#   CLAUDE_LAUNCH_DRYRUN     set to any non-empty value to engage dry-run mode

# Self-locate: Core scripts/ dir (where enter-task.sh and project_entry/ live).
_LAUNCHERS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source the auth-profile framework.
# shellcheck source=project_entry/auth-profiles.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/auth-profiles.sh"

# enter-task binary; ENTER_TASK_BIN overrides for tests.
_LAUNCHERS_ENTER_TASK="${ENTER_TASK_BIN:-$_LAUNCHERS_SCRIPTS_DIR/enter-task.sh}"

# ── Core dispatch function ────────────────────────────────────────────────────
# _dispatch_with_profile <profile> [first-token] [remaining-claude-args...]
#
# Classifies the first token into an enter-task spec, resolves the project
# directory, then (unless CLAUDE_LAUNCH_DRYRUN is set) applies the auth profile
# and runs claude in that directory.
_dispatch_with_profile() {
  local _profile="$1"; shift
  local _tok="${1:-}"
  local -a _spec _cargs=()

  # Classify the first token into an enter-task spec flag.
  if [[ -z "$_tok" ]]; then
    _spec=(--reuse)
  elif [[ "$_tok" =~ ^[A-Z][A-Z0-9]+-[0-9]+$ ]]; then
    # Tracker key (e.g. DEEPAGENT-7)
    _spec=(--key "$_tok"); shift; _cargs=("$@")
  elif [[ "$_tok" == "--new" ]]; then
    local _title="${2:-}"
    [[ -n "$_title" ]] || { printf 'usage: claude-task --new <title>\n' >&2; return 1; }
    _spec=(--new "$_title"); shift 2; _cargs=("$@")
  elif [[ "$_tok" =~ ^[0-9]+$ ]]; then
    # Bare integer treated as a tracker issue number
    _spec=(--key "$_tok"); shift; _cargs=("$@")
  else
    _spec=(--name "$_tok"); shift; _cargs=("$@")
  fi

  # Resolve the project directory.  --dry-run is forwarded so enter-task skips
  # external side effects while still printing the would-be directory.
  local _dir
  _dir="$("$_LAUNCHERS_ENTER_TASK" "${_spec[@]}" ${CLAUDE_LAUNCH_DRYRUN:+--dry-run} 2>/dev/null | tail -1)"
  if [[ -z "$_dir" ]]; then
    printf 'claude-task: workspace entry failed. Run enter-task.sh directly for details.\n' >&2
    return 1
  fi

  # Dry-run: report the resolved dir and profile, then return without cd or launch.
  if [[ -n "${CLAUDE_LAUNCH_DRYRUN:-}" ]]; then
    printf 'enter=%s profile=%s\n' "$_dir" "$_profile"
    return 0
  fi

  # Apply auth profile and run claude inside the resolved directory.
  # bash -c receives the dir as $1 (shifted away before command claude "$@").
  _auth_apply "$_profile" -- \
    bash -c 'cd "$1" && shift && command claude "$@"' -- "$_dir" "${_cargs[@]}"
}

# ── claude-task (default auth profile) ───────────────────────────────────────
claude-task() { _dispatch_with_profile default "$@"; }

# ── claude-<P> (one per machine-local profile, induced at source time) ────────
# On an unconfigured machine (_auth_list returns only 'default'), this loop
# defines no extra commands and only claude-task is available.
for _lp in $(_auth_list); do
  [[ "$_lp" == "default" ]] && continue
  # shellcheck disable=SC2064
  eval "claude-${_lp}() { _dispatch_with_profile '${_lp}' \"\$@\"; }"
done
unset _lp
