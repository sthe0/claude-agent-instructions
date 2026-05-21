#!/usr/bin/env bash
# Sync local agents/memory in Arcadia: branch the0-agents, mount ~/arcadia_the0-agents.
# Analog of sync-instructions-repo.sh (git). Logs: ~/.local/log/junk-agents-arc-sync.log
set -euo pipefail

MOUNT="${THE0_AGENTS_MOUNT:-$HOME/arcadia_the0-agents}"
BRANCH="${THE0_AGENTS_BRANCH:-the0-agents}"
JUNK_ROOT="${JUNK_AGENTS_ROOT:-$MOUNT/junk/the0/agents}"
LOG_DIR="$HOME/.local/log"
LOG_FILE="$LOG_DIR/junk-agents-arc-sync.log"

log() {
  mkdir -p "$LOG_DIR"
  printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE"
}

die() {
  log "ERROR: $*"
  exit 1
}

[[ -d "$MOUNT" ]] || die "mount missing: $MOUNT (create: scripts/setup-the0-agents-mount.sh)"
[[ -d "$MOUNT/.arc" ]] || die "not an arc mount: $MOUNT"

cmd_pull() {
  log "pull start ($MOUNT branch=$BRANCH)"
  cd "$MOUNT"
  local current
  current="$(arc info --json 2>/dev/null | sed -n 's/.*"name": *"\([^"]*\)".*/\1/p' | head -1 || arc branch 2>/dev/null | grep '^\*' | awk '{print $2}')"
  if [[ "$current" != "$BRANCH" ]]; then
    arc checkout "$BRANCH" 2>&1 | tee -a "$LOG_FILE" || die "checkout $BRANCH failed"
  fi
  if arc pull 2>&1 | tee -a "$LOG_FILE"; then
    log "pull: done"
  else
    log "WARN: arc pull failed — resolve manually in $MOUNT"
    return 1
  fi
}

cmd_push() {
  log "push start"
  cd "$MOUNT"
  arc checkout "$BRANCH" 2>/dev/null || true
  if arc push 2>&1 | tee -a "$LOG_FILE"; then
    log "push: done"
  else
    log "WARN: arc push failed"
    return 1
  fi
}

cmd_status() {
  cd "$MOUNT"
  arc checkout "$BRANCH" 2>/dev/null || true
  arc status "$JUNK_ROOT" 2>&1 | tee -a "$LOG_FILE"
  log "mount=$MOUNT junk=$JUNK_ROOT"
}

cmd_sync() {
  cmd_pull || true
  cmd_push || true
}

usage() {
  echo "Usage: $0 {pull|push|sync|status}" >&2
  echo "  THE0_AGENTS_MOUNT=$MOUNT" >&2
  exit 2
}

main() {
  case "${1:-sync}" in
    pull) cmd_pull ;;
    push) cmd_push ;;
    sync) cmd_sync ;;
    status) cmd_status ;;
    *) usage ;;
  esac
}

main "$@"
