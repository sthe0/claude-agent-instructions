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
#   2. machine-local:        $CLAUDE_PROJECTS_LOCAL_DIR  (else ~/.claude/projects.d)
# A later root completes/overrides an earlier one, so a machine-local record can
# attach an absolute workspace_path to a shared, portable definition.

_PROJECTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PROJECTS_PY="$_PROJECTS_DIR/projects.py"

# Read a key from the machine-local identity file (same file enter-task.sh uses).
_projects_id_get() {
  local f="${CLAUDE_AGENT_IDENTITY:-$HOME/.claude/agent-identity.local}"
  [[ -r "$f" ]] && sed -n "s/^$1=//p" "$f" | head -1
}

# project_roots — print the ordered root list, one per line (shared then local).
project_roots() {
  local shared local_dir
  shared="${CLAUDE_PROJECTS_DIR:-$(_projects_id_get projects_dir)}"
  local_dir="${CLAUDE_PROJECTS_LOCAL_DIR:-$HOME/.claude/projects.d}"
  [[ -n "$shared" ]] && printf '%s\n' "$shared"
  printf '%s\n' "$local_dir"
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
