#!/usr/bin/env bash
# Backend registry for the modular task-entry subsystem.
#
# Resolves a backend NAME -> the shell file that implements it, searching the
# built-in directory first and a machine-local plugin directory second. The CLI
# (enter-task.sh) then `source`s the resolved file and calls the contract
# functions below. This file is org-neutral: it knows nothing about any specific
# workspace (git, …) or tracker (github, …) — only how to find one by name.
#
# ── Provider contract ───────────────────────────────────────────────────────
# A WORKSPACE backend file (backends/<name>.sh) defines:
#   backend_detect                       -> exit 0 if usable on this machine
#   backend_ensure_workspace <name> <branch>
#                                        -> ensure an isolated working copy exists
#                                           (create if absent, reuse if present);
#                                           print the project_dir as the FINAL
#                                           stdout line.
#   backend_compose <project_dir>        -> wire `.claude` into project_dir
#                                           (no-op when the working copy already
#                                           carries .claude, as a git worktree does).
#
# Optional init-only extensions (declared only by backends that support --init):
#   backend_init_workspace <name> <target>
#                                        -> create a fresh repo at <target>;
#                                           print <target> as the final stdout line;
#                                           honour CLAUDE_DRY_RUN (log+print, no mkdir/vcs).
#   backend_seal_workspace <dir> <msg>   -> stage all + initial commit in the new repo;
#                                           no-op under CLAUDE_DRY_RUN.
#
# A TRACKER backend file (trackers/<name>.sh) defines:
#   tracker_resolve <key>                -> print 'key<TAB>slug' for an existing task
#   tracker_create  <title>             -> print 'key<TAB>slug' for a newly created task;
#                                          target queue from $CLAUDE_TRACKER_QUEUE (set by
#                                          enter-task.sh from the resolved project record)
#
# Every external tool a backend calls MUST go through a *_BIN env seam (GIT_BIN,
# GH_BIN, …) so the hermetic tests can stub it. Backends must honour CLAUDE_DRY_RUN
# (exported by the CLI): when set to a non-empty value they perform zero external
# side effects.
#
# ── Resolution order ────────────────────────────────────────────────────────
#   1. built-in:      scripts/project_entry/backends/<name>.sh  (resp. trackers/)
#   2. machine-local: ${CLAUDE_PROJECT_PLUGIN_DIR:-<agent-home>/project-entry-plugins}/backends/<name>.sh
#      (<agent-home> is $CLAUDE_AGENT_HOME when isolated, else the legacy
#      ~/.claude/project-entry-plugins — see _plugin_dir below)
# A built-in name is stable (checked first); a plugin ADDS a new name. A fresh
# plugin name not shipped in Core is discovered from the machine-local dir — the
# coupling-free way a specialized backend (arc, …) attaches without editing Core.

# Directory of this registry file -> the built-in backends/trackers live beside it.
_REGISTRY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Machine-local plugin root (overridable for tests / per-machine installs).
# Read-time resolution (same order as lib/config_root.py's agent_home()): an
# explicit override wins; else prefer an existing isolated root; else fall
# back to the legacy ~/.claude location for a not-yet-migrated machine; else
# default to the isolated root (so a fresh machine's first lookup still names
# the isolated path, not the dead one).
_plugin_dir() {
  if [[ -n "${CLAUDE_PROJECT_PLUGIN_DIR:-}" ]]; then
    printf '%s' "$CLAUDE_PROJECT_PLUGIN_DIR"
    return
  fi
  local isolated="${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}/project-entry-plugins"
  local legacy="$HOME/.claude/project-entry-plugins"
  if [[ -d "$isolated" ]]; then
    printf '%s' "$isolated"
  elif [[ -d "$legacy" ]]; then
    printf '%s' "$legacy"
  else
    printf '%s' "$isolated"
  fi
}

# _registry_resolve <kind> <name>  where <kind> is 'backends' or 'trackers'.
# Prints the resolved file path on stdout and returns 0, or prints an error to
# stderr and returns 1.
_registry_resolve() {
  local kind="$1" name="$2" builtin plugin
  builtin="$_REGISTRY_DIR/$kind/$name.sh"
  if [[ -f "$builtin" ]]; then
    printf '%s\n' "$builtin"
    return 0
  fi
  plugin="$(_plugin_dir)/$kind/$name.sh"
  if [[ -f "$plugin" ]]; then
    printf '%s\n' "$plugin"
    return 0
  fi
  printf 'registry: no %s backend named %q (looked in %s and %s)\n' \
    "$kind" "$name" "$builtin" "$plugin" >&2
  return 1
}

registry_resolve_workspace() { _registry_resolve backends "$1"; }
registry_resolve_tracker()   { _registry_resolve trackers "$1"; }
