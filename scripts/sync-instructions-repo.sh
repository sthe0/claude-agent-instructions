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
  printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*" | tee -a "$LOG_FILE"
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
    git stash push -u -m "sync-instructions-repo $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    echo stashed
  fi
}

pop_stash_if_any() {
  local stash_out
  stash_out="$(git stash list 2>/dev/null || true)"
  if [[ -n "$stash_out" ]]; then
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

  log "pull: incoming $behind commit(s) — reconcile session work (skills/self-improvement/policy.md § After pull)"

  local did_stash=false
  if has_uncommitted; then
    stash_if_dirty
    did_stash=true
  fi

  if [[ "$ahead" -gt 0 ]]; then
    log "pull: rebase $ahead local commit(s) onto $REMOTE/$BRANCH"
    if ! git rebase "$REMOTE/$BRANCH"; then
      resolve_rebase_conflicts_prefer_incoming || true
      local conflict_out
      conflict_out="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
      if [[ -n "$conflict_out" ]]; then
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
  # This command pushes $BRANCH (origin/$BRANCH), NOT the current HEAD. On a feature
  # branch your HEAD commits are not what gets published — warn so a no-op push to
  # $BRANCH is never mistaken for "work published" (the posted != published trap).
  local cur ahead
  cur="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  [[ "$cur" != "$BRANCH" ]] && \
    log "push: WARNING — HEAD is '$cur', not '$BRANCH'; this pushes '$BRANCH' only. Commits on '$cur' are NOT published here — push that branch directly if intended."
  # Count what ACTUALLY goes to $BRANCH (origin/$BRANCH..$BRANCH), not origin/$BRANCH..HEAD:
  # with HEAD != $BRANCH the two diverge and a HEAD-based count reports a false success.
  ahead="$(git rev-list --count "$REMOTE/$BRANCH".."$BRANCH" 2>/dev/null || echo 0)"
  if [[ "$ahead" -eq 0 ]]; then
    log "push: nothing to push ($BRANCH up to date with $REMOTE/$BRANCH)"
    return 0
  fi
  # Capture output so a "no push rights" failure degrades into a graceful skip
  # (the local commit(s) stay intact and the agent keeps working) instead of a
  # cryptic set -euo pipefail abort. Other failures (e.g. remote moved ahead)
  # still propagate so the pull → resolve → push guidance applies.
  local out rc=0
  out="$(git push "$REMOTE" "$BRANCH" 2>&1)" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    log "push: done ($ahead commit(s) to $BRANCH)"
    return 0
  fi
  printf '%s\n' "$out" | tee -a "$LOG_FILE" >&2
  if printf '%s' "$out" | grep -qiE 'permission|denied|forbidden|403|read[ -]?only|not authorized|access rights'; then
    log "push: SKIPPED — no push rights to $REMOTE/$BRANCH. Your $ahead local commit(s) stay in $REPO and the system keeps working. To contribute upstream, fork sthe0/claude-agent-instructions, push to your fork, and open a PR."
    return 0
  fi
  log "push: FAILED (rc=$rc). If '$REMOTE/$BRANCH' moved ahead, run '$0 pull', resolve, then '$0 push'."
  return "$rc"
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
