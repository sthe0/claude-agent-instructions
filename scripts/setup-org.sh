#!/usr/bin/env bash
# One-command onboarding for using this agent system in a new organization.
#
# Difficulty removed: a developer in a different org should not have to learn the
# agent-identity / difficulty-channel internals to get started. This wizard wraps
# configure-identity.sh (which auto-detects the right channel — `github` for any
# machine without internal Yandex signals) and prints what is left to do.
#
# Usage:
#   setup-org.sh [--non-interactive]
#
# Idempotent: never overwrites an existing ~/.claude/agent-identity.local
# (that guarantee is delegated to configure-identity.sh). No network calls.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for arg in "$@"; do
  case "$arg" in
    --non-interactive) : ;;  # reserved: this wizard prompts for nothing today
    -h|--help) grep '^#' "$0" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0 ;;
    *) echo "setup-org.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

IDENTITY_FILE="$HOME/.claude/agent-identity.local"

# 1. Detect + write the per-machine identity (idempotent; never overwrites).
"$SCRIPTS_DIR/configure-identity.sh"

_channel="$(sed -n 's/^difficulty_channel=//p' "$IDENTITY_FILE" 2>/dev/null || true)"
[[ -n "$_channel" ]] || _channel="(unset)"

# 1b. Write project_backend/tracker_backend if unset (idempotent; mirrors difficulty_channel).
_det_ws="git" _det_tr="none"
if _det_out="$(cd "$SCRIPTS_DIR" && python3 -m project_entry.detect_backend 2>/dev/null)"; then
  read -r _det_ws _det_tr <<<"$_det_out" || true
fi
_id_ws="$(sed -n 's/^project_backend=//p' "$IDENTITY_FILE" 2>/dev/null | head -1 || true)"
_id_tr="$(sed -n 's/^tracker_backend=//p' "$IDENTITY_FILE" 2>/dev/null | head -1 || true)"
if [[ -z "$_id_ws" ]]; then
  printf 'project_backend=%s\n' "$_det_ws" >> "$IDENTITY_FILE"
  _id_ws="$_det_ws"
fi
if [[ -z "$_id_tr" ]]; then
  printf 'tracker_backend=%s\n' "$_det_tr" >> "$IDENTITY_FILE"
  _id_tr="$_det_tr"
fi

# 2. Onboarding checklist.
cat <<EOF

-- Onboarding checklist (org-portable) ------------------------------
  difficulty channel : ${_channel}   (auto-detected; edit ${IDENTITY_FILE} to change)
  project backend    : ${_id_ws}/${_id_tr}   (auto-detected; edit ${IDENTITY_FILE} to change)
  [done] clone + setup-symlinks.sh   (symlinks, settings, hooks)
  [done] setup-org.sh                (this wizard -- identity written)
  [todo] scripts/doctor.sh           (expect all [ OK ])
  [todo] git / gh auth               (your org's VCS -- Claude uses git/gh natively)
  [opt ] per-project memory          (scripts/setup-project-memory.sh inside a repo)

Notes:
  * Internal-Yandex facilities (Arcadia arc, Startrek) are opt-in, not assumed.
  * Public services stay available: yandex-cloud-expert works (yandex.cloud is public).
  * Org-specific runbooks live in <project>/.claude/, never in this Core repo.
  * Long-job orchestrators are configurable: set long_job_orchestrators=... in
    ${IDENTITY_FILE} for your org's job runners.
---------------------------------------------------------------------
EOF
