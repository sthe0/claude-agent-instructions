#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ls -la "$HOME/.claude/skills" > "$REPO/docs/skills-symlinks.txt"
echo "Wrote docs/skills-symlinks.txt"
