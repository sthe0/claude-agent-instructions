#!/usr/bin/env bash
# Merge the versioned policy base (settings/base.json) into this machine's
# ~/.claude/settings.json. Idempotent and additive:
#   - env:                     base keys added; existing local keys win on conflict,
#                              EXCEPT the autocompact keys below which base OWNS
#   - autoCompactWindow:       base wins when base defines it (active pin, not just
#                              add-when-absent); else the local value is preserved
#   - CLAUDE_CODE_AUTO_COMPACT_WINDOW: same active pin from base.env when base defines it
#   - deprecated autocompact env keys (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE,
#     CLAUDE_CODE_DISABLE_1M_CONTEXT): pruned from the live file on every run
#     (superseded by the window pin; same recipe as set-context-cap.sh)
#   - permissions.allow:       union, base entries first, then local-only entries
#   - permissions.defaultMode: local value wins; else taken from base; omitted if neither
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
  | (if $b.autoCompactWindow then .autoCompactWindow = $b.autoCompactWindow else . end)
  | (if ($b.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW) then .env.CLAUDE_CODE_AUTO_COMPACT_WINDOW = $b.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW else . end)
  | .env |= del(.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, .CLAUDE_CODE_DISABLE_1M_CONTEXT)
  | .permissions = ((.permissions // {}) + {
      allow: (
        (($b.permissions.allow // []))
        + (((.permissions.allow // [])) - (($b.permissions.allow // [])))
      )
    })
  | ( ($c.permissions.defaultMode // $b.permissions.defaultMode) as $dm
      | if $dm then .permissions.defaultMode = $dm else . end )
')"

tmp="$(mktemp)"
printf '%s\n' "$merged" > "$tmp"
jq empty "$tmp"  # validate before swapping in
cp "$TARGET" "$TARGET.bak"
mv "$tmp" "$TARGET"
echo "apply-settings: merged $BASE -> $TARGET (backup: $TARGET.bak)"
