#!/usr/bin/env bash
# Single bootstrap call: aggregates what the agent typically asks about at the
# very start of a session — working directory, VCS branch / status / recent
# log, project agent-memory listing, in-progress markers — into one compact
# digest. Replaces the 4–5 separate `pwd` / `arc info` / `arc status` /
# `arc log` / `ls .claude/agent-memory/` calls observed at the start of
# deepagent sessions.
#
# Output is one screen of text (≤ ~80 lines). Each section is delimited by a
# `--- <name> ---` line; missing sections (no arc, no agent memory) are
# silently skipped.
#
# Usage:
#     ~/claude-agent-instructions/scripts/session-start-digest.sh
#     ~/claude-agent-instructions/scripts/session-start-digest.sh /path/to/project
#
# All commands are read-only.

set -uo pipefail

# Resolve before the cd below — $0 may be relative to the launch directory.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/config-root.sh
. "$SCRIPT_DIR/lib/config-root.sh"

ROOT="${1:-$PWD}"
cd "$ROOT" 2>/dev/null || { echo "[digest] cannot cd into $ROOT" >&2; exit 1; }

print_section() {
    printf '\n--- %s ---\n' "$1"
}

print_section "cwd"
pwd

# VCS layer: arc takes priority (Arcadia mount); fall back to git if not arc.
if [ -d .arc ] || [ -f a.yaml ] || arc info >/dev/null 2>&1; then
    print_section "arc"
    arc info 2>/dev/null | sed -n '1,8p'
    print_section "arc status"
    arc status 2>/dev/null | sed -n '1,15p'
    print_section "arc log (5 most recent)"
    arc log -n 5 --oneline 2>/dev/null | sed -n '1,5p'
elif [ -d .git ] || git rev-parse --git-dir >/dev/null 2>&1; then
    print_section "git"
    printf 'branch: %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    print_section "git status"
    git status --short 2>/dev/null | sed -n '1,15p'
    print_section "git log (5 most recent)"
    git log -n 5 --oneline 2>/dev/null
fi

# Agent memory layer: look for the project's .claude/agent-memory/ (the leaf
# location after the per-project symlink is set up). Fall back to the
# auto-memory mirror under <config root>/projects/ if the project tree has no
# direct .claude/ — resolved at read time (override -> isolated -> legacy),
# with the legacy ~/.claude/projects/ still probed for a not-yet-migrated
# mirror on a half-migrated machine.
mem=""
if [ -d .claude/agent-memory ]; then
    mem=".claude/agent-memory"
elif [ -d ../.claude/agent-memory ]; then
    mem="../.claude/agent-memory"
fi
if [ -z "$mem" ]; then
    san=$(printf '%s' "$ROOT" | sed 's|/|-|g')
    if [ -d "$(agent_home_read)/projects/$san/memory" ]; then
        mem="$(agent_home_read)/projects/$san/memory"
    elif [ -d "$HOME/.claude/projects/$san/memory" ]; then
        mem="$HOME/.claude/projects/$san/memory"
    fi
fi

if [ -n "$mem" ]; then
    print_section "agent memory (top-level)"
    ls "$mem" 2>/dev/null | sed -n '1,10p'
    if [ -d "$mem/leaves" ]; then
        leaf_count=$(ls "$mem/leaves" 2>/dev/null | wc -l)
        printf 'leaves/: %s entries\n' "$leaf_count"
    fi
    if [ -d "$mem/experience" ]; then
        print_section "recent experience leaves"
        ls -1t "$mem/experience" 2>/dev/null | grep -v '^MEMORY\.md$' | head -5
    fi
    if [ -f "$mem/session-checkpoint.md" ]; then
        print_section "session-checkpoint.md (first 12 lines)"
        sed -n '1,12p' "$mem/session-checkpoint.md"
    fi
fi

# In-progress markers: project-specific signals worth surfacing.
if [ -n "$mem" ] && [ -d "$mem/leaves" ]; then
    inprog=$(ls "$mem/leaves" 2>/dev/null | grep -iE 'in[-_]progress|current[-_]state|pr[-_]state' | head -6)
    if [ -n "$inprog" ]; then
        print_section "in-progress / state leaves"
        printf '%s\n' "$inprog"
    fi
fi
