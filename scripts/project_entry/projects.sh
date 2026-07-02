#!/usr/bin/env bash
# Shell seam over projects.py — the named, two-root project registry.
#
# Core (enter-task.sh, the launchers) sources this file and calls the four
# project_* functions below; the actual record loading/merging/resolution lives
# in the pure projects.py beside it. This file is org-neutral: it knows how to
# find the registry roots and delegate, nothing about any specific project.
#
# ── Root order (shared first, machine-local last) ───────────────────────────
#   1. shared / versioned:  $CLAUDE_PROJECTS_DIR  (else identity `projects_dir`)
#   2. machine-local:        $CLAUDE_PROJECTS_LOCAL_DIR  (else <config root>/projects.d)
# A later root completes/overrides an earlier one, so a machine-local record can
# attach an absolute workspace_path to a shared, portable definition.

_PROJECTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
_PROJECTS_PY="$_PROJECTS_DIR/projects.py"
# shellcheck source=scripts/lib/config-root.sh
source "$_PROJECTS_DIR/../lib/config-root.sh"   # exports CLAUDE_AGENT_HOME (idempotent)

# Read a key from the machine-local identity file (same file enter-task.sh uses).
_projects_id_get() {
  local f="${CLAUDE_AGENT_IDENTITY:-$CLAUDE_AGENT_HOME/agent-identity.local}"
  [[ -r "$f" ]] && sed -n "s/^$1=//p" "$f" | head -1
}

# Machine-local registry root: $CLAUDE_PROJECTS_LOCAL_DIR, else the read-time
# config root's projects.d. A not-yet-migrated ~/.claude/projects.d is still
# honored when the current root has none (migrate-to-isolated.sh moves it).
_projects_local_dir() {
  if [[ -n "${CLAUDE_PROJECTS_LOCAL_DIR:-}" ]]; then
    printf '%s\n' "$CLAUDE_PROJECTS_LOCAL_DIR"
    return
  fi
  local d
  d="$(agent_home_read)/projects.d"
  if [[ ! -d "$d" && -d "$HOME/.claude/projects.d" ]]; then
    printf '%s\n' "$HOME/.claude/projects.d"
    return
  fi
  printf '%s\n' "$d"
}

# project_roots — print the ordered root list, one per line (shared then local).
project_roots() {
  local shared
  shared="${CLAUDE_PROJECTS_DIR:-$(_projects_id_get projects_dir)}"
  [[ -n "$shared" ]] && printf '%s\n' "$shared"
  _projects_local_dir
}

# Join project_roots with the OS path separator into CLAUDE_PROJECT_ROOTS so the
# pure Python side receives the exact ordered list (identity resolution stays in
# bash). Echoed via a subshell so callers can `export` the result themselves.
_projects_roots_env() {
  local out="" r
  while IFS= read -r r; do
    [[ -n "$r" ]] || continue
    out="${out:+$out:}$r"
  done < <(project_roots)
  printf '%s' "$out"
}

project_list() {
  CLAUDE_PROJECT_ROOTS="$(_projects_roots_env)" python3 "$_PROJECTS_PY" list
}

# project_resolve [selector] — print the resolved key, or return non-zero.
# Resolution uses $PWD when no selector is given (handled in projects.py).
project_resolve() {
  CLAUDE_PROJECT_ROOTS="$(_projects_roots_env)" python3 "$_PROJECTS_PY" resolve "$@"
}

# project_register <root> <key> [field=value ...] — write a machine-local record.
project_register() {
  python3 "$_PROJECTS_PY" register "$@"
}

# project_local_root — print the machine-local registry root path.
project_local_root() {
  _projects_local_dir
}

# project_get_fields [selector] — print resolved record fields as key=value lines, or return non-zero.
project_get_fields() {
  CLAUDE_PROJECT_ROOTS="$(_projects_roots_env)" python3 "$_PROJECTS_PY" fields "$@"
}
