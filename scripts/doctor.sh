#!/usr/bin/env bash
# Readiness preflight: "am I ready to start working with the agent?"
#
# A new user runs this ONCE after setup-symlinks.sh to confirm the agent is
# wired into their Claude Code install before opening the first task. Distinct
# from verify-instructions-sync.sh (which checks symlink *integrity* from the
# repo-developer's side) — this checks the *user-facing* runtime: the CLI is
# present, the constitution is loaded, the engine + its gate hooks are armed,
# and agentctl runs. Read-only; mutates nothing.
#
# Usage:
#     ~/claude-agent-instructions/scripts/doctor.sh
#
# Exit 0 if every hard check passes; 1 if any hard check fails (soft checks
# only WARN). Override the repo location with CLAUDE_INSTRUCTIONS_REPO.
set -uo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
source "$REPO/scripts/lib/config-root.sh"
SETTINGS="$CLAUDE_AGENT_HOME/settings.json"
FAIL=0

pass() { printf '  [ OK ] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }
warn() { printf '  [WARN] %s\n' "$1"; }
_realpath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

echo "Agent readiness check (repo: $REPO)"
echo

# 1. Claude Code CLI on PATH — without it there is no main dialog to talk to.
if command -v claude >/dev/null 2>&1; then
  pass "Claude Code CLI found ($(command -v claude))"
else
  fail "Claude Code CLI ('claude') not on PATH — install Claude Code first"
fi

# 2. The constitution is loaded: ~/.claude/CLAUDE.md must symlink into this repo.
if [[ -L "$CLAUDE_AGENT_HOME/CLAUDE.md" ]] \
   && [[ "$(_realpath "$CLAUDE_AGENT_HOME/CLAUDE.md")" == "$(_realpath "$REPO/CLAUDE.md")" ]]; then
  pass "$CLAUDE_AGENT_HOME/CLAUDE.md -> repo CLAUDE.md"
else
  fail "$CLAUDE_AGENT_HOME/CLAUDE.md does not point at the repo — run scripts/setup-symlinks.sh"
fi

# 3. Engine + gate hooks wired into settings.json (apply-settings.sh does NOT
#    merge hooks; setup-symlinks.sh runs install-reminder-hooks.sh for this).
if [[ -f "$SETTINGS" ]] \
   && grep -q "hook-state-gate.py" "$SETTINGS" \
   && grep -q "hook-engine-start.py" "$SETTINGS"; then
  pass "engine hooks wired in settings.json (state-gate + engine-start)"
else
  fail "engine hooks not wired in $SETTINGS — run scripts/install-reminder-hooks.sh"
fi

# 4. The coordination engine is importable and runs.
if ( cd "$REPO/scripts" && python3 -m agentctl --help ) >/dev/null 2>&1; then
  pass "agentctl engine runs (python3 -m agentctl)"
else
  fail "agentctl engine does not run — check python3 and $REPO/scripts/agentctl/"
fi

# 5. Soft: instruction-repo git hooks (only needed if you EDIT the instructions).
#    install-git-hooks.sh wires them via core.hooksPath, not .git/hooks/.
hooks_dir="$(git -C "$REPO" config --get core.hooksPath 2>/dev/null || true)"
[[ -n "$hooks_dir" && "$hooks_dir" != /* ]] && hooks_dir="$REPO/$hooks_dir"
if [[ -f "${hooks_dir:-$REPO/.git/hooks}/pre-commit" ]]; then
  pass "instruction-repo git hooks installed (pre-commit)"
else
  warn "git hooks not installed — only needed to commit instruction edits: scripts/install-git-hooks.sh"
fi

# 6. Project-entry backend pair (identity-or-detected, informational).
_id_file="${CLAUDE_AGENT_IDENTITY:-$CLAUDE_AGENT_HOME/agent-identity.local}"
_id_ws="" _id_tr=""
if [[ -r "$_id_file" ]]; then
  _tmp="$(sed -n 's/^project_backend=//p' "$_id_file" | head -1 || true)"
  [[ -n "$_tmp" ]] && _id_ws="$_tmp"
  _tmp="$(sed -n 's/^tracker_backend=//p' "$_id_file" | head -1 || true)"
  [[ -n "$_tmp" ]] && _id_tr="$_tmp"
fi
_resolved_ws="$_id_ws" _resolved_tr="$_id_tr"
if [[ -z "$_resolved_ws" || -z "$_resolved_tr" ]]; then
  if _det="$(cd "$REPO/scripts" && python3 -m project_entry.detect_backend 2>/dev/null)"; then
    read -r _det_ws _det_tr <<<"$_det" || true
    [[ -z "$_resolved_ws" ]] && _resolved_ws="${_det_ws:-git}"
    [[ -z "$_resolved_tr" ]] && _resolved_tr="${_det_tr:-none}"
  fi
fi
_resolved_ws="${_resolved_ws:-git}"
_resolved_tr="${_resolved_tr:-none}"
pass "project backend: ${_resolved_ws}/${_resolved_tr}"

# 6b. Optional per-tracker self-check (warn-only). If the resolved tracker
#     backend ships a `tracker_doctor` hook, run it and surface its verdict as
#     [OK]/[WARN] — it can NEVER flip a passing doctor to FAIL. Core defines no
#     tracker_doctor; this is the generic "run the backend's own self-check if
#     it has one" seam. Silently skips when tracker=none or no plugin/hook.
if [[ "$_resolved_tr" != "none" ]]; then
  _tr_self="$(
    source "$REPO/scripts/project_entry/registry.sh" 2>/dev/null || exit 0
    _tf="$(registry_resolve_tracker "$_resolved_tr" 2>/dev/null)" || exit 0
    source "$_tf" 2>/dev/null || exit 0
    declare -F tracker_doctor >/dev/null 2>&1 || exit 0
    if _out="$(tracker_doctor 2>&1)"; then printf 'OK\t%s' "$_out"
    else printf 'WARN\t%s' "$_out"; fi
  )"
  if [[ -n "$_tr_self" ]]; then
    _tr_msg="${_tr_self#*$'\t'}"
    if [[ "${_tr_self%%$'\t'*}" == "OK" ]]; then
      pass "tracker '$_resolved_tr' self-check: ${_tr_msg:-ok}"
    else
      warn "tracker '$_resolved_tr' self-check: ${_tr_msg:-failed}"
    fi
  fi
fi

# 7. Soft: is the isolated system root logged in? Auth is per-config-root and by
#    policy no credential is copied/symlinked into it, so the root needs its own
#    one-time login. The CLI records a completed login as an "oauthAccount" block
#    in <root>/.claude.json; detection is side-effect-free (grep on one file) — we
#    never run `claude`, which needs a TTY doctor lacks. Warn-only: an
#    unauthenticated root is expected right after setup, not a hard failure.
if [[ -f "$CLAUDE_AGENT_HOME/.claude.json" ]] \
   && grep -q '"oauthAccount"' "$CLAUDE_AGENT_HOME/.claude.json" 2>/dev/null; then
  pass "system config root is logged in ($CLAUDE_AGENT_HOME)"
else
  _login_root="$CLAUDE_AGENT_HOME"
  [[ "$CLAUDE_AGENT_HOME" == "$HOME/.claude-agent" ]] && _login_root="~/.claude-agent"
  warn "system config root not logged in — run once: CLAUDE_CONFIG_DIR=$_login_root claude auth login"
fi

# 8. Legacy layout advisory: if ~/.claude (the old in-place location) still holds
#    repo-pointing system symlinks, the isolated root may be incomplete.
if [[ -d "$HOME/.claude" && "$HOME/.claude" != "$CLAUDE_AGENT_HOME" ]]; then
  _repo_real="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$REPO")"
  _found_legacy=0
  for _check_name in CLAUDE.md config.md memory-global; do
    _check_p="$HOME/.claude/$_check_name"
    if [[ -L "$_check_p" ]]; then
      _check_tgt="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$_check_p" 2>/dev/null)" || continue
      if [[ -n "$_check_tgt" && "$_check_tgt" == "$_repo_real"* ]]; then
        _found_legacy=1
        break
      fi
    fi
  done
  if [[ "$_found_legacy" -eq 1 ]]; then
    warn "legacy in-place layout detected in $HOME/.claude — run scripts/migrate-to-isolated.sh"
  fi
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "Ready. Open 'claude' in your working directory and describe your task in plain language."
  echo "  - No ticket: just describe it; a substantive task auto-plans and stops at the approval gate."
  echo "  - With a ticket: mention the key (e.g. ABC-123) so tracker-management loads its context."
  echo "    (Posting to a tracker needs that tracker's credentials configured — see the tracker-management skill.)"
  echo "See README.md § Getting started — your first task."
else
  echo "Not ready: fix the [FAIL] lines above (usually: re-run scripts/setup-symlinks.sh), then run this again."
fi
exit "$FAIL"
