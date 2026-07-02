#!/usr/bin/env bash
# Set the auto-compaction WINDOW (the knob you actually turn; what /context shows as
# "Auto-compact window" and what CLAUDE_CODE_AUTO_COMPACT_WINDOW / autoCompactWindow
# hold). Takes the desired window in tokens, derives the expected fire threshold, and
# REFUSES windows that would put the threshold near the post-compaction floor.
#
# Verified threshold math (decompiled claude.exe, 2026-06-17, functions Rr4/nAq/zB8/
# w1H/WYH; see memory-global/leaves/autocompact-threshold-policy.md):
#   z       = window - OUTPUT_RESERVE        # OUTPUT_RESERVE = min(maxOutputTokens, 20000) = 20000
#   trigger = min( round(z * (1 - FRACTION)), z - 13000 )   # FRACTION default 0.2
# For normal windows the FRACTION term binds, so trigger ~= 0.8*(window-20000).
# NOTE the /context "Autocompact buffer" (= OUTPUT_RESERVE + 13000 = 33000, window-
# independent) is a DISPLAY reserve, NOT window-trigger — do not use it for sizing.
#
# FRACTION (precomputeBufferFraction) is server-driven (LaunchDarkly); the binary
# fallback is 0.2. A larger live fraction lowers the real trigger, so treat the
# printed trigger as an estimate and keep the margin.
#
# HARD FLOOR: a compaction leaves ~90-97k tokens behind (structural: static prefix
# ~60k = system prompt + tools + MCP + memory + skills, plus summary ~14-20k + first
# reads; verified across 5 sessions 2026-06-17). A trigger at/below it re-fires every
# turn -> thrash (DEEPAGENT-430, harness even warns "a file/tool output is likely too
# large" — large retained tool outputs inflate the floor; see large-tool-output-
# discipline). We require trigger >= 100k floor + 50k margin = 150k, which (at
# FRACTION=0.2) means a minimum window of ~210k. For a tighter ACTIVE session use
# /compact by hand — never push the auto-trigger into the floor.
#
# Does NOT set CLAUDE_AUTOCOMPACT_PCT_OVERRIDE or CLAUDE_CODE_DISABLE_1M_CONTEXT (and
# prunes them if present): the window pin governs the trigger; the percent override
# was the source of the original thrash, and letting the 1M tier ride is fine.
#
# Writes settings/base.json and runs apply-settings.sh; because apply-settings is
# additive (live wins on conflict, even on the window key), it then forces the window
# into the live file directly. Env is read at session start: RESTART Claude Code and
# verify via /context. Commit base.json to share the new default.
#
# Usage: set-context-cap.sh <window-tokens> [--dry-run]
#   set-context-cap.sh 300000          # window 300k -> trigger ~224k (current default)
#   set-context-cap.sh 210000          # window 210k -> trigger ~152k (minimum allowed)
#   set-context-cap.sh 300000 --dry-run
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
BASE="$REPO/settings/base.json"
# shellcheck source=lib/config-root.sh
source "$REPO/scripts/lib/config-root.sh"
command -v python3 >/dev/null || { echo "set-context-cap: python3 required" >&2; exit 1; }

[[ $# -ge 1 ]] || { echo "usage: set-context-cap.sh <window-tokens> [--dry-run]" >&2; exit 2; }
WINDOW="$1"; shift
DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="1"

BASE="$BASE" DRY="$DRY" python3 - "$WINDOW" <<'PY'
import json, os, sys

base = os.environ["BASE"]
dry = bool(os.environ.get("DRY"))
try:
    window = int(sys.argv[1])
except ValueError:
    sys.exit(f"set-context-cap: window must be an integer, got {sys.argv[1]!r}")

OUTPUT_RESERVE = 20_000   # min(maxOutputTokens, 20000); maxOut default 64000 -> 20000
FLAT = 13_000             # zB8 flat subtrahend (Ks4)
FRACTION = 0.2            # precomputeBufferFraction default (server-tunable)
FLOOR = 100_000           # ~post-compaction floor (verified ~90-97k across 5 sessions, rounded up)
MARGIN = 50_000           # minimum headroom above the floor
MIN_TRIGGER = FLOOR + MARGIN

z = window - OUTPUT_RESERVE
trigger = min(round(z * (1 - FRACTION)), z - FLAT)

if trigger < MIN_TRIGGER:
    sys.exit(
        f"set-context-cap: refusing window {window} — its fire threshold ~{trigger} is "
        f"below the safe minimum {MIN_TRIGGER} (~{FLOOR} floor + {MARGIN} margin). A "
        "trigger near the floor re-fires every turn (thrash, cf. DEEPAGENT-430). At the "
        "default fraction the minimum safe window is ~210000. Use /compact by hand for a "
        "tighter active session."
    )

with open(base, encoding="utf-8") as fh:
    data = json.load(fh)
env = data.setdefault("env", {})
env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
data["autoCompactWindow"] = window
removed = [k for k in ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_DISABLE_1M_CONTEXT") if k in env]
for k in removed:
    env.pop(k, None)

print(f"window         : {window} tokens (CLAUDE_CODE_AUTO_COMPACT_WINDOW + autoCompactWindow)")
print(f"expected trigger: ~{trigger} tokens [min(round((W-{OUTPUT_RESERVE})*{1-FRACTION:.1f}), W-{OUTPUT_RESERVE+FLAT})], frac={FRACTION} (server-tunable estimate)")
print(f"margin over floor: ~{trigger - FLOOR} (floor ~{FLOOR})")
print(f"display buffer  : {OUTPUT_RESERVE + FLAT} (/context shows this; NOT window-trigger)")
if removed:
    print(f"pruned         : {', '.join(removed)} (superseded by the window pin)")
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
  # apply-settings.sh is additive and LIVE WINS on conflict — including on the window
  # key we own. Force the authoritative window into the live file directly, and prune
  # the deprecated knobs there too.
  # Live settings file at the READ-time root — on a migrated machine the CLI's
  # settings live under the isolated root, not the personal ~/.claude.
  TARGET="${CLAUDE_SETTINGS:-$(agent_home_read)/settings.json}"
  if [[ -f "$TARGET" ]] && command -v jq >/dev/null; then
    tmp="$(mktemp)"
    jq --argjson w "$WINDOW" '
      .autoCompactWindow = $w
      | if .env then
          .env.CLAUDE_CODE_AUTO_COMPACT_WINDOW = ($w|tostring)
          | .env |= del(.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, .CLAUDE_CODE_DISABLE_1M_CONTEXT)
        else . end
    ' "$TARGET" > "$tmp" && jq empty "$tmp" && mv "$tmp" "$TARGET"
  fi
  echo "set-context-cap: applied (base + live). RESTART Claude Code, then verify via /context (env is read at session start)."
fi
