#!/usr/bin/env bash
# Merge the versioned policy base (settings/base.json) into this machine's
# ~/.claude/settings.json. Idempotent and additive:
#   - env:               base keys added; existing local keys win on conflict
#   - autoCompactWindow: set from base when absent in local
#   - permissions.allow: union, base entries first, then local-only entries
#   - every other key in the live file is preserved untouched
# Machine-specific keys (hooks, marketplaces, absolute Read/Write paths, model)
# live only in the local file and are never removed.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$REPO/settings/base.json"
TARGET="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"

command -v jq >/dev/null || { echo "apply-settings: jq required" >&2; exit 1; }
[[ -f "$BASE" ]] || { echo "apply-settings: missing $BASE" >&2; exit 1; }

if [[ ! -f "$TARGET" ]]; then
  echo '{}' > "$TARGET"
fi

merged="$(jq -n --slurpfile base "$BASE" --slurpfile cur "$TARGET" '
  ($base[0]) as $b | ($cur[0]) as $c |
  $c
  | .env = (($b.env // {}) + (.env // {}))
  | .autoCompactWindow = ($c.autoCompactWindow // $b.autoCompactWindow)
  | .permissions = ((.permissions // {}) + {
      allow: (
        (($b.permissions.allow // []))
        + (((.permissions.allow // [])) - (($b.permissions.allow // [])))
      )
    })
')"

tmp="$(mktemp)"
printf '%s\n' "$merged" > "$tmp"
jq empty "$tmp"  # validate before swapping in
cp "$TARGET" "$TARGET.bak"
mv "$tmp" "$TARGET"
echo "apply-settings: merged $BASE -> $TARGET (backup: $TARGET.bak)"
