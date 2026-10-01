#!/usr/bin/env bash
# Run the smoke-test recipe against an instruction-sandbox root (see
# instruction-sandbox.sh and docs/operations/pre-land-instruction-smoke-test.md).
# Usage: instruction-sandbox-verify.sh [--no-live] [--timeout <seconds>] <root>
#   --no-live   skip the live `claude -p` launches (no marker is written)
#   --timeout   bound for each live launch (default: the live script's own)
#
# Prints one `CHECK <name> PASS|FAIL|UNAVAILABLE <detail>` line per check:
#   static:lint-prose-length  static:verify-layout-contract
#   static:verify-instructions-sync  static:lint-hooks-executable
#   project:structure         only when the sandbox composed a project
#   live:core-marker          a tool-less launch must echo a marker that is reachable
#                             only through CLAUDE.md's @~/.claude-agent/config.md import
#   live:project-marker       only with a project; cwd inside the composed project
#   canon:unchanged           canon snapshot before vs after the whole run
# and a final `RESULT: PASS|FAIL|UNAVAILABLE`. A live launch that did not complete
# (auth, timeout, launch error) is UNAVAILABLE, never FAIL: only a completed launch
# whose reply lacks the marker is `FAIL marker-missing`. Every check runs even after
# an earlier FAIL.
# Exit: 0 PASS, 1 FAIL, 3 UNAVAILABLE, 2 usage.
#
# The static checks are the sandbox clone's own copies, run under the sandbox
# environment; the live checks and the canon snapshot are this checkout's tools.
# Writes: only the markers appended to <root>/core/config.md and, with a project,
# <root>/project/CLAUDE.md (a previous run's markers are replaced).
set -euo pipefail

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "instruction-sandbox-verify: $*" >&2; exit 2; }

no_live=""
timeout_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-live) no_live=1; shift ;;
    --timeout) [[ $# -ge 2 ]] || die "--timeout needs a value"; timeout_args=(--timeout "$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown argument: $1" ;;
    *) break ;;
  esac
done
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
root="$(readlink -f "$1")"
[[ -f "$root/sandbox.env" && -d "$root/core" && -d "$root/home" ]] \
  || die "not an instruction-sandbox root: $1"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_value() { sed -n "s/^$1=//p" "$root/sandbox.env" | head -n 1; }
composer="$(env_value ISB_COMPOSER)"
project_mount="$(env_value ISB_PROJECT_MOUNT)"

scrub=(CLAUDE_CODE_SESSION_ID AGENT_LINEAGE_IDS CLAUDE_CODE_MESSAGING_SOCKET
       CLAUDE_CODE_MESSAGING_TOKEN CLAUDE_POLICY_LEDGER CLAUDE_PROJECT_PLUGIN_DIR
       CLAUDE_DIFFICULTY_PLUGIN_DIR CLAUDE_AUTH_PROFILE_DIR
       CLAUDE_INSTRUCTION_SANDBOX_PLUGIN_DIR CLAUDE_INSTRUCTIONS_CANON)
sbx=(env)
for v in "${scrub[@]}"; do sbx+=(-u "$v"); done
while IFS= read -r v; do
  [[ "$v" == AGENTCTL_* ]] && sbx+=(-u "$v")
done < <(compgen -e)
sbx+=(HOME="$root/home"
      CLAUDE_AGENT_HOME="$root/home/.claude-agent"
      CLAUDE_CONFIG_DIR="$root/home/.claude-agent"
      CLAUDE_INSTRUCTIONS_REPO="$root/core")

n_fail=0
n_unavailable=0
report() {
  local status="$1" name="$2"; shift 2
  printf 'CHECK %s %s%s\n' "$name" "$status" "${*:+ $*}"
  case "$status" in
    FAIL) n_fail=$((n_fail + 1)) ;;
    UNAVAILABLE) n_unavailable=$((n_unavailable + 1)) ;;
  esac
}

# One line of detail: the first FAIL line if any, else the last line.
detail_of() {
  local out="$1" line
  line="$(grep -m1 -E 'FAIL|Error|error' <<< "$out" || true)"
  [[ -n "$line" ]] || line="$(tail -n 1 <<< "$out")"
  printf '%s' "${line:0:200}"
}

static_check() {
  local name="$1" out rc=0; shift
  out="$(cd "$root/core" && "${sbx[@]}" "$@" 2>&1)" || rc=$?
  if [[ $rc -eq 0 ]]; then report PASS "static:$name" "$(detail_of "$out")"
  else report FAIL "static:$name" "exit=$rc $(detail_of "$out")"; fi
}

snapshot() {
  local args=()
  [[ -z "$composer" ]] || args=(--composer "$composer")
  "$here/lib/instruction-sandbox-canon-snapshot.sh" ${args[@]+"${args[@]}"}
}

snapshot_before="" snapshot_ok=1
snapshot_before="$(snapshot 2>&1)" || snapshot_ok=0

static_check lint-prose-length python3 "$root/core/scripts/lint-prose-length.py"
static_check verify-layout-contract "$root/core/scripts/verify-layout-contract.sh"
static_check verify-instructions-sync "$root/core/scripts/verify-instructions-sync.sh"
static_check lint-hooks-executable python3 "$root/core/scripts/lint-hooks-executable.py" --root "$root/core"

if [[ -n "$project_mount" ]]; then
  rc=0
  out="$("$here/lib/instruction-sandbox-project-check.sh" "$root" 2>&1)" || rc=$?
  line="$(grep -m1 '^CHECK project:structure ' <<< "$out" || true)"
  if [[ -n "$line" ]]; then
    read -r _ _ status rest <<< "$line"
    report "$status" project:structure "$rest"
  else
    report FAIL project:structure "exit=$rc $(detail_of "$out")"
  fi
fi

new_token() { printf '%s-%s' "$1" "$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"; }

# Replace any earlier marker, then append one plain-text marker line.
write_marker() {
  local file="$1" token="$2"
  { grep -v '^SANDBOX-MARKER: ' "$file" || true; } > "$file.isb-tmp"
  printf '\nSANDBOX-MARKER: %s\n' "$token" >> "$file.isb-tmp"
  mv -- "$file.isb-tmp" "$file"
}

live_check() {
  local name="$1" cwd="$2" out rc=0 missing reason; shift 2
  local expect=()
  for t in "$@"; do expect+=(--expect "$t"); done
  out="$(python3 "$here/instruction-sandbox-live.py" --root "$root" --cwd "$cwd" \
           ${timeout_args[@]+"${timeout_args[@]}"} "${expect[@]}" 2>/dev/null)" || rc=$?
  case "$rc" in
    0) report PASS "live:$name" ;;
    1) missing="$(sed -n 's/^missing //p' <<< "$out" | tr '\n' ' ')"
       report FAIL "live:$name" "marker-missing ${missing% }" ;;
    3) reason="$(sed -n 's/^UNAVAILABLE //p' <<< "$out" | head -n 1)"
       report UNAVAILABLE "live:$name" "${reason:-unknown}" ;;
    *) report UNAVAILABLE "live:$name" "live-script-exit=$rc" ;;
  esac
}

if [[ -z "$no_live" ]]; then
  core_token="$(new_token isb-core)"
  write_marker "$root/core/config.md" "$core_token"
  live_check core-marker "$root" "$core_token"
  if [[ -n "$project_mount" ]]; then
    project_token="$(new_token isb-proj)"
    write_marker "$root/project/CLAUDE.md" "$project_token"
    live_check project-marker "$root/project" "$core_token" "$project_token"
  fi
fi

if [[ $snapshot_ok -eq 0 ]]; then
  report FAIL canon:unchanged "snapshot failed before the run: $(detail_of "$snapshot_before")"
else
  rc=0
  snapshot_after="$(snapshot 2>&1)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    report FAIL canon:unchanged "snapshot failed after the run: $(detail_of "$snapshot_after")"
  elif [[ "$snapshot_before" == "$snapshot_after" ]]; then
    report PASS canon:unchanged
  else
    changed="$(diff <(printf '%s\n' "$snapshot_before") <(printf '%s\n' "$snapshot_after") | grep -m1 '^[<>]' || true)"
    report FAIL canon:unchanged "${changed:0:200}"
  fi
fi

if [[ $n_fail -gt 0 ]]; then echo "RESULT: FAIL"; exit 1
elif [[ $n_unavailable -gt 0 ]]; then echo "RESULT: UNAVAILABLE"; exit 3
else echo "RESULT: PASS"; fi
