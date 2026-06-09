#!/usr/bin/env bash
# Set the effective context-size cap (auto-compaction trigger) to an arbitrary
# token value, by computing the two documented Claude Code env knobs:
#   CLAUDE_CODE_DISABLE_1M_CONTEXT  -> selects the active window (200k vs 1M)
#   CLAUDE_AUTOCOMPACT_PCT_OVERRIDE -> % of that window at which auto-compact fires
#
# Effective cap = PCT% * active_window. The override can only LOWER the trigger
# below the ~83% default (Math.min clamp in Claude Code — values above are
# ignored), so the max enforceable cap is ~0.83 * 1M = 830k tokens. For a cap
# <= 166k the 200k window is used (DISABLE_1M=1); for a larger cap the 1M window
# is used (DISABLE_1M removed) with a low percentage.
#
# Caveats: auto-compaction is a *trigger*, not a hard wall — expect minor
# overshoot, and it has been reported as not always respected on the main
# session. Env changes apply to NEW sessions only (restart Claude Code).
#
# Writes settings/base.json (the stable merge source; a machine-local env value
# would be clobbered by apply-settings.sh) and runs apply-settings.sh. Re-run
# anytime to change the cap; commit base.json to share the new default.
#
# Usage: set-context-cap.sh <tokens> [--dry-run]
#   set-context-cap.sh 150000          # ~150k cap (200k window, 75%)
#   set-context-cap.sh 300000          # ~300k cap (1M window, 30%)
#   set-context-cap.sh 200000 --dry-run
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
BASE="$REPO/settings/base.json"
command -v python3 >/dev/null || { echo "set-context-cap: python3 required" >&2; exit 1; }

[[ $# -ge 1 ]] || { echo "usage: set-context-cap.sh <tokens> [--dry-run]" >&2; exit 2; }
TOKENS="$1"; shift
DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="1"

BASE="$BASE" DRY="$DRY" python3 - "$TOKENS" <<'PY'
import json, os, sys

base = os.environ["BASE"]
dry = bool(os.environ.get("DRY"))
try:
    cap = int(sys.argv[1])
except ValueError:
    sys.exit(f"set-context-cap: tokens must be an integer, got {sys.argv[1]!r}")
if cap < 10_000:
    sys.exit("set-context-cap: cap below 10k tokens is impractical")

MAX_PCT = 83  # Claude Code clamps the override at the ~83% default
W200, W1M = 200_000, 1_000_000

if cap <= int(MAX_PCT / 100 * W200):          # <= 166k -> 200k window
    window, disable_1m = W200, True
elif cap <= int(MAX_PCT / 100 * W1M):         # <= 830k -> 1M window
    window, disable_1m = W1M, False
else:
    sys.exit(
        f"set-context-cap: {cap} exceeds the enforceable max "
        f"(~{int(MAX_PCT/100*W1M)} = 83% of the 1M window; the override "
        "cannot delay compaction past the default)."
    )

pct = max(1, min(MAX_PCT, round(cap / window * 100)))
effective = round(pct / 100 * window)

with open(base, encoding="utf-8") as fh:
    data = json.load(fh)
env = data.setdefault("env", {})
env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(pct)
if disable_1m:
    env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "1"
else:
    env.pop("CLAUDE_CODE_DISABLE_1M_CONTEXT", None)

print(f"requested cap : {cap} tokens")
print(f"active window : {window} (DISABLE_1M={'1' if disable_1m else 'unset'})")
print(f"autocompact % : {pct}")
print(f"effective cap : ~{effective} tokens (compaction trigger)")
if dry:
    print("--dry-run: settings/base.json NOT modified")
    sys.exit(0)
with open(base, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print(f"wrote {base}")
PY

if [[ -z "$DRY" ]]; then
  "$REPO/scripts/apply-settings.sh"
  echo "set-context-cap: applied. Restart Claude Code for the new cap to take effect (env is read at session start)."
fi
