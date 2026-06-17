#!/usr/bin/env bash
# Set the effective context-size cap (auto-compaction trigger) to a token value
# by pinning the window knob:
#   CLAUDE_CODE_AUTO_COMPACT_WINDOW (env, precedence) + autoCompactWindow (top-level)
#
# The harness fires auto-compaction at threshold = effective_window - 13000, where
# effective_window = min(window setting, model max). So to land the trigger at the
# requested cap we pin window = cap + 13000.
#
# HARD FLOOR: a compaction leaves ~150k tokens behind (system prompt + memory +
# recent turns). A trigger at or below that floor re-fires every turn -> thrash
# (this is what DEEPAGENT-430's window=200k/pct=75 -> 150k config caused). So this
# script REFUSES any cap below ~200k (floor 150k + 50k margin). If you need a
# tighter ACTIVE session, use /compact by hand — never aim the auto-trigger at the
# floor. See memory-global/leaves/autocompact-threshold-policy.md.
#
# This script does NOT set CLAUDE_AUTOCOMPACT_PCT_OVERRIDE or
# CLAUDE_CODE_DISABLE_1M_CONTEXT (and removes them if present): the window pin caps
# the trigger regardless of model/1M tier, so the percent override is unneeded and
# was the source of the thrash. Letting the 1M tier ride is fine.
#
# Caveats: auto-compaction is a *trigger*, not a hard wall — expect minor overshoot.
# Env is read at session start: RESTART Claude Code and verify via /context. Note
# apply-settings.sh is additive (live env keys win on conflict and removed base
# keys are NOT cleared from live) — this script also prunes the stale keys from the
# live file so the change actually takes effect.
#
# Writes settings/base.json (the stable merge source) and runs apply-settings.sh.
# Commit base.json to share the new default.
#
# Usage: set-context-cap.sh <tokens> [--dry-run]
#   set-context-cap.sh 387000          # default-equivalent (400k window)
#   set-context-cap.sh 250000          # ~250k cap (263k window)
#   set-context-cap.sh 387000 --dry-run
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

BUFFER = 13_000           # harness autocompact buffer: trigger = window - BUFFER
FLOOR = 150_000           # ~post-compaction floor (retained context after a compact)
MARGIN = 50_000           # minimum headroom above the floor
MIN_CAP = FLOOR + MARGIN  # 200k — below this the trigger collides with the floor -> thrash

if cap < MIN_CAP:
    sys.exit(
        f"set-context-cap: refusing cap {cap} — it is at or below the ~{FLOOR} "
        f"post-compaction floor (+{MARGIN} margin = min {MIN_CAP}). A trigger near "
        "the floor re-fires every turn (thrash, cf. DEEPAGENT-430). Use /compact by "
        "hand for a tighter active session instead of lowering the auto-trigger."
    )

window = cap + BUFFER

with open(base, encoding="utf-8") as fh:
    data = json.load(fh)
env = data.setdefault("env", {})
env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
data["autoCompactWindow"] = window
# Prune the deprecated knobs that caused the thrash; the window pin supersedes them.
removed = [k for k in ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_DISABLE_1M_CONTEXT") if k in env]
for k in removed:
    env.pop(k, None)

print(f"requested cap : {cap} tokens")
print(f"pinned window : {window} (CLAUDE_CODE_AUTO_COMPACT_WINDOW + autoCompactWindow)")
print(f"trigger       : ~{window - BUFFER} tokens (window - {BUFFER}); floor ~{FLOOR}, margin ~{cap - FLOOR}")
if removed:
    print(f"pruned        : {', '.join(removed)} (superseded by the window pin)")
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
  # apply-settings.sh is additive (live env keys win, removed base keys persist),
  # so prune the deprecated knobs directly from the live file too.
  TARGET="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
  if [[ -f "$TARGET" ]] && command -v jq >/dev/null; then
    tmp="$(mktemp)"
    jq 'if .env then .env |= del(.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, .CLAUDE_CODE_DISABLE_1M_CONTEXT) else . end' \
      "$TARGET" > "$tmp" && jq empty "$tmp" && mv "$tmp" "$TARGET"
  fi
  echo "set-context-cap: applied. RESTART Claude Code, then verify via /context (env is read at session start)."
fi
