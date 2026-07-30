#!/usr/bin/env bash
# agent-dispatch.sh — backend-agnostic task-entry dispatch for agent CLIs.
#
# Source this from a backend registration file (e.g. claude-launchers.sh). The
# registration defines the five descriptor functions below for its backend id;
# this library owns onboard, usage, and the shared workspace-entry / launch path.
#
# Descriptor contract — each backend registration defines five functions:
#   _backend_<id>_prefix      -> command-name prefix (claude / cursor)
#   _backend_<id>_bin         -> executable (claude / cursor-agent)
#   _backend_<id>_auth_ns     -> auth-profile namespace for _auth_apply_ns
#   _backend_<id>_config_env  -> zero or more VAR=VALUE for the `env` prefix
#   _backend_<id>_plain_cmd   -> optional plain-launch twin for usage "See also:"
# Lookups below resolve those per-backend names so multiple registrations can
# coexist in one shell (claude-launchers.sh + cursor-launchers.sh).
#
# Env seams for tests (unchanged from the former claude-launchers.sh surface):
#   ENTER_TASK_BIN          override the enter-task.sh path
#   OPENING_BIN             override the opening.py path
#   CLAUDE_AUTH_PROFILE_DIR / CURSOR_AUTH_PROFILE_DIR  (via auth-profiles.sh)
#   CLAUDE_LAUNCH_DRYRUN    set to any non-empty value to engage dry-run mode
#   CLAUDE_ONBOARD_HOOK_DIR override the onboard hook dir
#   CLAUDE_SKIP_ONBOARD     set to any non-empty value to skip the init probe
#   CLAUDE_ONBOARD_BIN      override the onboard.sh path
#   CLAUDE_OPENING          off|on — force-suppress or force-enable opening dialogue

# Self-locate: Core scripts/ dir (where enter-task.sh and project_entry/ live).
# Prefer a registration-set path so a thin registrar can pin the scripts root
# before sourcing this file; otherwise resolve from this file's own location.
if [[ -z "${_LAUNCHERS_SCRIPTS_DIR:-}" ]]; then
  _LAUNCHERS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
fi

# Resolve the system config root (CLAUDE_AGENT_HOME, default ~/.claude-agent).
# shellcheck source=../lib/config-root.sh
source "$_LAUNCHERS_SCRIPTS_DIR/lib/config-root.sh"

# Source the auth-profile framework.
# shellcheck source=auth-profiles.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/auth-profiles.sh"

# Source the project registry seam for _launcher_usage + dispatch routing.
# shellcheck source=projects.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/projects.sh"

# enter-task binary; ENTER_TASK_BIN overrides for tests.
_LAUNCHERS_ENTER_TASK="${ENTER_TASK_BIN:-$_LAUNCHERS_SCRIPTS_DIR/enter-task.sh}"

# opening-dialogue binary; OPENING_BIN overrides for tests.
OPENING_BIN="${OPENING_BIN:-$_LAUNCHERS_SCRIPTS_DIR/project_entry/opening.py}"

# ── Backend descriptor lookups ────────────────────────────────────────────────
_backend_lookup() {
  local _backend="$1" _axis="$2"
  local _impl="_backend_${_backend}_${_axis}"
  if ! declare -f "$_impl" >/dev/null 2>&1; then
    printf 'agent-dispatch: backend %s has no %s descriptor\n' "$_backend" "$_axis" >&2
    return 1
  fi
  "$_impl"
}

_backend_prefix()     { _backend_lookup "$1" prefix; }
_backend_bin()        { _backend_lookup "$1" bin; }
_backend_auth_ns()    { _backend_lookup "$1" auth_ns; }
_backend_config_env() { _backend_lookup "$1" config_env; }
_backend_plain_cmd()  { _backend_lookup "$1" plain_cmd; }

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
# _launcher_usage <backend> <cmd> -> print this launcher's help to stdout.
# Config-root and See-also lines are independent descriptor axes: empty
# _backend_config_env omits "System config root:"; empty _backend_plain_cmd
# omits "See also:" — neither is inferred from the other.
_launcher_usage() {
  local _backend="$1"
  local _cmd="$2"
  local _prefix _config_env _plain_cmd
  _prefix="$(_backend_prefix "$_backend")"
  _config_env="$(_backend_config_env "$_backend")"
  _plain_cmd="$(_backend_plain_cmd "$_backend")"
  cat <<USAGE
Usage: $_cmd [<name> | <TICKET-123> | --new "<title>"] [${_prefix} args...]

  $_cmd                    no task -> run plain '${_prefix}' here (current dir)
                           under this command's auth profile, in normal mode
  $_cmd <name>             named scratch workspace, then launch ${_prefix} in it
  $_cmd <TICKET-123>       resolve a tracker ticket -> isolated workspace
  $_cmd --new "<title>"    create a tracker issue (confirms first), then enter
  $_cmd --project <key> ...   resolve project explicitly (else auto-detected from cwd)
  $_cmd --init <name>      create a NEW local git project, register it, then enter
  $_cmd --list-projects    list registered projects and their tracker queues
  $_cmd -h | --help        show this help

--new performs an irreversible tracker write: interactively it asks to confirm;
non-interactively it requires CLAUDE_LAUNCH_ASSUME_YES=1.

With no task (or a bare '${_prefix}' flag such as -c / -p) the command does NOT
create a workspace; it launches plain '${_prefix}' in the current directory under
the auth profile. Pass a name or ticket above to start work in an isolated copy.
USAGE
  if [[ -n "$_config_env" ]]; then
    printf 'System config root: %s\n' "${CLAUDE_AGENT_HOME}"
  fi
  if [[ -n "$_plain_cmd" ]]; then
    printf 'See also: %s — plain launch on the system root (for scripted -p/-c use;\n' "$_plain_cmd"
    printf '          no workspace management). First use: %s /login\n' "$_plain_cmd"
  fi
  # Dynamic projects line — degrade silently if registry unavailable or empty.
  local _proj_line
  _proj_line="$(project_list 2>/dev/null | awk 'NR>1{printf "%s%s",sep,$1;sep=", "}')" || true
  [[ -n "$_proj_line" ]] && printf 'Projects: %s (see --list-projects)\n' "$_proj_line"
}

# _backend_config_value <backend>
# Extract the value of the first VAR=VALUE assignment from _backend_config_env,
# for dry-run `config=` reporting. Empty config env -> empty value (field kept).
_backend_config_value() {
  local _backend="$1"
  local _config_env _assignment
  _config_env="$(_backend_config_env "$_backend")"
  [[ -n "$_config_env" ]] || return 0
  for _assignment in $_config_env; do
    case "$_assignment" in
      *=*) printf '%s\n' "${_assignment#*=}"; return 0 ;;
    esac
  done
}

# ── Core dispatch function ────────────────────────────────────────────────────
# _dispatch_agent <backend> <profile> [first-token] [remaining-agent-args...]
#
# Routes the first token: -h/--help prints usage; no task (or a bare agent flag)
# launches the backend binary in the current dir under the auth profile (with a
# hint on how to start a real task); a ticket / --new / plain name enters an
# isolated workspace via enter-task and launches the backend there. The auth
# profile is applied on BOTH paths.
_dispatch_agent() {
  local _backend="$1"; shift
  local _profile="$1"; shift
  local _tok="${1:-}"
  local _prefix _bin _auth_ns _config_env _config_value _cmd
  _prefix="$(_backend_prefix "$_backend")"
  _bin="$(_backend_bin "$_backend")"
  _auth_ns="$(_backend_auth_ns "$_backend")"
  _config_env="$(_backend_config_env "$_backend")"
  _config_value="$(_backend_config_value "$_backend")"
  [[ "$_profile" == "default" ]] && _cmd="${_prefix}-task" || _cmd="${_prefix}-${_profile}"
  local -a _spec _cargs=() _pass=()
  local _opening_flag=""

  _maybe_onboard

  # -h/--help: print usage and stop. No workspace entry, no agent launch.
  if [[ "$_tok" == "-h" || "$_tok" == "--help" ]]; then
    _launcher_usage "$_backend" "$_cmd"
    return 0
  fi

  # --list-projects / --register: forward directly to enter-task and return.
  if [[ "$_tok" == "--list-projects" || "$_tok" == "--register" ]]; then
    "$_LAUNCHERS_ENTER_TASK" "$@"
    return $?
  fi

  # Extract a leading run of enter-task modifier flags (--project/--workspace/
  # --tracker) BEFORE classifying the task token, so e.g.
  # `<prefix>-personal --project myorg/myproject --new "Title"` forwards --project
  # instead of the classifier swallowing it as part of --new's positional args.
  while [[ "${1:-}" == "--project" || "${1:-}" == "--workspace" || "${1:-}" == "--tracker" || \
           "${1:-}" == "--no-opening" || "${1:-}" == "--opening" ]]; do
    case "$1" in
      --project|--workspace|--tracker)
        local _mflag="$1"
        if [[ -z "${2:-}" ]]; then
          printf '%s: %s needs a value\n' "$_cmd" "$_mflag" >&2
          return 1
        fi
        _pass+=("$_mflag" "$2")
        shift 2
        ;;
      --no-opening) _opening_flag="off"; shift ;;
      --opening)    _opening_flag="on";  shift ;;
    esac
  done
  _tok="${1:-}"

  # No task specified (bare invocation), or a bare agent flag (e.g. -c / -p, but
  # NOT our --new selector): do NOT enter a workspace. Launch the backend binary
  # in the current directory under the auth profile, after a one-time how-to warning.
  if [[ -z "$_tok" || ( "$_tok" == -* && "$_tok" != "--new" && "$_tok" != "--init" ) ]]; then
    [[ -z "$_tok" ]] || _cargs=("$@")   # forward flags to the agent; bare -> none
    if [[ -n "${CLAUDE_LAUNCH_DRYRUN:-}" ]]; then
      printf 'inplace profile=%s dir=%s config=%s\n' "$_profile" "$PWD" "$_config_value"
      return 0
    fi
    printf "%s: no task specified — starting plain '%s' in normal mode here (%s).\n" \
      "$_cmd" "$_prefix" "$PWD" >&2
    printf '  To start work on a task in an isolated workspace, run:\n' >&2
    printf '     %s <NAME>           # named scratch workspace\n' "$_cmd" >&2
    printf '     %s <TICKET-123>     # a tracker ticket\n' "$_cmd" >&2
    printf '     %s --new "title"    # create a ticket, then enter\n' "$_cmd" >&2
    # ${_cargs[@]+...}: bash 3.2 (macOS) errors on "${empty[@]}" under set -u.
    # Run via `env` (not a VAR=val prefix on the _auth_apply_ns *function* call)
    # for portability: in zsh a prefix assignment on a function call is a
    # function-local and is NOT exported to the function's children; in bash it
    # PERSISTS into the caller's interactive shell — both wrong. env scopes
    # config-dir assignments to this exec only, the same in bash 3.2 and zsh,
    # and its exec of the backend binary bypasses a user function/alias exactly
    # as `command` did.
    local -a _env_prefix=(env)
    [[ -n "$_config_env" ]] && _env_prefix+=("$_config_env")
    _auth_apply_ns "$_auth_ns" "$_profile" -- \
      "${_env_prefix[@]}" "$_bin" ${_cargs[@]+"${_cargs[@]}"}
    return
  fi

  # Classify the first token into an enter-task spec flag (workspace entry).
  if [[ "$_tok" =~ ^[A-Z][A-Z0-9]+-[0-9]+$ ]]; then
    # Tracker key (e.g. PROJ-7)
    _spec=(--key "$_tok"); shift; _cargs=("$@")
  elif [[ "$_tok" == "--new" ]]; then
    local _title="${2:-}"
    [[ -n "$_title" ]] || { printf 'usage: %s --new <title>\n' "$_cmd" >&2; return 1; }
    _spec=(--new "$_title"); shift 2; _cargs=("$@")
  elif [[ "$_tok" == "--init" ]]; then
    local _initname="${2:-}"
    [[ -n "$_initname" ]] || { printf 'usage: %s --init <name-or-path>\n' "$_cmd" >&2; return 1; }
    _spec=(--init "$_initname"); shift 2; _cargs=("$@")
  elif [[ "$_tok" =~ ^[0-9]+$ ]]; then
    # Bare integer treated as a tracker issue number
    _spec=(--key "$_tok"); shift; _cargs=("$@")
  else
    _spec=(--name "$_tok"); shift; _cargs=("$@")
  fi

  # --no-opening/--opening only make sense BEFORE the task token (the
  # pre-token loop above); one that lands after it is a user mistake, not an
  # agent flag to forward — reject with a usage hint instead of silently
  # passing it through to the agent.
  local _oc
  for _oc in ${_cargs[@]+"${_cargs[@]}"}; do
    case "$_oc" in
      --opening|--no-opening)
        printf '%s: %s must precede the task (e.g. "%s %s <task>"), not follow it\n' \
          "$_cmd" "$_oc" "$_cmd" "$_oc" >&2
        return 1
        ;;
    esac
  done

  # Resolve whether the opening dialogue fires. Default ON for --key/--new,
  # OFF for --name/--init (a nameless scratch workspace has no task to read).
  # CLAUDE_OPENING=off, then --no-opening, suppress it; --opening forces it
  # on, taking final precedence over both.
  local _opening_on=""
  case "${_spec[0]:-}" in
    --key|--new) _opening_on=1 ;;
  esac
  [[ "${CLAUDE_OPENING:-}" == "off" ]] && _opening_on=""
  [[ "$_opening_flag" == "off" ]] && _opening_on=""
  [[ "$_opening_flag" == "on" ]] && _opening_on=1

  # --new is an irreversible tracker write. enter-task guards it behind
  # CLAUDE_LAUNCH_ASSUME_YES=1; confirm interactively (or honor a pre-set gate /
  # non-interactive abort) and forward the gate so the create can proceed.
  local _assume_yes="${CLAUDE_LAUNCH_ASSUME_YES:-}"
  if [[ "$_tok" == "--new" && -z "${CLAUDE_LAUNCH_DRYRUN:-}" && "$_assume_yes" != "1" ]]; then
    if [[ -t 0 ]]; then
      # Fail fast: validate the workspace context BEFORE asking the user to
      # confirm the irreversible tracker create — enter-task's empty-context
      # guard fires under --dry-run too, so a doomed entry (e.g. no project
      # resolvable from cwd) aborts here with its explanation instead of
      # after a wasted "y".
      local _pf_err
      _pf_err="$(mktemp)"
      if ! "$_LAUNCHERS_ENTER_TASK" ${_pass[@]+"${_pass[@]}"} "${_spec[@]}" --dry-run >/dev/null 2>"$_pf_err"; then
        printf '%s: workspace entry failed:\n' "$_cmd" >&2
        sed 's/^/  /' "$_pf_err" >&2
        rm -f "$_pf_err"
        return 1
      fi
      rm -f "$_pf_err"
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
  _dir="$(CLAUDE_LAUNCH_ASSUME_YES="$_assume_yes" "$_LAUNCHERS_ENTER_TASK" ${_pass[@]+"${_pass[@]}"} "${_spec[@]}" ${CLAUDE_LAUNCH_DRYRUN:+--dry-run} 2>"$_errfile" | tail -1)"
  if [[ -z "$_dir" ]]; then
    printf '%s: workspace entry failed:\n' "$_cmd" >&2
    sed 's/^/  /' "$_errfile" >&2
    rm -f "$_errfile"
    return 1
  fi
  rm -f "$_errfile"

  # Compose the opening-dialogue prompt (agent-takes-first-turn). Suppressed
  # (exit 3) yields no prompt; any OTHER nonzero is an internal opening.py
  # failure — never conflate the two, and never fail the launch over it.
  local _prompt=""
  if [[ -n "$_opening_on" ]]; then
    local -a _opening_args=()
    case "${_spec[0]:-}" in
      --key)                 _opening_args=(--key "${_spec[1]}") ;;
      --new|--name|--init)   _opening_args=(--title "${_spec[1]}") ;;
    esac
    if [[ ${#_opening_args[@]} -gt 0 ]]; then
      local _opening_rc=0
      if [[ -n "${CLAUDE_LAUNCH_DRYRUN:-}" ]]; then
        _prompt="$(CLAUDE_DRY_RUN=1 "$OPENING_BIN" emit --dir "$_dir" "${_opening_args[@]}")"
      else
        _prompt="$("$OPENING_BIN" emit --dir "$_dir" "${_opening_args[@]}")"
      fi
      _opening_rc=$?
      case "$_opening_rc" in
        0) : ;;
        3) _prompt="" ;;
        *)
          _prompt=""
          printf '%s: opening.py exited %d — opening dialogue disabled for this launch\n' \
            "$_cmd" "$_opening_rc" >&2
          ;;
      esac
    fi
  fi

  # Dry-run: report the resolved dir and profile, then return without cd or launch.
  if [[ -n "${CLAUDE_LAUNCH_DRYRUN:-}" ]]; then
    printf 'enter=%s profile=%s config=%s\n' "$_dir" "$_profile" "$_config_value"
    # argv= is a diagnostic only, stderr-only, and never the real prompt text
    # (a literal <prompt> placeholder avoids coupling to the template and a
    # multi-line prompt shattering this one-line report).
    printf 'argv=%s%s\n' "${_prompt:+<prompt> }" "${_cargs[@]+${_cargs[*]}}" >&2
    return 0
  fi

  # Apply auth profile and run the backend binary inside the resolved directory.
  # bash -c receives the dir as $1 and the backend binary as $2; after shifting
  # both away, "$@" is the optional opening prompt plus trailing agent args.
  # env scopes config-dir assignments to this launch only (see the in-place
  # branch for why a VAR=val prefix on the _auth_apply_ns function call is not
  # portable).
  local -a _env_prefix=(env)
  [[ -n "$_config_env" ]] && _env_prefix+=("$_config_env")
  _auth_apply_ns "$_auth_ns" "$_profile" -- \
    "${_env_prefix[@]}" \
    bash -c 'cd "$1" && bin="$2" && shift 2 && command "$bin" "$@"' -- \
    "$_dir" "$_bin" ${_prompt:+"$_prompt"} ${_cargs[@]+"${_cargs[@]}"}
}
