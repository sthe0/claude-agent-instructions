#!/usr/bin/env bash
# Print a deterministic text listing of the Core canon targets an instruction
# test root must never touch. Run it in the caller's real environment before and
# after a build; identical output means canon was left unchanged.
#
# Covers: the agent-root instruction/skill/agent links, settings and identity
# files, the personal settings file, the editor rule/agent links, and the
# canonical checkout's hooksPath, non-session worktrees and scripts status.
# Session worktrees (<canon>/.claude/worktrees/) and __pycache__ byproducts are
# excluded: unrelated parallel sessions and interpreter runs create them.
#
# Canon checkout: CLAUDE_INSTRUCTIONS_CANON, default $HOME/claude-agent-instructions.
# Exits nonzero rather than print a partial listing.
set -euo pipefail
shopt -s inherit_errexit

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

canon="${CLAUDE_INSTRUCTIONS_CANON:-$HOME/claude-agent-instructions}"
agent_home="$HOME/.claude-agent"

emit_link() {
  printf 'link %s -> %s\n' "$1" "$(readlink "$1")"
}

emit_entries() {
  local dir="$1" entry
  [[ -d "$dir" ]] || return 0
  for entry in "$dir"/* "$dir"/.[!.]*; do
    if [[ -L "$entry" ]]; then
      emit_link "$entry"
    elif [[ -e "$entry" ]]; then
      printf 'entry %s\n' "$entry"
    fi
  done
  return 0
}

emit_sha() {
  if [[ -e "$1" ]]; then
    printf 'sha256 %s %s\n' "$1" "$(sha256sum < "$1" | cut -d' ' -f1)"
  else
    printf 'sha256 %s ABSENT\n' "$1"
  fi
}

listing() {
  local name hp line
  for name in CLAUDE.md config.md memory-global; do
    if [[ -L "$agent_home/$name" ]]; then
      emit_link "$agent_home/$name"
    else
      printf 'link %s -> ABSENT\n' "$agent_home/$name"
    fi
  done
  emit_entries "$agent_home/skills"
  emit_entries "$agent_home/agents"
  emit_entries "$HOME/.cursor/rules"
  emit_entries "$HOME/.cursor/agents"
  emit_sha "$agent_home/settings.json"
  emit_sha "$agent_home/agent-identity.local"
  emit_sha "$HOME/.claude/settings.json"

  git -C "$canon" rev-parse --git-dir > /dev/null
  hp="$(git -C "$canon" config --local --get core.hooksPath || true)"
  printf 'git-hooks-path %s %s\n' "$canon" "${hp:-UNSET}"
  git -C "$canon" worktree list --porcelain | while IFS= read -r line; do
    case "$line" in
      "worktree $canon/.claude/worktrees/"*) ;;
      "worktree "*) printf 'git-worktree %s\n' "${line#worktree }" ;;
    esac
  done
  git -C "$canon" status --porcelain --untracked-files=all -- scripts cursor githooks \
    | { grep -v '__pycache__/' || true; } \
    | while IFS= read -r line; do printf 'git-status-scripts %s\n' "$line"; done
}

out="$(listing)"
printf '%s\n' "$out" | LC_ALL=C sort
