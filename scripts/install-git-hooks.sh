#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS="$REPO/githooks"
chmod +x "$HOOKS/post-commit" "$REPO/scripts/sync-instructions-repo.sh"
cd "$REPO"
git config core.hooksPath githooks
echo "core.hooksPath=$(git config core.hooksPath)"
