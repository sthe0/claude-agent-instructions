#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS="$REPO/githooks"
chmod +x "$HOOKS/post-commit" "$HOOKS/pre-commit" \
  "$REPO/scripts/sync-instructions-repo.sh" \
  "$REPO/scripts/verify-all.py" \
  "$REPO/scripts/verify-language.py" \
  "$REPO/scripts/verify-permissions.py" \
  "$REPO/scripts/permissions.py"
cd "$REPO"
git config core.hooksPath githooks
echo "core.hooksPath=$(git config core.hooksPath)"
