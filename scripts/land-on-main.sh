#!/usr/bin/env bash
# Land the currently STAGED change onto <remote>/<branch> (default origin/main)
# as a single new commit, from any branch, without touching the caller's
# current branch, working tree, or index.
#
# Uses an isolated `git worktree` checked out (detached) at <remote>/<branch>;
# the staged diff is applied and committed there, then pushed. See
# memory-global/leaves/system-knowledge/landing-changes-core-git-and-arc.md
set -euo pipefail

REMOTE="${LAND_ON_MAIN_REMOTE:-origin}"
BRANCH="${LAND_ON_MAIN_BRANCH:-main}"
MAX_RETRIES="${LAND_ON_MAIN_MAX_RETRIES:-5}"
REPO_DIR="."
MESSAGE=""
DRY_RUN=0

usage() {
  cat <<'USAGE' >&2
Usage: land-on-main.sh -m "<message>" [-C <repo-dir>] [--dry-run]

Land the currently STAGED change (git diff --cached) onto <remote>/<branch>
(default origin/main) as a single new commit, via an isolated git worktree.
The caller's current branch, working tree, and index are never modified.

Options:
  -m, --message <msg>   Commit message for the landed commit (required)
  -C <dir>              Run as if started in <dir> (default: .)
      --dry-run         Print what would happen; make no changes, no push
  -h, --help            Show this help

Env overrides: LAND_ON_MAIN_REMOTE (default origin), LAND_ON_MAIN_BRANCH (default main),
               LAND_ON_MAIN_MAX_RETRIES (default 5)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      MESSAGE="${2:-}"
      shift 2
      ;;
    -C)
      REPO_DIR="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "land-on-main.sh: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$MESSAGE" ]]; then
  echo "land-on-main.sh: -m/--message is required" >&2
  usage
  exit 2
fi

cd "$REPO_DIR"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if git diff --cached --quiet; then
  echo "land-on-main.sh: nothing staged (git diff --cached is empty) — stage the change first" >&2
  exit 2
fi

PATCH="$(git diff --cached)"
CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY-RUN: would land staged change from '$CUR_BRANCH' onto $REMOTE/$BRANCH:"
  echo "  message: $MESSAGE"
  git diff --cached --stat
  exit 0
fi

git fetch --quiet "$REMOTE" "$BRANCH"

WT="$(mktemp -d "${TMPDIR:-/tmp}/land-on-main.XXXXXX")"
cleanup() {
  git worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
}
trap cleanup EXIT

git worktree add --quiet --detach "$WT" "$REMOTE/$BRANCH"

(
  cd "$WT"
  if ! printf '%s\n' "$PATCH" | git apply --index -; then
    echo "land-on-main.sh: staged patch does not apply cleanly onto $REMOTE/$BRANCH" >&2
    exit 1
  fi
  git commit --quiet -m "$MESSAGE"

  attempt=0
  while true; do
    if git push --quiet "$REMOTE" "HEAD:$BRANCH"; then
      break
    fi
    attempt=$((attempt + 1))
    if [[ "$attempt" -ge "$MAX_RETRIES" ]]; then
      echo "land-on-main.sh: push to $REMOTE/$BRANCH failed after $attempt attempt(s) (non-fast-forward?)" >&2
      exit 1
    fi
    echo "land-on-main.sh: push rejected, retrying ($attempt/$MAX_RETRIES) after rebase onto latest $REMOTE/$BRANCH..." >&2
    git fetch --quiet "$REMOTE" "$BRANCH"
    if ! git rebase --quiet "$REMOTE/$BRANCH"; then
      git rebase --abort >/dev/null 2>&1 || true
      echo "land-on-main.sh: rebase onto updated $REMOTE/$BRANCH failed (conflict) — aborting" >&2
      exit 1
    fi
  done

  echo "land-on-main.sh: landed onto $REMOTE/$BRANCH ($(git rev-parse HEAD))"
)
