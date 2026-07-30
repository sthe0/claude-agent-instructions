#!/usr/bin/env bash
# Merge the versioned Cursor CLI policy base (cursor/config/cli-base.json) into
# this machine's ~/.cursor/cli-config.json. Idempotent and additive:
#   - permissions.allow:  union, base entries first, then local-only entries
#   - permissions.deny:   union base ∪ local
#   - approvalMode:       base wins when base defines it
#   - sandbox:            base wins for keys base defines; other sandbox keys
#                         from local are preserved
#   - every other key in the live file is preserved untouched
# Also ensures ~/.cursor/permissions.json → cursor/config/permissions.json
# (refuse if a regular file is in the way), unless SKIP_CURSOR_PERMISSIONS_LINK=1.
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"
BASE="${CURSOR_CLI_BASE:-$REPO/cursor/config/cli-base.json}"
TARGET="${CURSOR_CLI_CONFIG:-$HOME/.cursor/cli-config.json}"
PERMISSIONS_SRC="${CURSOR_PERMISSIONS_SRC:-$REPO/cursor/config/permissions.json}"
PERMISSIONS_DST="${CURSOR_PERMISSIONS_DST:-$HOME/.cursor/permissions.json}"

command -v jq >/dev/null || { echo "apply-cursor-config: jq required" >&2; exit 1; }
[[ -f "$BASE" ]] || { echo "apply-cursor-config: missing $BASE" >&2; exit 1; }

link_permissions() {
  [[ "${SKIP_CURSOR_PERMISSIONS_LINK:-}" == "1" ]] && return 0
  [[ -f "$PERMISSIONS_SRC" ]] || return 0
  mkdir -p "$(dirname "$PERMISSIONS_DST")"
  if [[ -e "$PERMISSIONS_DST" && ! -L "$PERMISSIONS_DST" ]]; then
    echo "refuse: $PERMISSIONS_DST exists and is not a symlink (move aside manually)" >&2
    exit 1
  fi
  ln -sfn "$PERMISSIONS_SRC" "$PERMISSIONS_DST"
}

link_permissions

if [[ ! -f "$TARGET" ]]; then
  mkdir -p "$(dirname "$TARGET")"
  echo '{}' > "$TARGET"
fi

merged="$(jq -n --slurpfile base "$BASE" --slurpfile cur "$TARGET" '
  ($base[0]) as $b | ($cur[0]) as $c |
  $c
  | .permissions = ((.permissions // {}) + {
      allow: (
        (($b.permissions.allow // []))
        + (((.permissions.allow // [])) - (($b.permissions.allow // [])))
      ),
      deny: (
        (($b.permissions.deny // []))
        + (((.permissions.deny // [])) - (($b.permissions.deny // [])))
      )
    })
  | (if ($b | has("approvalMode")) then .approvalMode = $b.approvalMode else . end)
  | .sandbox = ((.sandbox // {}) + ($b.sandbox // {}))
')"

tmp="$(mktemp)"
printf '%s\n' "$merged" > "$tmp"
jq empty "$tmp"
cp "$TARGET" "$TARGET.bak"
mv "$tmp" "$TARGET"
echo "apply-cursor-config: merged $BASE -> $TARGET (backup: $TARGET.bak)"
