#!/usr/bin/env bash
# Sync ~/claude-agent-instructions with origin (pull / push / status).
# Used by agents and cron; logs to ~/.local/log/claude-agent-instructions-sync.log
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
BRANCH="${CLAUDE_INSTRUCTIONS_BRANCH:-}"
REMOTE="${CLAUDE_INSTRUCTIONS_REMOTE:-origin}"
LOG_DIR="$HOME/.local/log"
LOG_FILE="$LOG_DIR/claude-agent-instructions-sync.log"

# Shared legacy-layout detector (agent_legacy_inplace_layout) + CLAUDE_AGENT_HOME.
# Guard with a file check first: a missing lib must never break a plain pull/push,
# and bash 3.2 (macOS) exits a `set -e` shell on `source <missing>` even with `|| true`.
# shellcheck source=lib/config-root.sh
if [[ -f "$REPO/scripts/lib/config-root.sh" ]]; then
  source "$REPO/scripts/lib/config-root.sh"
fi

log() {
  mkdir -p "$LOG_DIR"
  printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*" | tee -a "$LOG_FILE"
}

die() {
  log "ERROR: $*"
  exit 1
}

cd "$REPO" || die "repo not found: $REPO"

if [[ -z "$BRANCH" ]]; then
  # Reconcile the branch actually checked out, not a hardcoded trunk — otherwise
  # running sync from a feature branch rebases its commits onto origin/main and
  # diverges from the branch's own upstream (origin/<branch>).
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [[ -z "$BRANCH" || "$BRANCH" == HEAD ]] && BRANCH=main
fi

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

# Interactive when both stdin and stdout are TTYs. Overridable for tests/automation:
#   CLAUDE_SYNC_NONINTERACTIVE=1   force the cron/notify-only path
#   CLAUDE_SYNC_FORCE_INTERACTIVE=1 force the auto-migrate path
is_interactive() {
  [[ -n "${CLAUDE_SYNC_NONINTERACTIVE:-}" ]] && return 1
  [[ -n "${CLAUDE_SYNC_FORCE_INTERACTIVE:-}" ]] && return 0
  [[ -t 0 && -t 1 ]]
}

# After a successful pull, ease the one-time migration from the old in-place
# ~/.claude layout to the isolated root. In an interactive terminal we run the
# (idempotent, backed-up) migration for the user; in cron/headless we NEVER move
# files unattended — only emit a loud ACTION NEEDED line so the next interactive
# run (or the user) completes it. No-op when no legacy layout is present.
# migrate/setup are indirected through env seams so tests can stub them.
maybe_migrate_isolated() {
  declare -F agent_legacy_inplace_layout >/dev/null 2>&1 || return 0
  agent_legacy_inplace_layout "$REPO" || return 0

  local migrate="${CLAUDE_MIGRATE_BIN:-$REPO/scripts/migrate-to-isolated.sh}"
  local setup="${SETUP_SYMLINKS_BIN:-$REPO/scripts/setup-symlinks.sh}"

  if is_interactive; then
    log "pull: legacy in-place ~/.claude layout detected — migrating to ${CLAUDE_AGENT_HOME:-~/.claude-agent}"
    if "$migrate" --apply && "$setup"; then
      log "pull: migration to isolated root complete — run the system with claude-task / claude-agent"
    else
      log "pull: WARN migration did not finish — run manually: $migrate --apply && $setup"
      return 1
    fi
  else
    log "pull: ACTION NEEDED — legacy in-place ~/.claude layout detected but NOT migrated (non-interactive run). Migrate to the isolated root with: $migrate --apply && $setup   (or just run 'onboard' in a terminal)."
  fi
}

cmd_pull() {
  log "pull start ($REPO)"

  local fetch_out fetch_rc=0
  fetch_out="$(git fetch "$REMOTE" "$BRANCH" 2>&1)" || fetch_rc=$?
  if [[ "$fetch_rc" -ne 0 ]]; then
    printf '%s\n' "$fetch_out" | tee -a "$LOG_FILE" >&2
    log "pull: $REMOTE/$BRANCH not found — nothing to reconcile (branch not pushed yet)"
    return 0
  fi

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

# Run `git push "$@"`, capturing output so a "no push rights" failure degrades
# into a graceful skip (the local commit(s) stay intact and the agent keeps
# working) instead of a cryptic set -euo pipefail abort. Other failures (e.g.
# remote moved ahead) still propagate so the pull → resolve → push guidance
# applies. Returns 0 on push or graceful skip, the git rc on any other failure.
push_and_degrade() {
  local out rc=0
  out="$(git push "$@" 2>&1)" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi
  printf '%s\n' "$out" | tee -a "$LOG_FILE" >&2
  if printf '%s' "$out" | grep -qiE 'permission|denied|forbidden|403|read[ -]?only|not authorized|access rights'; then
    log "push: SKIPPED — no push rights to $REMOTE/$BRANCH. Local commit(s) stay in $REPO and the system keeps working. To contribute upstream, fork sthe0/claude-agent-instructions, push to your fork, and open a PR."
    return 0
  fi
  return "$rc"
}

cmd_push() {
  log "push start"
  # This command pushes $BRANCH (origin/$BRANCH), NOT the current HEAD. On a feature
  # branch your HEAD commits are not what gets published — warn so a no-op push to
  # $BRANCH is never mistaken for "work published" (the posted != published trap).
  local cur
  cur="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  [[ "$cur" != "$BRANCH" ]] && \
    log "push: WARNING — HEAD is '$cur', not '$BRANCH'; this pushes '$BRANCH' only. Commits on '$cur' are NOT published here — push that branch directly if intended."

  if ! git rev-parse --verify --quiet "$REMOTE/$BRANCH" >/dev/null; then
    log "push: $REMOTE/$BRANCH does not exist yet — publishing $BRANCH via 'git push -u'"
    local rc=0
    push_and_degrade -u "$REMOTE" "$BRANCH" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
      log "push: done (published $BRANCH)"
      return 0
    fi
    log "push: FAILED (rc=$rc) publishing $BRANCH."
    return "$rc"
  fi

  # Count what ACTUALLY goes to $BRANCH (origin/$BRANCH..$BRANCH), not origin/$BRANCH..HEAD:
  # with HEAD != $BRANCH the two diverge and a HEAD-based count reports a false success.
  local ahead
  ahead="$(git rev-list --count "$REMOTE/$BRANCH".."$BRANCH" 2>/dev/null || echo 0)"
  if [[ "$ahead" -eq 0 ]]; then
    log "push: nothing to push ($BRANCH up to date with $REMOTE/$BRANCH)"
    return 0
  fi

  local rc=0
  push_and_degrade "$REMOTE" "$BRANCH" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    log "push: done ($ahead commit(s) to $BRANCH)"
    return 0
  fi
  log "push: FAILED (rc=$rc). If '$REMOTE/$BRANCH' moved ahead, run '$0 pull', resolve, then '$0 push'."
  return "$rc"
}

cmd_sync() {
  cmd_pull || true
  maybe_migrate_isolated || true
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
    pull) cmd_pull && maybe_migrate_isolated ;;
    push) cmd_push ;;
    sync) cmd_sync ;;
    status) cmd_status ;;
    *) usage ;;
  esac
}

main "$@"
