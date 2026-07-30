#!/usr/bin/env bash
# claude-launchers.sh — Claude backend registration for the shared agent dispatch.
#
# Source this file from ~/.bashrc or ~/.zshrc (or equivalent). It defines:
#   claude-task      dispatch with the 'default' auth profile (workspace management)
#   claude-<P>       dispatch with machine-local profile P (generated per profile
#                    listed by _auth_list_ns claude at source time; unconfigured
#                    machine exposes only claude-task)
#   claude-agent     plain launch on the system config root (no workspace management;
#                    for scripted -p/-c use). First use: claude-agent /login
#   onboard          user-callable wrapper (from agent-dispatch.sh)
#
# All launchers run on the isolated system config root (CLAUDE_AGENT_HOME,
# default ~/.claude-agent). Bare `claude` uses the user's personal ~/.claude.
#
# Core ships only the 'default' profile + the apply/list framework; no
# specialized auth. Any pre-existing machine-local raw claude() fallback (e.g. one
# that sources a proxy env file) lives in ~/.bashrc, not here, so this file stays
# org-neutral.
#
# Works in bash and zsh: self-locates via BASH_SOURCE (bash) or $0 (zsh). Sourced
# functions so that cd can persist in the caller's shell if callers extend the
# dispatch.
#
# Env seams for tests:
#   ENTER_TASK_BIN          override the enter-task.sh path (default: co-located script)
#   OPENING_BIN             override the opening.py path (default: co-located project_entry/opening.py)
#   CLAUDE_AUTH_PROFILE_DIR override the profile dir (consumed by auth-profiles.sh)
#   CLAUDE_LAUNCH_DRYRUN    set to any non-empty value to engage dry-run mode
#   CLAUDE_ONBOARD_HOOK_DIR override the onboard hook dir (default: ~/.config/claude/onboard.d)
#   CLAUDE_SKIP_ONBOARD     set to any non-empty value to skip the init probe
#   CLAUDE_ONBOARD_BIN      override the onboard.sh path (default: co-located onboard.sh)
#   CLAUDE_OPENING          off|on — force-suppress or force-enable the opening dialogue
#                           (--no-opening / --opening on the command line take precedence)

# Self-locate: Core scripts/ dir (where enter-task.sh and project_entry/ live).
_LAUNCHERS_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# Shared dispatch library (config root, auth profiles, projects, onboard, dispatch).
# shellcheck source=project_entry/agent-dispatch.sh
source "$_LAUNCHERS_SCRIPTS_DIR/project_entry/agent-dispatch.sh"

# ── Claude backend descriptors (looked up by agent-dispatch.sh) ───────────────
_backend_claude_prefix()  { printf 'claude\n'; }
_backend_claude_bin()     { printf 'claude\n'; }
_backend_claude_auth_ns() { printf 'claude\n'; }
# Zero or more VAR=VALUE assignments for the `env` prefix. Independent of the
# plain-cmd axis: a backend may emit a config env, a plain twin, both, or neither.
_backend_claude_config_env() { printf 'CLAUDE_CONFIG_DIR=%s\n' "$CLAUDE_AGENT_HOME"; }
_backend_claude_plain_cmd()  { printf 'claude-agent\n'; }

# Compatibility alias: existing callers / muscle memory for the Claude-only name.
_dispatch_with_profile() { _dispatch_agent claude "$@"; }

# ── claude-task (default auth profile) ───────────────────────────────────────
claude-task() { _dispatch_agent claude default "$@"; }

# ── claude-agent (system plain-launch, isolated config root) ──────────────────
# Direct launch on the system config root without workspace management.
# Use for scripted -p / -c invocations where workspace isolation is not needed.
# Auth: if the system root is not yet authenticated, run `claude-agent /login`
# once (or set CLAUDE_CONFIG_DIR=$CLAUDE_AGENT_HOME and run `claude /login`).
claude-agent() { env CLAUDE_CONFIG_DIR="$CLAUDE_AGENT_HOME" claude "$@"; }

# ── claude-<P> (one per machine-local profile, induced at source time) ────────
# On an unconfigured machine (_auth_list_ns claude returns only 'default'), this
# loop defines no extra commands and only claude-task is available.
for _lp in $(_auth_list_ns claude); do
  [[ "$_lp" == "default" ]] && continue
  # shellcheck disable=SC2064
  eval "claude-${_lp}() { _dispatch_agent claude '${_lp}' \"\$@\"; }"
done
unset _lp
