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

# Directory the caller invoked us from, captured BEFORE the `cd "$REPO"` below so
# the post-pull integrity gate can locate a project's .claude/agent-memory to
# verify (a detached/cron run whose cwd holds no project simply finds nothing).
INVOCATION_DIR="$(pwd -P)"

cd "$REPO" || die "repo not found: $REPO"

if [[ -z "$BRANCH" ]]; then
  # Reconcile the branch actually checked out, not a hardcoded trunk — otherwise
  # running sync from a feature branch rebases its commits onto origin/main and
  # diverges from the branch's own upstream (origin/<branch>).
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [[ -z "$BRANCH" || "$BRANCH" == HEAD ]] && BRANCH=main
fi

# Sha of the stash entry THIS RUN created; empty when we created none. The pop
# path keys off that identity and never off "the stash stack is non-empty": the
# stack is global to the repository, so a foreign session's rescue-stash — or an
# abandoned entry from an earlier run — can be sitting on top of ours.
SYNC_STASH_SHA=""

# Set once cmd_pull has confirmed it started from a tree with no unmerged paths,
# so resolve_rebase_conflicts_prefer_incoming can refuse to auto-resolve a
# conflict this pull did not create.
SYNC_PULL_CLEAN_START=0

# Like log(), but also on stderr — for every path that leaves session work
# un-restored. Those lines went to the log file alone and nobody ever read them.
loud() {
  mkdir -p "$LOG_DIR"
  printf '%s %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*" | tee -a "$LOG_FILE" >&2
}

has_uncommitted() {
  git diff --quiet || return 0
  git diff --cached --quiet || return 0
  [[ -n "$(git status --porcelain)" ]]
}

has_unmerged() {
  [[ -n "$(git ls-files --unmerged)" ]]
}

# Membership in a newline-separated list, without a subprocess: `printf | grep -q`
# can fail as SIGPIPE 141 under `set -o pipefail` on bash 3.2 (macOS).
list_contains() {
  local needle="$1" hay="$2" nl
  nl=$'\n'
  case "$nl$hay$nl" in
    *"$nl$needle$nl"*) return 0 ;;
  esac
  return 1
}

# Print the stash@{N} that currently holds <sha>. Return 0 (with the ref) when
# found, 1 when the stack was enumerated but <sha> is not on it, 2 when the
# enumeration itself failed — a caller must not conflate the two: "not found"
# means the entry is provably gone, "enumeration failed" means presence is
# UNKNOWN. Bounded by the stack size rather than probing until rev-parse
# fails, so a rev-parse that ever answers differently for an out-of-range
# index cannot spin this forever.
stash_ref_for_sha() {
  local want="$1" n=0 total sha
  # %H is one sha per line, so a foreign entry with a multi-line message cannot
  # skew the count. A failed `git stash list` fails the whole pipeline under
  # `set -o pipefail` and leaves $total empty — that is the ONLY way $total is
  # empty (an empty stack legitimately yields "0"), so it is a reliable signal.
  total="$(git stash list --format=%H 2>/dev/null | wc -l | tr -d '[:space:]')" || total=""
  [[ -z "$total" ]] && return 2
  while [[ "$n" -lt "$total" ]]; do
    sha="$(git rev-parse --verify --quiet "stash@{$n}" 2>/dev/null || true)"
    if [[ -n "$sha" && "$sha" == "$want" ]]; then
      printf '%s\n' "stash@{$n}"
      return 0
    fi
    n=$((n+1))
  done
  return 1
}

# Print the stash@{N} whose subject contains <label>. Same three-way outcome
# as stash_ref_for_sha (0 found / 1 not-found / 2 enumeration-failed) — subject-
# keyed twin for the one case where our own sha isn't known yet: the
# concurrent-writer refusal below, which fires before SYNC_STASH_SHA is ever
# set.
stash_ref_for_label() {
  local want="$1" n=0 total subject
  total="$(git stash list --format=%H 2>/dev/null | wc -l | tr -d '[:space:]')" || total=""
  [[ -z "$total" ]] && return 2
  while [[ "$n" -lt "$total" ]]; do
    subject="$(git log -1 --format=%s "stash@{$n}" 2>/dev/null || true)"
    case "$subject" in
      *"$want"*)
        printf '%s\n' "stash@{$n}"
        return 0
        ;;
    esac
    n=$((n+1))
  done
  return 1
}

stash_if_dirty() {
  has_uncommitted || return 0

  local label before after subject
  label="sync-instructions-repo $(date -u +"%Y-%m-%dT%H:%M:%SZ") pid=$$"
  before="$(git rev-parse --verify --quiet 'stash@{0}' 2>/dev/null || true)"
  log "stash uncommitted changes ($label)"
  if ! git stash push -u -m "$label"; then
    loud "WARN: 'git stash push' failed in $REPO — pull aborted, working tree untouched"
    return 1
  fi
  after="$(git rev-parse --verify --quiet 'stash@{0}' 2>/dev/null || true)"
  if [[ -z "$after" || "$after" == "$before" ]]; then
    loud "WARN: 'git stash push' created no entry in $REPO (top is still ${before:-<empty>}) — refusing to pop anything later"
    return 1
  fi
  # A differing top sha is not enough on its own: a concurrent session or an
  # overlapping timer pull can land its entry between our push and this read,
  # and we would adopt it as ours. The per-run label is the identity check.
  subject="$(git log -1 --format=%s "$after" 2>/dev/null || true)"
  case "$subject" in
    *"$label"*) SYNC_STASH_SHA="$after" ;;
    *)
      local our_ref our_sha
      if our_ref="$(stash_ref_for_label "$label")"; then
        our_sha="$(git rev-parse --verify --quiet "$our_ref" 2>/dev/null || true)"
        loud "WARN: top stash entry in $REPO is '$subject', not this run's ('$label') — another process stashed concurrently. Refusing to pop; your changes are safe at $our_ref (${our_sha:-unknown sha}). Recover with: git -C $REPO stash apply ${our_sha:-$our_ref}"
      else
        loud "WARN: top stash entry in $REPO is '$subject', not this run's ('$label') — another process stashed concurrently, AND our own entry could not be found on the stack by its label. Check by hand: git -C $REPO stash list"
      fi
      return 1
      ;;
  esac
}

# A conflicting `git stash pop` keeps its entry, so the tree can be put back to
# HEAD without losing anything — but only once that entry is proven still there.
restore_after_failed_pop() {
  local sha="$1" untracked_before="$2"

  if ! stash_ref_for_sha "$sha" >/dev/null; then
    loud "WARN: stash pop of $sha conflicted AND that entry is gone from the stack in $REPO — leaving the tree exactly as the pop left it (conflict markers included), because resetting it would destroy the only copy of that work. Resolve by hand."
    SYNC_STASH_SHA=""
    return 1
  fi

  # Only a `-u` stash has a third parent; its absence means the entry captured
  # no untracked files, which is "remove nothing", never an error. `-c
  # core.quotePath=false` on every one of these three path sources (this one,
  # $now below, and the caller's $untracked_before) so a non-ASCII path comes
  # back byte-identical from all three and the string comparisons in
  # list_contains actually match instead of comparing quoted against raw.
  local stash_untracked=""
  if git rev-parse --verify --quiet "$sha^3" >/dev/null 2>&1; then
    stash_untracked="$(git -c core.quotePath=false ls-tree -r --name-only "$sha^3" 2>/dev/null || true)"
  fi
  local now
  now="$(git -c core.quotePath=false ls-files --others --exclude-standard 2>/dev/null || true)"

  local reset_rc=0
  git reset -q --hard HEAD || reset_rc=$?

  # reset --hard does not touch untracked files (whether or not it succeeded),
  # and a leftover one makes the advertised `stash apply` recovery fail with
  # "already exists" — sweep unconditionally rather than only on the success
  # path. $now and $untracked_before were both captured before the reset, so
  # the sweep is valid regardless of its outcome, and deleting this residue is
  # loss-free: every path it touches is also in the stash.
  local path removed="" kept=""
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    list_contains "$path" "$untracked_before" && continue
    list_contains "$path" "$stash_untracked" || continue
    if [[ -f "$path" && ! -L "$path" ]] && rm -f -- "$path"; then
      removed="$removed $path"
    else
      kept="$kept $path"
    fi
  done <<< "$now"

  if [[ "$reset_rc" -ne 0 ]]; then
    local rmsg="WARN: stash pop of $sha conflicted AND 'git reset --hard HEAD' failed in $REPO — the tree still holds the conflicted merge (conflict markers included), NOT restored to HEAD. Your work is SAFE in stash $sha."
    [[ -n "$removed" ]] && rmsg="$rmsg | removed untracked residue the pop had restored:$removed"
    [[ -n "$kept" ]] && rmsg="$rmsg | COULD NOT remove:$kept — 'stash apply' will fail until you delete them"
    loud "$rmsg Resolve the tree by hand, then recover with: git -C $REPO stash apply $sha"
    SYNC_STASH_SHA=""
    return 1
  fi

  local msg="WARN: stash pop conflicted in $REPO — the working tree was restored to HEAD and your work is SAFE in stash $sha. Recover with: git -C $REPO stash apply $sha"
  [[ -n "$removed" ]] && msg="$msg | removed untracked residue the pop had restored:$removed"
  [[ -n "$kept" ]] && msg="$msg | COULD NOT remove:$kept — 'stash apply' will fail until you delete them"
  loud "$msg | older sync-instructions-repo entries may also be parked on the stack (git -C $REPO stash list)"
  SYNC_STASH_SHA=""
  return 1
}

pop_stash_if_any() {
  [[ -n "$SYNC_STASH_SHA" ]] || return 0

  local sha="$SYNC_STASH_SHA" ref untracked_before pop_rc=0

  # Snapshot untracked files BEFORE ref resolution, never after: the merge and
  # its hooks create untracked files too (so this can't be taken any earlier),
  # but stash_ref_for_sha itself spawns subprocesses, and nothing may run
  # between ref resolution and the pop below (see the comment there).
  # `-c core.quotePath=false` here too, matching restore_after_failed_pop's own
  # $now/$stash_untracked sources — without it, a non-ASCII path the MERGE
  # itself created (coincidentally also present in our stash's untracked tree)
  # comes back quoted here but raw from the other two, the list_contains
  # "already there" check misses it, and the sweep deletes a file it was meant
  # to leave alone.
  untracked_before="$(git -c core.quotePath=false ls-files --others --exclude-standard 2>/dev/null || true)"

  if ! ref="$(stash_ref_for_sha "$sha")"; then
    loud "WARN: the stash this run created ($sha) is no longer on the stack in $REPO — nothing was restored. Recover with: git -C $REPO stash apply $sha"
    SYNC_STASH_SHA=""
    return 1
  fi

  # Nothing — no subprocess, no command substitution — may sit between the ref
  # resolution above and the pop below: a concurrent `git stash push` landing
  # in that window shifts every index, and a positional pop then drops a
  # FOREIGN entry silently. `git stash pop <raw-sha>` is rejected by git, so
  # the positional conversion cannot be eliminated, only narrowed to zero
  # instructions here and detected below.
  git stash pop "$ref" || pop_rc=$?
  if [[ "$pop_rc" -eq 0 ]]; then
    # A pop that reports success must have consumed OUR entry. If $sha is
    # still on the stack, the index shifted under us and we popped a
    # DIFFERENT entry into the tree instead — the same silent-wrong-outcome
    # shape (D3) the rest of this change turns loud. A THIRD outcome here —
    # the check itself failing to enumerate — must not be read as "gone",
    # i.e. a clean pop: that is the one fail-open path left in a change whose
    # entire purpose is to make a wrong outcome loud.
    local check_rc=0
    stash_ref_for_sha "$sha" >/dev/null || check_rc=$?
    if [[ "$check_rc" -eq 0 ]]; then
      loud "WARN: 'git stash pop $ref' reported success but $sha is STILL on the stack in $REPO — a concurrent writer shifted the stack and we popped a DIFFERENT entry into the tree instead of ours. Your work is safe: git -C $REPO stash apply $sha . The tree may now hold another session's restored work — a human must look before trusting it."
      SYNC_STASH_SHA=""
      return 1
    elif [[ "$check_rc" -eq 2 ]]; then
      loud "WARN: 'git stash pop $ref' reported success but a follow-up 'git stash list' failed in $REPO — cannot tell whether $sha still on the stack (a shifted-stack pop would be silently hidden). Check by hand: git -C $REPO stash list . If your work looks wrong, recover with: git -C $REPO stash apply $sha"
      SYNC_STASH_SHA=""
      return 1
    fi
    log "stash pop $ref ($sha)"
    SYNC_STASH_SHA=""
    return 0
  fi
  restore_after_failed_pop "$sha" "$untracked_before"
  return 1
}

# NOTE: during a rebase `--ours` is the upstream/onto side and `--theirs` is the
# commit being replayed, so this keeps the LOCAL side despite the name. Flipping
# it would silently discard local commits — a separate task.
resolve_rebase_conflicts_prefer_incoming() {
  if [[ "$SYNC_PULL_CLEAN_START" != "1" ]]; then
    loud "REFUSED: auto-resolve called without a recorded clean pull start in $REPO — this conflict was not created by this pull; not touching it."
    return 1
  fi
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

# After a pull that APPLIED commits, verify instruction integrity in BOTH layers
# so a freshly-pulled inconsistency surfaces immediately instead of at the next
# random failure. Determinizes the norm "run integrity checks when instructions
# are updated" structurally, at the pull event, rather than as a prose rule the
# model may forget.
#
# Fail-open by construction: every check is guarded and the caller invokes this as
# `run_integrity_checks || true`, so a failing check emits a loud WARN (detail to
# stderr, summary via log) but NEVER aborts the already-applied pull. The verify
# entrypoints are env seams (CLAUDE_VERIFY_ALL_BIN / CLAUDE_VERIFY_LEAF_BIN),
# mirroring the migrate/setup seams above, so tests can stub them.
run_integrity_checks() {
  local verify_all="${CLAUDE_VERIFY_ALL_BIN:-$REPO/scripts/verify-all.py}"
  local verify_leaf="${CLAUDE_VERIFY_LEAF_BIN:-$REPO/scripts/verify-leaf-structure.py}"

  # Global layer: the instructions repo's own verify-all suite.
  if [[ -f "$verify_all" ]]; then
    local out rc=0
    out="$(cd "$REPO" && python3 "$verify_all" 2>&1)" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
      printf '%s\n' "$out" >&2
      log "pull: WARN global integrity check (verify-all.py) reported problems — fail-open, pull NOT aborted; review the output above"
    else
      log "pull: integrity OK (global verify-all.py)"
    fi
  fi

  # Project layer: if the pull was invoked from within a project tree carrying
  # .claude/agent-memory/MEMORY.md, verify that project's leaves with the
  # layout-aware --root checker. Walk UP from the invocation dir so a subdirectory
  # invocation still finds the project root; SKIP when the discovered agent-memory
  # resolves under $REPO itself (the instructions repo is covered by the global
  # check above, never re-checked as a "project").
  if [[ -f "$verify_leaf" ]]; then
    local dir repo_real
    dir="${INVOCATION_DIR:-$(pwd -P)}"
    repo_real="$(cd "$REPO" && pwd -P)"
    while [[ -n "$dir" && "$dir" != "/" ]]; do
      if [[ -f "$dir/.claude/agent-memory/MEMORY.md" ]]; then
        local mem="$dir/.claude/agent-memory"
        case "$mem/" in
          "$repo_real"/*) : ;;  # under the instructions repo — global check covers it
          *)
            local pout prc=0
            pout="$(cd "$REPO" && python3 "$verify_leaf" --root "$mem" 2>&1)" || prc=$?
            if [[ "$prc" -ne 0 ]]; then
              printf '%s\n' "$pout" >&2
              log "pull: WARN project integrity check (verify-leaf-structure --root $mem) reported problems — fail-open, pull NOT aborted"
            else
              log "pull: integrity OK (project $mem)"
            fi
            ;;
        esac
        break
      fi
      dir="$(dirname "$dir")"
    done
  fi
}

# After a pull that APPLIED commits, re-run the reminder-hook installer so hooks
# ADDED to the repo after this machine was onboarded (e.g. hook-guard-canon-
# readonly.py) actually reach live settings.json — the installer otherwise runs
# only on the one-time legacy migration path, leaving post-onboarding hooks dead.
# Idempotent (installs each hook once), fail-open by construction: the caller
# invokes this as `rewire_reminder_hooks || true`, so a failure emits a loud WARN
# but NEVER aborts the already-applied pull. Env-seamed (CLAUDE_INSTALL_HOOKS_BIN)
# so tests can stub the installer, mirroring run_integrity_checks's seams.
rewire_reminder_hooks() {
  local installer="${CLAUDE_INSTALL_HOOKS_BIN:-$REPO/scripts/install-reminder-hooks.sh}"
  [[ -f "$installer" ]] || return 0
  local out rc=0
  out="$(CLAUDE_INSTRUCTIONS_REPO="$REPO" bash "$installer" 2>&1)" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    printf '%s\n' "$out" >&2
    log "pull: WARN reminder-hook rewire (install-reminder-hooks.sh) failed — fail-open, pull NOT aborted; wire hooks manually with: $installer"
  else
    log "pull: reminder hooks rewired (install-reminder-hooks.sh)"
  fi
}

cmd_pull() {
  log "pull start ($REPO)"

  # An unmerged index is exactly the state in which `git stash push` creates
  # nothing while the rest of the pull goes on to pop something else. Refuse
  # before touching the remote, the history or the stack.
  if has_unmerged; then
    loud "REFUSED: $REPO has unmerged paths — resolve them (git -C $REPO status) before syncing. Nothing was fetched, stashed or popped."
    return 1
  fi
  SYNC_PULL_CLEAN_START=1

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

  if ! stash_if_dirty; then
    loud "REFUSED: could not take ownership of a stash for the local changes in $REPO — pull aborted before any history was touched."
    return 1
  fi

  if [[ "$ahead" -gt 0 ]]; then
    log "pull: rebase $ahead local commit(s) onto $REMOTE/$BRANCH"
    if ! git rebase "$REMOTE/$BRANCH"; then
      resolve_rebase_conflicts_prefer_incoming || true
      local conflict_out
      conflict_out="$(git diff --name-only --diff-filter=U 2>/dev/null || true)"
      if [[ -n "$conflict_out" ]]; then
        loud "WARN: unresolved rebase conflicts in $REPO — aborting rebase"
        if ! git rebase --abort 2>/dev/null; then
          if [[ -n "$SYNC_STASH_SHA" ]]; then
            loud "WARN: 'git rebase --abort' failed in $REPO — the tree is still mid-rebase; NOT touching the stash (resetting it now would run against a detached mid-rebase HEAD). Your work is SAFE in stash $SYNC_STASH_SHA. Resolve the rebase by hand, then recover with: git -C $REPO stash apply $SYNC_STASH_SHA"
          else
            loud "WARN: 'git rebase --abort' failed in $REPO — the tree is still mid-rebase; NOT touching the stash (resetting it now would run against a detached mid-rebase HEAD). Nothing was stashed this run; resolve the rebase by hand first."
          fi
          return 1
        fi
        pop_stash_if_any || true
        return 1
      fi
    fi
  else
    local ff_rc=0
    git merge --ff-only "$REMOTE/$BRANCH" || git pull --ff-only "$REMOTE" "$BRANCH" || ff_rc=$?
    if [[ "$ff_rc" -ne 0 ]]; then
      loud "WARN: fast-forward to $REMOTE/$BRANCH failed in $REPO"
      pop_stash_if_any || true
      return 1
    fi
  fi

  pop_stash_if_any || return 1

  # Integrity gate runs ONLY here — on the reconcile path where commits were
  # actually applied. The behind==0 and fetch-not-found early returns above skip
  # it, keeping up-to-date auto-pulls cheap. Fail-open: never aborts the pull.
  run_integrity_checks || true
  rewire_reminder_hooks || true

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
  # `sync` is the default subcommand, so this is the entry point most callers
  # (cron, timers, agents) actually reach. Keep `|| true` — a failed pull must
  # not block the push — but do not swallow the status: a pull that left session
  # work un-restored has to reach the caller's exit code, not stderr alone.
  local pull_rc=0
  cmd_pull || pull_rc=$?
  maybe_migrate_isolated || true
  cmd_push || true
  if [[ "$pull_rc" -ne 0 ]]; then
    loud "sync: pull FAILED (rc=$pull_rc) — see the WARN above; push was still attempted"
    return "$pull_rc"
  fi
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
