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

# Minimum Claude Code version the system requires. Source: the whole system is
# skill-driven (the Skill tool) and uses plugins + hooks. Per the Claude Code
# changelog, the Skill tool ("Added support for Claude Skills") landed in 2.0.20
# and the plugin system in 2.0.12 — so 2.0.20 is the binding floor. Overridable
# via the CLAUDE_MIN_VERSION env var (used by tests to exercise the FAIL path).
CLAUDE_MIN_VERSION="${CLAUDE_MIN_VERSION:-2.0.20}"

# version_ge A B → true when semver A >= B (uses `sort -V`; the lower of the two
# sorts first, so if B sorts first then A >= B).
version_ge() { [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$2" ]]; }

echo "Agent readiness check (repo: $REPO)"
echo

# 0. Requirements — external dependencies the system needs before anything else.
#    Missing git/python3 makes the launchers, engine, and git workflow unusable.
for _dep in git python3; do
  if command -v "$_dep" >/dev/null 2>&1; then
    pass "dependency '$_dep' found ($(command -v "$_dep"))"
  else
    fail "dependency '$_dep' not on PATH — install it before setup"
  fi
done

# 1. Claude Code CLI on PATH + minimum version — without it there is no main
#    dialog to talk to, and below CLAUDE_MIN_VERSION the Skill/plugin/hook
#    surface the system depends on is absent.
if command -v claude >/dev/null 2>&1; then
  pass "Claude Code CLI found ($(command -v claude))"
  _claude_ver="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if [[ -z "$_claude_ver" ]]; then
    warn "could not parse 'claude --version' — need >= $CLAUDE_MIN_VERSION (Skill tool + plugins)"
  elif version_ge "$_claude_ver" "$CLAUDE_MIN_VERSION"; then
    pass "Claude Code version $_claude_ver (>= $CLAUDE_MIN_VERSION)"
  else
    fail "Claude Code $_claude_ver < required $CLAUDE_MIN_VERSION — upgrade Claude Code (Skill tool + plugins)"
  fi
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

# 6c. Soft: `gh` (GitHub CLI) — the transport behind the `github` tracker
#     backend's tracker_read / tracker_comment / tracker_publish_plan verbs.
#     Warn-only: a missing or unauthenticated gh degrades those verbs to their
#     single nonzero class (registry.sh's presence-probe contract) — the
#     launchers and opening.py run fine without it.
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    pass "dependency 'gh' found and authenticated ($(command -v gh))"
  else
    warn "gh (GitHub CLI) found but not authenticated — run: gh auth login"
  fi
else
  warn "gh (GitHub CLI) not found — install it for tracker_read/tracker_comment/tracker_publish_plan (see docs/operations/setup.md)"
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
#    repo-pointing system symlinks, the isolated root may be incomplete. Detection
#    is the shared agent_legacy_inplace_layout helper (config-root.sh) — the single
#    source of truth also used by sync-instructions-repo.sh.
if agent_legacy_inplace_layout "$REPO"; then
  warn "legacy in-place layout detected in $HOME/.claude — run scripts/migrate-to-isolated.sh"
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "Ready. How to start working on tasks:"
  echo "  - Run the SYSTEM with 'claude-task' (or 'claude-agent' for scripted -p/-c) — this is the"
  echo "    disciplined agent on the isolated root ($CLAUDE_AGENT_HOME). Bare 'claude' stays your"
  echo "    OWN personal ~/.claude, untouched — that is the user↔core switch."
  echo "  - Then just describe your task in plain language (English or Russian):"
  echo "      • No ticket: a substantive task auto-plans and stops at the approval gate."
  echo "      • With a ticket: mention the key (e.g. ABC-123) so tracker-management loads its context."
  echo "        (Posting to a tracker needs that tracker's credentials — see the tracker-management skill.)"
  echo "See README.md § Getting started — your first task."
else
  echo "Not ready: fix the [FAIL] lines above (usually: re-run scripts/setup-symlinks.sh), then run this again."
fi
exit "$FAIL"
