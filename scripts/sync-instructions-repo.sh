#!/usr/bin/env bash
# Sync ~/claude-agent-instructions with origin (pull / push / status).
# Used by agents and cron; logs to ~/.local/log/claude-agent-instructions-sync.log
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
BRANCH="${CLAUDE_INSTRUCTIONS_BRANCH:-main}"
REMOTE="${CLAUDE_INSTRUCTIONS_REMOTE:-origin}"
LOG_DIR="$HOME/.local/log"
LOG_FILE="$LOG_DIR/claude-agent-instructions-sync.log"

log() {
  mkdir -p "$LOG_DIR"
  printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG_FILE"
}

die() {
  log "ERROR: $*"
  exit 1
}

cd "$REPO" || die "repo not found: $REPO"

has_uncommitted() {
  ! git diff --quiet || return 0
  ! git diff --cached --quiet && return 0
  [[ -n "$(git status --porcelain)" ]]
}

stash_if_dirty() {
  if has_uncommitted; then
    log "stash uncommitted changes"
    git stash push -u -m "sync-instructions-repo $(date -Is)"
    echo stashed
  fi
}

pop_stash_if_any() {
  if git stash list | grep -q .; then
    log "stash pop"
    if ! git stash pop; then
      log "WARN: stash pop conflict — resolve manually in $REPO"
      return 1
    fi
  fi
  return 0
}

resolve_rebase_conflicts_prefer_incoming() {
  local unresolved
  unresolved="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
  [[ -z "$unresolved" ]] && return 0
  log "conflicts (prefer incoming): $unresolved"
  while IFS= read -r file; do
    [[ -z "$file" ]] && continue
    git checkout --theirs -- "$file" 2>/dev/null || git checkout --theirs "$file"
    git add -- "$file"
  done <<< "$unresolved"
  GIT_EDITOR=true git rebase --continue 2>/dev/null || true
}

cmd_pull() {
  log "pull start ($REPO)"
  git fetch "$REMOTE" "$BRANCH"

  local behind ahead
  behind="$(git rev-list --count HEAD.."$REMOTE"/"$BRANCH" 2>/dev/null || echo 0)"
  ahead="$(git rev-list --count "$REMOTE"/"$BRANCH"..HEAD 2>/dev/null || echo 0)"

  if [[ "$behind" -eq 0 ]]; then
    log "pull: already up to date (ahead=$ahead)"
    return 0
  fi

  local did_stash=false
  if has_uncommitted; then
    stash_if_dirty
    did_stash=true
  fi

  if [[ "$ahead" -gt 0 ]]; then
    log "pull: rebase $ahead local commit(s) onto $REMOTE/$BRANCH"
    if ! git rebase "$REMOTE/$BRANCH"; then
      resolve_rebase_conflicts_prefer_incoming || true
      if git diff --name-only --diff-filter=U 2>/dev/null | grep -q .; then
        log "WARN: unresolved rebase conflicts — aborting rebase"
        git rebase --abort 2>/dev/null || true
        [[ "$did_stash" == true ]] && pop_stash_if_any || true
        return 1
      fi
    fi
  else
    git merge --ff-only "$REMOTE/$BRANCH" || git pull --ff-only "$REMOTE" "$BRANCH"
  fi

  if [[ "$did_stash" == true ]]; then
    pop_stash_if_any || return 1
  fi

  log "pull: done"
}

cmd_push() {
  log "push start"
  local ahead
  ahead="$(git rev-list --count "$REMOTE/$BRANCH"..HEAD 2>/dev/null || echo 0)"
  if [[ "$ahead" -eq 0 ]]; then
    log "push: nothing to push"
    return 0
  fi
  git push "$REMOTE" "$BRANCH"
  log "push: done ($ahead commit(s))"
}

cmd_sync() {
  cmd_pull || true
  cmd_push || true
}

cmd_status() {
  git fetch "$REMOTE" "$BRANCH" 2>/dev/null || true
  git status -sb
  local behind ahead
  behind="$(git rev-list --count HEAD.."$REMOTE"/"$BRANCH" 2>/dev/null || echo 0)"
  ahead="$(git rev-list --count "$REMOTE"/"$BRANCH"..HEAD 2>/dev/null || echo 0)"
  log "status: behind=$behind ahead=$ahead"
}

usage() {
  echo "Usage: $0 {pull|push|sync|status}" >&2
  exit 2
}

main() {
  local cmd="${1:-sync}"
  case "$cmd" in
    pull) cmd_pull ;;
    push) cmd_push ;;
    sync) cmd_sync ;;
    status) cmd_status ;;
    *) usage ;;
  esac
}

main "$@"
