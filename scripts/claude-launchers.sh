#!/usr/bin/env bash
# claude-launchers.sh — sourced launcher functions for Claude Code sessions.
#
# Source this file from ~/.bashrc or ~/.zshrc (or equivalent). It defines:
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
# Works in bash and zsh: self-locates via BASH_SOURCE (bash) or $0 (zsh). Sourced functions
# so that cd can persist in the caller's shell if callers extend the dispatch.
#
# Env seams for tests:
#   ENTER_TASK_BIN          override the enter-task.sh path (default: co-located script)
#   CLAUDE_AUTH_PROFILE_DIR override the profile dir (consumed by auth-profiles.sh)
#   CLAUDE_LAUNCH_DRYRUN    set to any non-empty value to engage dry-run mode
#   CLAUDE_ONBOARD_HOOK_DIR override the onboard hook dir (default: ~/.config/claude/onboard.d)
#   CLAUDE_SKIP_ONBOARD     set to any non-empty value to skip the init probe
#   CLAUDE_ONBOARD_BIN      override the onboard.sh path (default: co-located onboard.sh)

# Self-locate: Core scripts/ dir (where enter-task.sh and project_entry/ live).
_LAUNCHERS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# Source the auth-profile framework.
# shellcheck source=project_entry/auth-profiles.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/auth-profiles.sh"

# Source the project registry seam for _launcher_usage + dispatch routing.
# shellcheck source=project_entry/projects.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/projects.sh"

# enter-task binary; ENTER_TASK_BIN overrides for tests.
_LAUNCHERS_ENTER_TASK="${ENTER_TASK_BIN:-$_LAUNCHERS_SCRIPTS_DIR/enter-task.sh}"

# ── Self-init probe ────────────────────────────────────────────────────────────
# Probes machine-local onboard.d hooks for --needs-init and runs onboard when
# any hook signals that initialization is required.  Core-neutral: only the
# hook directory + contract are named; the org-specific details live in the hook.
_maybe_onboard() {
  [[ -n "${CLAUDE_SKIP_ONBOARD:-}" ]] && return 0
  local _hook_dir="${CLAUDE_ONBOARD_HOOK_DIR:-$HOME/.config/claude/onboard.d}"
  local _need=0
  if [[ -d "$_hook_dir" ]]; then
    local _h
    for _h in "$_hook_dir"/*.sh; do
      [[ -f "$_h" ]] || continue
      if "${_h}" --needs-init 2>/dev/null; then
        _need=1
        break
      fi
    done
  fi
  if [[ $_need -eq 1 ]]; then
    printf 'environment not initialized — running onboard (this may mount storage and compose project configs)…\n' >&2
    "${CLAUDE_ONBOARD_BIN:-$_LAUNCHERS_SCRIPTS_DIR/onboard.sh}" || \
      printf 'onboard: warning: initialization failed (continuing)\n' >&2
  fi
}

# ── onboard (user-callable wrapper) ───────────────────────────────────────────
onboard() { command "${CLAUDE_ONBOARD_BIN:-$_LAUNCHERS_SCRIPTS_DIR/onboard.sh}" "$@"; }

# ── Usage ─────────────────────────────────────────────────────────────────────
# _launcher_usage <cmd> -> print this launcher's help to stdout.
_launcher_usage() {
  local _cmd="$1"
  cat <<USAGE
Usage: $_cmd [<name> | <TICKET-123> | --new "<title>"] [claude args...]

  $_cmd                    no task -> run plain 'claude' here (current dir)
                           under this command's auth profile, in normal mode
  $_cmd <name>             named scratch workspace, then launch claude in it
  $_cmd <TICKET-123>       resolve a tracker ticket -> isolated workspace
  $_cmd --new "<title>"    create a tracker issue (confirms first), then enter
  $_cmd --list-projects    list registered projects and their tracker queues
  $_cmd -h | --help        show this help

--new performs an irreversible tracker write: interactively it asks to confirm;
non-interactively it requires CLAUDE_LAUNCH_ASSUME_YES=1.

With no task (or a bare 'claude' flag such as -c / -p) the command does NOT
create a workspace; it launches plain 'claude' in the current directory under
the auth profile. Pass a name or ticket above to start work in an isolated copy.
USAGE
  # Dynamic projects line — degrade silently if registry unavailable or empty.
  local _proj_line
  _proj_line="$(project_list 2>/dev/null | awk 'NR>1{printf "%s%s",sep,$1;sep=", "}')" || true
  [[ -n "$_proj_line" ]] && printf 'Projects: %s (see --list-projects)\n' "$_proj_line"
}

# ── Core dispatch function ────────────────────────────────────────────────────
# _dispatch_with_profile <profile> [first-token] [remaining-claude-args...]
#
# Routes the first token: -h/--help prints usage; no task (or a bare claude flag)
# launches plain claude in the current dir under the auth profile (with a hint on
# how to start a real task); a ticket / --new / plain name enters an isolated
# workspace via enter-task and launches claude there. The auth profile is applied
# on BOTH paths.
_dispatch_with_profile() {
  local _profile="$1"; shift
  local _tok="${1:-}"
  local _cmd; [[ "$_profile" == "default" ]] && _cmd="claude-task" || _cmd="claude-$_profile"
  local -a _spec _cargs=()

  _maybe_onboard

  # -h/--help: print usage and stop. No workspace entry, no claude launch.
  if [[ "$_tok" == "-h" || "$_tok" == "--help" ]]; then
    _launcher_usage "$_cmd"
    return 0
  fi

  # --list-projects / --register: forward directly to enter-task and return.
  if [[ "$_tok" == "--list-projects" || "$_tok" == "--register" ]]; then
    "$_LAUNCHERS_ENTER_TASK" "$@"
    return $?
  fi

  # No task specified (bare invocation), or a bare claude flag (e.g. -c / -p, but
  # NOT our --new selector): do NOT enter a workspace. Launch plain claude in the
  # current directory under the auth profile, after a one-time how-to warning.
  if [[ -z "$_tok" || ( "$_tok" == -* && "$_tok" != "--new" ) ]]; then
    [[ -z "$_tok" ]] || _cargs=("$@")   # forward flags to claude; bare -> none
    if [[ -n "${CLAUDE_LAUNCH_DRYRUN:-}" ]]; then
      printf 'inplace profile=%s dir=%s\n' "$_profile" "$PWD"
      return 0
    fi
    printf "%s: no task specified — starting plain 'claude' in normal mode here (%s).\n" "$_cmd" "$PWD" >&2
    printf '  To start work on a task in an isolated workspace, run:\n' >&2
    printf '     %s <NAME>           # named scratch workspace\n' "$_cmd" >&2
    printf '     %s <TICKET-123>     # a tracker ticket\n' "$_cmd" >&2
    printf '     %s --new "title"    # create a ticket, then enter\n' "$_cmd" >&2
    # ${_cargs[@]+...}: bash 3.2 (macOS) errors on "${empty[@]}" under set -u.
    _auth_apply "$_profile" -- command claude ${_cargs[@]+"${_cargs[@]}"}
    return
  fi

  # Classify the first token into an enter-task spec flag (workspace entry).
  if [[ "$_tok" =~ ^[A-Z][A-Z0-9]+-[0-9]+$ ]]; then
    # Tracker key (e.g. DEEPAGENT-7)
    _spec=(--key "$_tok"); shift; _cargs=("$@")
  elif [[ "$_tok" == "--new" ]]; then
    local _title="${2:-}"
    [[ -n "$_title" ]] || { printf 'usage: %s --new <title>\n' "$_cmd" >&2; return 1; }
    _spec=(--new "$_title"); shift 2; _cargs=("$@")
  elif [[ "$_tok" =~ ^[0-9]+$ ]]; then
    # Bare integer treated as a tracker issue number
    _spec=(--key "$_tok"); shift; _cargs=("$@")
  else
    _spec=(--name "$_tok"); shift; _cargs=("$@")
  fi

  # --new is an irreversible tracker write. enter-task guards it behind
  # CLAUDE_LAUNCH_ASSUME_YES=1; confirm interactively (or honor a pre-set gate /
  # non-interactive abort) and forward the gate so the create can proceed.
  local _assume_yes="${CLAUDE_LAUNCH_ASSUME_YES:-}"
  if [[ "$_tok" == "--new" && -z "${CLAUDE_LAUNCH_DRYRUN:-}" && "$_assume_yes" != "1" ]]; then
    if [[ -t 0 ]]; then
      printf '%s --new will CREATE a tracker task. Proceed? [y/N] ' "$_cmd" >&2
      local _ans; read -r _ans
      case "$_ans" in
        [yY]|[yY][eE][sS]) _assume_yes=1 ;;
        *) printf '%s: aborted (no task created).\n' "$_cmd" >&2; return 1 ;;
      esac
    else
      printf '%s: --new creates a tracker task; set CLAUDE_LAUNCH_ASSUME_YES=1 to confirm (non-interactive).\n' "$_cmd" >&2
      return 1
    fi
  fi

  # Resolve the project directory.  --dry-run is forwarded so enter-task skips
  # external side effects while still printing the would-be directory. Capture
  # enter-task's stderr so a failure surfaces ITS explanation instead of a
  # generic message (the hint used to be swallowed by 2>/dev/null).
  local _dir _errfile
  _errfile="$(mktemp)"
  _dir="$(CLAUDE_LAUNCH_ASSUME_YES="$_assume_yes" "$_LAUNCHERS_ENTER_TASK" "${_spec[@]}" ${CLAUDE_LAUNCH_DRYRUN:+--dry-run} 2>"$_errfile" | tail -1)"
  if [[ -z "$_dir" ]]; then
    printf '%s: workspace entry failed:\n' "$_cmd" >&2
    sed 's/^/  /' "$_errfile" >&2
    rm -f "$_errfile"
    return 1
  fi
  rm -f "$_errfile"

  # Dry-run: report the resolved dir and profile, then return without cd or launch.
  if [[ -n "${CLAUDE_LAUNCH_DRYRUN:-}" ]]; then
    printf 'enter=%s profile=%s\n' "$_dir" "$_profile"
    return 0
  fi

  # Apply auth profile and run claude inside the resolved directory.
  # bash -c receives the dir as $1 (shifted away before command claude "$@").
  _auth_apply "$_profile" -- \
    bash -c 'cd "$1" && shift && command claude "$@"' -- "$_dir" ${_cargs[@]+"${_cargs[@]}"}
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
