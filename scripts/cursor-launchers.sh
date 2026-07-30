#!/usr/bin/env bash
# cursor-launchers.sh — Cursor backend registration for the shared agent dispatch.
#
# Source this file from ~/.bashrc or ~/.zshrc (or equivalent). It defines:
#   cursor-task      dispatch with the 'default' auth profile (workspace management)
#   cursor-<P>       dispatch with machine-local profile P (generated per profile
#                    listed by _auth_list_ns cursor at source time; unconfigured
#                    machine exposes only cursor-task)
#
# Deliberate asymmetries vs claude-launchers.sh:
#   1. No `cursor-agent` plain-launch twin of `claude-agent`. That name is already
#      the Cursor CLI binary itself and must not be shadowed — expressed by the
#      empty plain_cmd descriptor, which also omits the usage "See also:" line.
#   2. Runs on the user's own ~/.cursor config root (rules, skills-cursor, mcp.json,
#      the logged-in account), not an isolated one — empty config env. A future
#      isolated cursor root is a one-line descriptor change.
#
# Works in bash and zsh: self-locates via BASH_SOURCE (bash) or $0 (zsh).
# Registration only — all dispatch / usage / onboard live in agent-dispatch.sh.
#
# Env seams for tests (shared with agent-dispatch.sh):
#   CURSOR_AUTH_PROFILE_DIR override the cursor profile dir (consumed by auth-profiles.sh)
#   CLAUDE_LAUNCH_DRYRUN    set to any non-empty value to engage dry-run mode
#   CLAUDE_SKIP_ONBOARD     set to any non-empty value to skip the init probe
#   CLAUDE_OPENING          off|on — force-suppress or force-enable the opening dialogue

# Self-locate: Core scripts/ dir (siblings of project_entry/).
_LAUNCHERS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# Shared dispatch library (config root, auth profiles, projects, onboard, dispatch).
# shellcheck source=project_entry/agent-dispatch.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/agent-dispatch.sh"

# ── Cursor backend descriptors (looked up by agent-dispatch.sh) ───────────────
_backend_cursor_prefix()  { printf 'cursor\n'; }
_backend_cursor_bin()     { printf 'cursor-agent\n'; }
_backend_cursor_auth_ns() { printf 'cursor\n'; }
# Empty: no isolated config-dir assignment (user's ~/.cursor). Independent of
# the plain-cmd axis — both happen to be empty for cursor, but that is not a rule.
_backend_cursor_config_env() { :; }
# Empty: no plain-launch twin (name taken by the CLI binary); omits "See also:".
_backend_cursor_plain_cmd()  { :; }

# ── cursor-task (default auth profile) ───────────────────────────────────────
cursor-task() { _dispatch_agent cursor default "$@"; }

# ── cursor-<P> (one per machine-local profile, induced at source time) ────────
# On an unconfigured machine (_auth_list_ns cursor returns only 'default'), this
# loop defines no extra commands and only cursor-task is available.
for _lp in $(_auth_list_ns cursor); do
  [[ "$_lp" == "default" ]] && continue
  # shellcheck disable=SC2064
  eval "cursor-${_lp}() { _dispatch_agent cursor '${_lp}' \"\$@\"; }"
done
unset _lp
