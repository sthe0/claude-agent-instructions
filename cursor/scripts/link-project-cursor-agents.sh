#!/usr/bin/env bash
# Symlink <project>/.cursor/agents/*.md to canonical cursor/agents in the instructions repo.
# Safe to re-run. Refuses to overwrite regular files (move aside manually).
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
ROOT="$(cd "${1:?usage: link-project-cursor-agents.sh <project_root>}" && pwd)"

link() {
  local target="$1" linkpath="$2"
  if [[ -e "$linkpath" && ! -L "$linkpath" ]]; then
    echo "refuse: $linkpath exists and is not a symlink (move aside manually)" >&2
    exit 1
  fi
  ln -sfn "$target" "$linkpath"
}

if [[ ! -d "$REPO/cursor/agents" ]]; then
  echo "error: missing $REPO/cursor/agents (pull claude-agent-instructions?)" >&2
  exit 1
fi

mkdir -p "$ROOT/.cursor/agents"

for file_path in "$REPO/cursor/agents/"*.md; do
  [[ -f "$file_path" ]] || continue
  base="$(basename "$file_path")"
  [[ "$base" == "README.md" ]] && continue
  link "$file_path" "$ROOT/.cursor/agents/$base"
done

echo "ok: $ROOT/.cursor/agents -> $REPO/cursor/agents/"
