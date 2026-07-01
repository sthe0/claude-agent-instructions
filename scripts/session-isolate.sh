#!/usr/bin/env bash
# session-isolate.sh <task-name> — Component C remediation for a filesystem-scope
# conflict flagged by hook-scope-conflict.py (docs/operations/cross-session-scope-isolation.md).
#
# Routes the current session's contended task into its own isolated workspace by
# REUSING project_entry's workspace-backend contract (registry.sh + backends/git.sh
# backend_ensure_workspace) — no new isolation mechanism is invented here. It then
# re-registers this session's filesystem scope (session_scope.registry.set_context)
# at the NEW workspace root, so session_scope.detector immediately sees this
# session and the holder as non-overlapping: two disjoint worktree roots never
# path-overlap, which is what stops the conflict hook from firing again.
#
# git backend only in this slice (arc is a later slice); the workspace-backend
# name still resolves through registry.sh so a future arc backend attaches with
# no change to this file.
#
# Usage: session-isolate.sh <task-name>
#
# Honors CLAUDE_DRY_RUN: when set, backend_ensure_workspace performs zero
# mutating git calls (see backends/git.sh) and this script creates nothing on
# disk. The session's scope re-registration always runs when CLAUDE_SESSION_ID
# is set — it is local bookkeeping under ~/.claude/agentctl/scopes (the same
# family as agentctl's own state store), not a mutation of the task's tree, and
# recording it is the entire point of the dry-run check: proving the detector
# would see the isolated root.
#
# Reads CLAUDE_SESSION_ID for the session to re-register (same seam
# spawn-specialist.py uses to pass session_id through).
#
# Prints the new workspace directory as the FINAL stdout line; progress and the
# land-back instructions go to stderr.
set -uo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/project_entry/registry.sh
source "$_SCRIPT_DIR/project_entry/registry.sh"

log() { printf '%s\n' "$*" >&2; }
die() { printf 'session-isolate: %s\n' "$*" >&2; exit 1; }

[[ $# -eq 1 ]] || die "usage: session-isolate.sh <task-name>"
task_name="$1"

slug() {
  python3 "$_SCRIPT_DIR/project_entry/slugify.py" "$1"
}

name="$(slug "$task_name")"
[[ -n "$name" ]] || name="$task_name"
branch="$name"

# ── Resolve the workspace backend NAME (env override > detector > git default) ──
# Mirrors enter-task.sh's degrade-safe resolution: an inferred name that isn't
# installed on this machine falls back to the org-neutral git default.
_detector="${CLAUDE_BACKEND_DETECTOR:-$_SCRIPT_DIR/project_entry/detect_backend.py}"
det_ws="git"
if read -r _dws _dtr < <(python3 "$_detector" 2>/dev/null); then
  [[ -n "${_dws:-}" ]] && det_ws="$_dws"
fi

ws_from_flag=""
if [[ -n "${CLAUDE_WORKSPACE_BACKEND:-}" ]]; then
  ws_name="$CLAUDE_WORKSPACE_BACKEND"; ws_from_flag=1
else
  ws_name="$det_ws"
fi

if ! ws_file="$(registry_resolve_workspace "$ws_name")"; then
  if [[ -z "$ws_from_flag" && "$ws_name" != "git" ]]; then
    log "session-isolate: workspace backend '$ws_name' not installed; falling back to git"
    ws_name="git"
    ws_file="$(registry_resolve_workspace "$ws_name")" || die "cannot resolve workspace backend 'git'"
  else
    die "cannot resolve workspace backend '$ws_name'"
  fi
fi
# shellcheck source=/dev/null
source "$ws_file"

log "session-isolate: isolating task '$name' (workspace=$ws_name)${CLAUDE_DRY_RUN:+ (dry-run)}"

# ── Ensure the isolated workspace (reused verbatim from project_entry) ──────
project_dir="$(backend_ensure_workspace "$name" "$branch")" || die "backend_ensure_workspace failed"
project_dir="$(printf '%s' "$project_dir" | tail -1)"

if [[ -z "${CLAUDE_DRY_RUN:-}" ]]; then
  backend_compose "$project_dir" || die "backend_compose failed"
else
  log "session-isolate: [dry-run] skipping compose for $project_dir"
fi

# ── Re-register this session's scope at the NEW workspace root ─────────────
# Always runs (dry-run or not) when a session id is known: it is what makes the
# isolation immediately visible to the conflict detector, and it never touches
# the caller's git branch/worktree/index.
session_id="${CLAUDE_SESSION_ID:-}"
if [[ -n "$session_id" ]]; then
  PYTHONPATH="$_SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -c '
import sys
from session_scope import registry
session_id, project_dir, vcs = sys.argv[1:4]
registry.set_context(session_id, project_dir, project_dir, vcs)
' "$session_id" "$project_dir" "$ws_name" \
    || log "session-isolate: warning: could not re-register scope for session $session_id"
else
  log "session-isolate: CLAUDE_SESSION_ID not set; skipping scope re-registration" \
      "(the conflict detector will keep seeing the OLD root until this session's next heartbeat)"
fi

log "session-isolate: isolated workspace ready at $project_dir"
log "session-isolate: continue the task there, e.g.:  cd \"$project_dir\""
log "session-isolate: when done, stage your change there and land it back:"
log "  $_SCRIPT_DIR/land-on-main.sh -C \"$project_dir\" -m \"<message>\""

printf '%s\n' "$project_dir"
