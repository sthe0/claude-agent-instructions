#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS="$REPO/githooks"
chmod +x "$HOOKS/post-commit" "$HOOKS/pre-commit" "$HOOKS/commit-msg" \
  "$REPO/scripts/sync-instructions-repo.sh" \
  "$REPO/scripts/verify-all.py" \
  "$REPO/scripts/verify-language.py" \
  "$REPO/scripts/lint-permissions.py" \
  "$REPO/scripts/permissions-cli.py" \
  "$REPO/scripts/spawn-specialist.py" \
  "$REPO/scripts/verify-cross-refs.py" \
  "$REPO/cursor/scripts/lint-cursor-mirror.py" \
  "$REPO/cursor/scripts/install-cursor-links.sh" \
  "$REPO/cursor/scripts/migrate-cursor-namespace.sh" \
  "$REPO/scripts/cost-report.py" \
  "$REPO/scripts/memory-audit.py" \
  "$REPO/scripts/verify-self-improvement-edit.py" \
  "$REPO/scripts/lint-prose-length.py"
cd "$REPO"
git config core.hooksPath githooks
echo "core.hooksPath=$(git config core.hooksPath)"
