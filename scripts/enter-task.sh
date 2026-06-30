#!/usr/bin/env bash
# enter-task — modular, org-neutral entry point for starting work on a task.
#
# Resolves a workspace backend (default: git worktree) and an optional tracker
# backend (default: GitHub issue when `gh` is present, else none), derives the
# task name + branch, ensures an isolated working copy exists, wires `.claude`,
# and prints the project working directory as the FINAL stdout line. All progress
# / logging goes to stderr, so a caller can `cd "$(enter-task.sh …)"`.
#
# This file is org-neutral: it knows the provider contract and nothing about any
# specific backend implementation. Specialized backends (arc, …) attach as
# machine-local plugins discovered by registry.sh — no edit to this file needed.
#
# Usage:
#   enter-task.sh (--key <K> | --new <title> | --reuse | --name <plain>)
#                 [--workspace <backend>] [--tracker <backend>]
#                 [--project <registry-key>] [--dry-run]
#                 [--list-projects]
#                 [--register <repo-path> [--as <registry-key>]]
#
# Backend NAME selection precedence:
#   explicit flag  >  env (CLAUDE_WORKSPACE_BACKEND / CLAUDE_TRACKER_BACKEND)
#   >  project record (workspace_backend / tracker_backend)
#   >  identity (project_backend / tracker_backend)  >  detector (git / github-if-gh-else-none).
#
# --dry-run performs ZERO external effects (no git/gh/compose side effects).
set -uo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/project_entry/registry.sh
source "$_SCRIPT_DIR/project_entry/registry.sh"
# shellcheck source=scripts/project_entry/projects.sh
source "$_SCRIPT_DIR/project_entry/projects.sh"

log() { printf '%s\n' "$*" >&2; }
die() { printf 'enter-task: %s\n' "$*" >&2; exit 1; }

# slug: delegate to slugify.py — transliterates Cyrillic, folds accents, and may
# return empty (caller handles the empty-slug fallback). python3 is a hard system
# dependency here, so no bash fallback is needed.
slug() {
  python3 "$_SCRIPT_DIR/project_entry/slugify.py" "$1"
}

# ── Parse arguments ─────────────────────────────────────────────────────────
selector="" sel_arg=""           # one of: key|new|reuse|name + its value
ws_flag="" tr_flag="" project=""
DRY_RUN=""
do_list_projects="" do_register="" register_path="" register_as=""

set_selector() {
  [[ -z "$selector" ]] || die "--key/--new/--reuse/--name are mutually exclusive"
  selector="$1"; sel_arg="${2:-}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key)       set_selector key "${2:-}"; shift 2 ;;
    --new)       set_selector new "${2:-}"; shift 2 ;;
    --reuse)     set_selector reuse "";     shift 1 ;;
    --name)      set_selector name "${2:-}"; shift 2 ;;
    --workspace) ws_flag="${2:-}"; shift 2 ;;
    --tracker)   tr_flag="${2:-}"; shift 2 ;;
    --project)        project="${2:-}"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift 1 ;;
    --list-projects)  do_list_projects=1; shift 1 ;;
    --register)       do_register=1; register_path="${2:-}"; shift 2 ;;
    --as)             register_as="${2:-}"; shift 2 ;;
    -h|--help)        grep '^#' "$0" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0 ;;
    *)                die "unknown argument: $1" ;;
  esac
done

# ── Early exits (no selector required) ─────────────────────────────────────
if [[ -n "$do_list_projects" ]]; then
  project_list
  exit $?
fi
if [[ -n "$do_register" ]]; then
  [[ -n "$register_path" ]] || die "--register needs a repo path"
  _local_root="$(project_local_root)"
  _reg_key="${register_as:-$(basename "$register_path")}"
  project_register "$_local_root" "$_reg_key" "workspace_path=$register_path" "workspace_backend=git"
  exit $?
fi

[[ -n "$selector" ]] || die "specify one of --key / --new / --reuse / --name"
export CLAUDE_DRY_RUN="$DRY_RUN"
# Contract seam: a sourced tracker plugin can locate Core's slugify.py via this.
export CLAUDE_ENTER_TASK_DIR="$_SCRIPT_DIR"

# ── Resolve project record (registry-key selector or PWD auto-detect) ────────
_proj_resolved="" _proj_key=""
_rec_ws_subpath="" _rec_ws_path="" _rec_ws_backend="" _rec_tracker="" _rec_queue=""
if [[ -n "$project" ]]; then
  # Explicit --project selector: must resolve or die with the available list.
  _fields_out="$(project_get_fields "$project" 2>/dev/null)" || {
    log "enter-task: unknown project '$project'; available projects:"
    project_list >&2
    exit 1
  }
  _proj_resolved=1
else
  # Auto-detect from $PWD via workspace_subpath / workspace_path suffix match.
  _fields_out="$(project_get_fields 2>/dev/null)" && _proj_resolved=1 || _fields_out=""
fi
if [[ -n "$_proj_resolved" ]]; then
  while IFS= read -r _line; do
    [[ -z "$_line" ]] && continue
    _k="${_line%%=*}"; _v="${_line#*=}"
    case "$_k" in
      workspace_subpath) _rec_ws_subpath="$_v" ;;
      workspace_path)    _rec_ws_path="$_v" ;;
      workspace_backend) _rec_ws_backend="$_v" ;;
      tracker_backend)   _rec_tracker="$_v" ;;
      tracker_queue)     _rec_queue="$_v" ;;
      _key)              _proj_key="$_v" ;;
    esac
  done <<<"$_fields_out"
fi

# ── Resolve backend NAMES (precedence: flag > env > record > identity > detector) ─
# The detector's own else-branch (git/none) is the final fallback, so there is
# no separate hardcoded default below it. The two identity keys live next to
# difficulty_channel in the machine-local agent-identity file.
GH_BIN="${GH_BIN:-gh}"   # consumed by trackers/github.sh

_identity_file="${CLAUDE_AGENT_IDENTITY:-$HOME/.claude/agent-identity.local}"
_id_get() { [[ -r "$_identity_file" ]] && sed -n "s/^$1=//p" "$_identity_file" | head -1; }
id_ws="$(_id_get project_backend)"
id_tr="$(_id_get tracker_backend)"

# Run the detector once. Seam CLAUDE_BACKEND_DETECTOR points at a python script
# printing "<workspace> <tracker>"; default is the real detect_backend.py. Falls
# back to git/none if the detector yields nothing.
_detector="${CLAUDE_BACKEND_DETECTOR:-$_SCRIPT_DIR/project_entry/detect_backend.py}"
det_ws="git" det_tr="none"
if read -r _dws _dtr < <(python3 "$_detector" 2>/dev/null); then
  [[ -n "${_dws:-}" ]] && det_ws="$_dws"
  [[ -n "${_dtr:-}" ]] && det_tr="$_dtr"
fi

ws_from_flag="" tr_from_flag=""
if   [[ -n "$ws_flag" ]];                      then ws_name="$ws_flag"; ws_from_flag=1
elif [[ -n "${CLAUDE_WORKSPACE_BACKEND:-}" ]]; then ws_name="$CLAUDE_WORKSPACE_BACKEND"
elif [[ -n "$_rec_ws_backend" ]];              then ws_name="$_rec_ws_backend"
elif [[ -n "$id_ws" ]];                        then ws_name="$id_ws"
else                                                ws_name="$det_ws"
fi

if   [[ -n "$tr_flag" ]];                      then tr_name="$tr_flag"; tr_from_flag=1
elif [[ -n "${CLAUDE_TRACKER_BACKEND:-}" ]];   then tr_name="$CLAUDE_TRACKER_BACKEND"
elif [[ -n "$_rec_tracker" ]];                 then tr_name="$_rec_tracker"
elif [[ -n "$id_tr" ]];                        then tr_name="$id_tr"
else                                                tr_name="$det_tr"
fi

# Export record-derived seams; never override an explicit env value.
[[ -z "${CLAUDE_PROJECT_SUBPATH:-}" && -n "$_rec_ws_subpath" ]] && export CLAUDE_PROJECT_SUBPATH="$_rec_ws_subpath"
[[ -z "${CLAUDE_WORKSPACE_ROOT:-}"  && -n "$_rec_ws_path"    ]] && export CLAUDE_WORKSPACE_ROOT="$_rec_ws_path"
[[ -z "${CLAUDE_TRACKER_QUEUE:-}"   && -n "$_rec_queue"      ]] && export CLAUDE_TRACKER_QUEUE="$_rec_queue"

# A --name / --reuse selector never wants a tracker; force 'none' for them.
case "$selector" in name|reuse) tr_name="none" ;; esac

log "enter-task: workspace=$ws_name tracker=$tr_name selector=$selector${DRY_RUN:+ (dry-run)}"
# When a registry record resolved, surface its provenance fields so callers (and
# tests) can see which project/subpath/queue drove the entry.
[[ -n "$_proj_resolved" ]] && \
  log "enter-task: project=$_proj_key subpath=$_rec_ws_subpath queue=$_rec_queue"

# ── Empty-context guard ──────────────────────────────────────────────────────
# For --new / --key: if no project resolved and no explicit tracker was named,
# abort with the available-projects list so the user knows to use --project.
if [[ "$selector" == "new" || "$selector" == "key" ]]; then
  if [[ -z "$_proj_resolved" && -z "$tr_from_flag" && -z "${CLAUDE_TRACKER_BACKEND:-}" ]]; then
    log "enter-task: no project resolved from context (cwd=$PWD)"
    log "enter-task: use --project <key> or cd into a project directory; available projects:"
    project_list >&2
    exit 1
  fi
fi

# ── Source the chosen workspace backend ─────────────────────────────────────
# Degrade-safe: an INFERRED name (env/identity/detector) whose backend isn't
# installed on this machine falls back to the org-neutral git default rather
# than aborting; an explicit --workspace flag still errors loudly on a typo.
if ! ws_file="$(registry_resolve_workspace "$ws_name")"; then
  if [[ -z "$ws_from_flag" && "$ws_name" != "git" ]]; then
    log "enter-task: workspace backend '$ws_name' not installed; falling back to git"
    ws_name="git"
    ws_file="$(registry_resolve_workspace "$ws_name")" || die "cannot resolve workspace backend 'git'"
  else
    die "cannot resolve workspace backend '$ws_name'"
  fi
fi
# shellcheck source=/dev/null
source "$ws_file"

# Source the tracker backend only when one is actually requested.
if [[ "$tr_name" != "none" ]]; then
  if ! tr_file="$(registry_resolve_tracker "$tr_name")"; then
    if [[ -z "$tr_from_flag" ]]; then
      log "enter-task: tracker backend '$tr_name' not installed; falling back to none"
      tr_name="none"
    else
      die "cannot resolve tracker backend '$tr_name'"
    fi
  fi
  if [[ "$tr_name" != "none" ]]; then
    # shellcheck source=/dev/null
    source "$tr_file"
  fi
fi

# ── Resolve the task -> name + branch ───────────────────────────────────────
name="" branch=""
case "$selector" in
  key)
    [[ -n "$sel_arg" ]] || die "--key needs a value"
    [[ "$tr_name" != "none" ]] || die "--key requires a tracker backend (got none)"
    IFS=$'\t' read -r tkey tslug < <(tracker_resolve "$sel_arg") || die "tracker_resolve failed for '$sel_arg'"
    name="$tkey${tslug:+-$tslug}"; branch="$name"
    ;;
  new)
    [[ -n "$sel_arg" ]] || die "--new needs a title"
    [[ "$tr_name" != "none" ]] || die "--new requires a tracker backend (got none)"
    if [[ -z "$DRY_RUN" && "${CLAUDE_LAUNCH_ASSUME_YES:-}" != "1" ]]; then
      die "--new creates a tracker task; set CLAUDE_LAUNCH_ASSUME_YES=1 to confirm"
    fi
    IFS=$'\t' read -r tkey tslug < <(tracker_create "$sel_arg") || die "tracker_create failed"
    name="$tkey${tslug:+-$tslug}"; branch="$name"
    ;;
  reuse)
    # Derive the name from $PWD when it sits under a recognizable worktree dir
    # named <repo>-<name>; otherwise guide the user to pass an explicit name.
    base="$(basename "$PWD")"
    [[ "$base" == *-* ]] || die "--reuse: cannot derive a task name from '$PWD'; pass --name <plain>"
    name="${base#*-}"; branch="$name"
    ;;
  name)
    [[ -n "$sel_arg" ]] || die "--name needs a value"
    name="$(slug "$sel_arg")"; branch="$name"
    ;;
esac

[[ -n "$name" ]] || die "could not derive a task name"

# ── Ensure the workspace, compose, print the dir ────────────────────────────
project_dir="$(backend_ensure_workspace "$name" "$branch")" || die "backend_ensure_workspace failed"
project_dir="$(printf '%s' "$project_dir" | tail -1)"

if [[ -z "$DRY_RUN" ]]; then
  backend_compose "$project_dir" || die "backend_compose failed"
else
  log "enter-task: [dry-run] skipping compose for $project_dir"
fi

# project_dir is ALWAYS the final stdout line.
printf '%s\n' "$project_dir"
