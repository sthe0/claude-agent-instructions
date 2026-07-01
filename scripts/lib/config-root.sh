# Resolver — source this file from any setup/tooling script that targets the
# agent config root.  Never hardcode $HOME/.claude in install targets.
#
# Override for tests or the Yandex overlay:  export CLAUDE_AGENT_HOME=/path
# Default: ~/.claude-agent  (isolated from the user's personal ~/.claude)
export CLAUDE_AGENT_HOME="${CLAUDE_AGENT_HOME:-$HOME/.claude-agent}"

# agent_legacy_inplace_layout [repo] — single source of truth for detecting the
# OLD in-place install: system symlinks written directly into ~/.claude instead
# of the isolated root. Returns 0 (legacy present, migration needed) when ~/.claude
# holds at least one repo-pointing system symlink AND it is not the isolated root
# itself; returns 1 otherwise (fresh isolated install, or nothing to migrate).
# Read-only. Callers: doctor.sh (WARN) and sync-instructions-repo.sh (auto/notify).
agent_legacy_inplace_layout() {
  local repo="${1:-${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}}"
  [[ -d "$HOME/.claude" ]] || return 1
  [[ "$HOME/.claude" != "$CLAUDE_AGENT_HOME" ]] || return 1
  [[ -d "$repo" ]] || return 1
  local repo_real
  repo_real="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$repo" 2>/dev/null)" || return 1
  [[ -n "$repo_real" ]] || return 1
  local name p tgt
  for name in CLAUDE.md config.md memory-global; do
    p="$HOME/.claude/$name"
    [[ -L "$p" ]] || continue
    tgt="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$p" 2>/dev/null)" || continue
    [[ -n "$tgt" && "$tgt" == "$repo_real"* ]] && return 0
  done
  return 1
}
