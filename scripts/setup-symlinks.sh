#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Single source of truth for the agent config root (default: ~/.claude-agent).
# Override with CLAUDE_AGENT_HOME=/path for tests or the Yandex overlay.
source "$REPO/scripts/lib/config-root.sh"

mkdir -p "$CLAUDE_AGENT_HOME" "$HOME/.cursor"

link() {
  local target="$1" linkpath="$2"
  if [[ -e "$linkpath" && ! -L "$linkpath" ]]; then
    echo "refuse: $linkpath exists and is not a symlink (move aside manually)" >&2
    exit 1
  fi
  ln -sfn "$target" "$linkpath"
}

link_agent_md() {
  local file_path="$1"
  local base
  base="$(basename "$file_path")"
  [[ "$base" == "README.md" ]] && return 0
  link "$file_path" "$CLAUDE_AGENT_HOME/agents/$base"
}

link_skill_dir() {
  local dir_path="$1"
  local base
  base="$(basename "$dir_path")"
  link "$dir_path" "$CLAUDE_AGENT_HOME/skills/$base"
}

# Skills that live outside the repo: the repo-local skills-local/ (untracked, kept
# for back-compat) and the machine-local overlay under $CLAUDE_AGENT_HOME. The
# overlay is where skills extracted from Core live, so a machine keeps them
# invocable without the public repo carrying them.
link_local_skills() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  local entry base
  for entry in "$dir"/*; do
    [[ -e "$entry" ]] || continue
    base="$(basename "$entry")"
    [[ "$base" == "README.md" ]] && continue
    if [[ -f "$entry" && "$entry" == *.md ]]; then
      # Single-file skill — link the .md directly.
      link "$entry" "$CLAUDE_AGENT_HOME/skills/$base"
    elif [[ -d "$entry" && -f "$entry/SKILL.md" ]]; then
      # Multi-file skill — link the whole directory (mirrors how skills/ are linked).
      link "$entry" "$CLAUDE_AGENT_HOME/skills/$base"
    fi
  done
  return 0
}

prune_dangling() {
  local dir="$1"
  local logfile="$HOME/.local/log/setup-symlinks-prune.log"
  mkdir -p "$(dirname "$logfile")"
  local stale now
  while IFS= read -r stale; do
    [[ -z "$stale" ]] && continue
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "$now prune-dangling: $stale" >&2
    echo "$now prune-dangling: $stale" >> "$logfile"
    rm "$stale"
  done < <(find "$dir" -maxdepth 1 -type l ! -exec test -e {} \; -print 2>/dev/null || true)
}

# Core global symlinks
link "$REPO/CLAUDE.md" "$CLAUDE_AGENT_HOME/CLAUDE.md"
link "$REPO/config.md" "$CLAUDE_AGENT_HOME/config.md"
link "$REPO/memory-global" "$CLAUDE_AGENT_HOME/memory-global"

# Do not install org-yandex globally — it lives in robot/deepagent/.claude/rules/
rm -f "$HOME/.cursor/rules/org-yandex.mdc"
rm -rf "$CLAUDE_AGENT_HOME/scripts-local" 2>/dev/null || true

# Agents: global only in $CLAUDE_AGENT_HOME/agents/
if [[ -L "$CLAUDE_AGENT_HOME/agents" ]]; then
  rm "$CLAUDE_AGENT_HOME/agents"
fi
mkdir -p "$CLAUDE_AGENT_HOME/agents"
prune_dangling "$CLAUDE_AGENT_HOME/agents"

for file_path in "$REPO/agents/"*.md; do
  [[ -f "$file_path" ]] && link_agent_md "$file_path"
done

if [[ -d "$REPO/agents-local" ]]; then
  for file_path in "$REPO/agents-local/"*.md; do
    [[ -f "$file_path" ]] && link_agent_md "$file_path"
  done
fi

# Skills: global instruction skills only in $CLAUDE_AGENT_HOME/skills/
if [[ -L "$CLAUDE_AGENT_HOME/skills" ]]; then
  rm "$CLAUDE_AGENT_HOME/skills"
fi
mkdir -p "$CLAUDE_AGENT_HOME/skills"
prune_dangling "$CLAUDE_AGENT_HOME/skills"

# Remove legacy home symlinks to Arcadia artifacts or the0-agents
for entry in "$CLAUDE_AGENT_HOME/skills/"*; do
  [[ -e "$entry" ]] || continue
  base="$(basename "$entry")"
  [[ "$base" == "overcome-difficulty" || "$base" == "self-improvement" || "$base" == "README.md" ]] && continue
  if [[ -L "$entry" ]]; then
    target="$(readlink "$entry")"
    if [[ "$target" == *"/ai/artifacts/skills"* ]] || [[ "$target" == *"arcadia_the0-agents"* ]]; then
      rm -f "$entry"
    fi
  elif [[ -d "$entry" ]]; then
    rm -rf "$entry"
  fi
done

if [[ -d "$REPO/skills" ]]; then
  for dir_path in "$REPO/skills/"*/; do
    [[ -d "$dir_path" ]] || continue
    dir_path="${dir_path%/}"
    base="$(basename "$dir_path")"
    # skip the specializations container itself; iterate its contents below
    [[ "$base" == "specializations" ]] && continue
    link_skill_dir "$dir_path"
  done
fi

# Specializations live in skills/specializations/ but are symlinked flat into
# $CLAUDE_AGENT_HOME/skills/<name>/ so the Claude Code skill catalog sees them by name.
if [[ -d "$REPO/skills/specializations" ]]; then
  for dir_path in "$REPO/skills/specializations/"*/; do
    [[ -d "$dir_path" ]] || continue
    dir_path="${dir_path%/}"
    link_skill_dir "$dir_path"
  done
fi

link_local_skills "$REPO/skills-local"
link_local_skills "$CLAUDE_AGENT_HOME/skills-local"

# Drop legacy per-agent symlinks to the0-agents / logos / deepagent project agents
for entry in "$CLAUDE_AGENT_HOME/agents/"*.md; do
  [[ -L "$entry" ]] || continue
  target="$(readlink "$entry")"
  if [[ "$target" == *"arcadia_the0-agents"* ]] || [[ "$target" == *"/logos/"* ]] || [[ "$target" == *"/robot/deepagent/.claude/agents"* ]]; then
    rm -f "$entry"
  fi
done

chmod +x "$REPO/scripts/verify-instructions-sync.sh" "$REPO/scripts/verify-layout-contract.sh" "$REPO/scripts/setup-project-memory.sh" "$REPO/scripts/apply-settings.sh" "$REPO/cursor/scripts/install-cursor-links.sh" "$REPO/cursor/scripts/link-project-cursor-agents.sh" "$REPO/cursor/scripts/migrate-cursor-namespace.sh" "$REPO/scripts/migrate-to-isolated.sh"

# Every hook is exec'd directly by the harness (via /bin/sh); a missing +x bit
# makes it fail silently with "Permission denied". chmod the whole family so a
# newly added hook can never regress this way (enforced by lint-hooks-executable.py).
chmod +x "$REPO/scripts/hook-"*.py

"$REPO/cursor/scripts/install-cursor-links.sh"

# Merge versioned policy permissions (read-only allowlist + env) into the
# machine-local $CLAUDE_AGENT_HOME/settings.json; machine-specific keys are preserved.
CLAUDE_SETTINGS="$CLAUDE_AGENT_HOME/settings.json" "$REPO/scripts/apply-settings.sh"

# Create $CLAUDE_AGENT_HOME/agent-identity.local (per-machine difficulty channel) if absent.
"$REPO/scripts/configure-identity.sh"

# Wire the canonical reminder-hook set into settings.json. Hooks are a
# machine-specific key (apply-settings.sh does not merge them), so the repo's
# hook scripts stay dead without this idempotent installer.
"$REPO/scripts/install-reminder-hooks.sh"

"$REPO/scripts/install-git-hooks.sh"
"$REPO/scripts/verify-layout-contract.sh" 2>/dev/null || true
"$REPO/scripts/verify-instructions-sync.sh" || true

echo "Global symlinks ok. Per-project setup (run from each repo root):"
echo "  robot/deepagent  →  .claude/scripts/setup-local.sh"
echo "  logos            →  .claude/scripts/setup-local.sh"
ls -la "$CLAUDE_AGENT_HOME/memory-global" "$CLAUDE_AGENT_HOME/skills" "$CLAUDE_AGENT_HOME/agents" 2>/dev/null || true

# ── One-time login hint (auth is per-config-root) ─────────────────────────────
# The CLI records a completed login for a config root as an "oauthAccount" block
# in <root>/.claude.json. Auth/session is PER-ROOT: the macOS Keychain token alone
# does not authenticate a fresh root (verified empirically, Stage A), and by
# binding user decision we copy/symlink NO credential and add NO apiKeyHelper —
# so the isolated system root needs its own one-time login. Detection is
# side-effect-free (a grep on one file); we never run `claude`, which would need a
# TTY this setup shell lacks. Absence of the recorded account => print the login
# command for the user to run once.
if [[ ! -f "$CLAUDE_AGENT_HOME/.claude.json" ]] \
   || ! grep -q '"oauthAccount"' "$CLAUDE_AGENT_HOME/.claude.json" 2>/dev/null; then
  # Show ~/.claude-agent for the default root; the real path for an overridden one.
  _login_root="$CLAUDE_AGENT_HOME"
  [[ "$CLAUDE_AGENT_HOME" == "$HOME/.claude-agent" ]] && _login_root="~/.claude-agent"
  echo
  echo "The agent system uses its own config root, separate from your personal ~/.claude."
  echo "Log in to it ONCE (nothing is copied from ~/.claude; the token stays in your Keychain):"
  echo "    CLAUDE_CONFIG_DIR=$_login_root claude auth login"
  echo "(or 'claude-agent /login' if you source scripts/claude-launchers.sh)"
fi
